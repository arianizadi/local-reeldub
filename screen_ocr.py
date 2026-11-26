"""Read visible text from a video locally and track its location in display coordinates."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import os
import re
import subprocess
import unicodedata

import numpy as np


SAMPLE_FPS = 8
PACKS = {"ar": "ara", "de": "deu", "en": "eng", "es": "spa", "fa": "fas", "fr": "fra",
         "he": "heb", "hi": "hin", "it": "ita", "ja": "jpn", "ko": "kor", "nl": "nld",
         "pl": "pol", "pt": "por", "ru": "rus", "tr": "tur", "uk": "ukr", "zh": "chi_sim"}
# The soundtrack can be in a different language from a sign or a message.
SCRIPT_PACKS = ("eng", "fas", "rus", "chi_sim", "jpn", "kor", "ara")


def ocr_languages(source_language):
    listing = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True,
                             timeout=15, check=True).stdout
    installed = set(listing.splitlines()[1:])
    override = os.getenv("REELDUB_OCR_LANGUAGES", "").strip()
    if override:
        packs = re.split(r"[, +]+", override)
        if any(pack not in installed for pack in packs):
            raise RuntimeError("Install the requested Tesseract language packs or update REELDUB_OCR_LANGUAGES.")
    else:
        requested = (PACKS.get(source_language), *SCRIPT_PACKS)
        packs = list(dict.fromkeys(pack for pack in requested if pack in installed))
    if not packs:
        raise RuntimeError("Install Tesseract language data on the render worker.")
    return "+".join(packs)


def key(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def script(text):
    letters = [unicodedata.name(c, "").split(" ")[0] for c in text if c.isalpha()]
    if not letters:
        return ""
    counts = Counter("CJK" if s in {"CJK", "HIRAGANA", "KATAKANA"} else s for s in letters)
    name, count = counts.most_common(1)[0]
    return name if count >= 0.8 * len(letters) else ""


@dataclass
class Line:
    text: str
    box: tuple   # x0, y0, x1, y1 in the sampled frame, normalized to 0..1
    confidence: float
    verified: bool = False
    uncertain: bool = False
    alternatives: tuple = ()


def union(a, b):
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def overlap(a, b):
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    common = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return common / max(1e-9, area_a + area_b - common)


def same_region(a, b):
    if overlap(a, b) >= .28:
        return True
    # A sparse recognizer may return only the last word of a detected line.
    # Drop a fully contained fragment while keeping vertically adjacent rows.
    height_a, height_b = a[3] - a[1], b[3] - b[1]
    common_height = min(a[3], b[3]) - max(a[1], b[1])
    common_width = min(a[2], b[2]) - max(a[0], b[0])
    return (common_height >= .75 * min(height_a, height_b)
            and common_height >= .43 * max(height_a, height_b)
            and common_width >= .85 * min(a[2] - a[0], b[2] - b[0]))


def word_text(words):
    parts = [words[0][0]]
    for previous, current in zip(words, words[1:]):
        gap = current[1][0] - previous[1][2]
        # Tesseract occasionally emits overlapping "word" boxes for adjacent
        # Korean syllables in one word. Preserve word spaces where the ink separates.
        contiguous = (script(previous[0]) == script(current[0]) == "HANGUL"
                      and gap <= .10 * (current[1][2] - current[1][0]) / max(1, len(current[0])))
        parts.extend(("" if contiguous else " ", current[0]))
    return "".join(parts)


def parse_words(tsv, width, height, *, sparse=True):
    groups = defaultdict(list)
    for line in tsv.splitlines()[1:]:
        fields = line.split("\t", 11)
        if len(fields) != 12 or fields[0] != "5":
            continue
        text = "".join(c for c in fields[11].strip() if not unicodedata.category(c).startswith("C") or c == "\u200c")
        if not text:
            continue
        x, y, w, h = map(int, fields[6:10])
        conf = float(fields[10])
        if conf >= 30 and w > 0 and h > 0:
            groups[tuple(fields[1:5])].append((text, (x / width, y / height,
                                                     (x + w) / width, (y + h) / height), conf))
    result = []
    for words in groups.values():
        # Ignore detached bullets, timestamps and specks at the edges of a line.
        while words and not any(c.isalpha() for c in words[0][0]):
            words.pop(0)
        while words and not any(c.isalpha() for c in words[-1][0]):
            words.pop()
        if not words:
            continue
        text = word_text(words)
        alphabet = script(text)
        if alphabet and len(words) > 1:
            words = [word for word in words if len(key(word[0])) > 1 or script(word[0]) in ("", alphabet)]
            text = word_text(words)
        letters = sum(c.isalpha() for c in text)
        if len(set(key(text))) <= 1 or not alphabet or letters < (2 if alphabet == "CJK" else 3) or text.startswith(("@", "#")) or "://" in text:
            continue
        weights = [max(1, len(key(w[0]))) for w in words]
        confidence = sum(w[2] * n for w, n in zip(words, weights)) / sum(weights)
        if confidence < (86 if len(words) == 1 else 77) or letters < 0.65 * len(text.replace(" ", "")):
            continue
        box = words[0][1]
        for word in words[1:]:
            box = union(box, word[1])
        if not sparse or box[3] - box[1] < .85:
            result.append(Line(text, box, confidence))
    return result


def paragraphs(lines):
    """Join aligned adjacent rows of the same script, keeping UI fields and columns separate."""
    result = []
    for line in sorted(lines, key=lambda item: item.box[1]):
        x, y, right, bottom = line.box
        height = bottom - y
        for i in range(len(result) - 1, -1, -1):
            prior, tail, rows = result[i]
            px, py, pr, pb = tail
            ph = pb - py
            gap = y - pb
            if (rows >= 5 or not -.43 * min(ph, height) <= gap <= .7 * ph
                    or not .6 <= height / ph <= 1.65 or script(prior.text) != script(line.text)):
                continue
            narrow, wide = sorted((pr-px, right-x))
            if narrow < .55 * wide and min(len(line.text.split()), len(prior.text.split())) == 1:
                continue  # A contact/title and a wider status below are separate fields.
            shared = max(0, min(pr, right) - max(px, x))
            if shared < .7 * min(pr - px, right - x):
                continue
            if min(abs(px - x), abs(pr - right), abs(px + pr - x - right) / 2) > .18 * max(pr - px, right - x):
                continue
            merged = Line(prior.text + " " + line.text, union(prior.box, line.box),
                          (prior.confidence * rows + line.confidence) / (rows + 1), prior.verified and line.verified,
                          prior.uncertain or line.uncertain,
                          tuple([variant + " " + line.text for variant in prior.alternatives]
                                + [prior.text + " " + variant for variant in line.alternatives])[:12])
            result[i] = merged, line.box, rows + 1
            break
        else:
            result.append((line, line.box, 1))
    return [line for line, _, _ in result]


def light_letters(gray):
    # Local contrast isolates faint white glyphs on photos and tinted app chrome.
    # A summed-area table keeps the cost independent of the averaging window.
    radius = 12
    side = 2 * radius + 1
    padded = np.pad(gray, radius, mode="edge")
    sums = np.pad(padded.cumsum(0, dtype=np.int32).cumsum(1, dtype=np.int32), ((1, 0), (1, 0)))
    local = sums[side:, side:] - sums[:-side, side:] - sums[side:, :-side] + sums[:-side, :-side]
    return np.where((gray.astype(np.int32) - 9) * side ** 2 > local, 0, 255).astype(np.uint8)


def transcribe(gray, languages, psm):
    height, width = gray.shape
    header = f"P5\n{width} {height}\n255\n".encode()
    proc = subprocess.run(["tesseract", "stdin", "stdout", "-l", languages, "--psm", str(psm),
                           "-c", "tessedit_create_tsv=1"], input=header + gray.tobytes(),
                          capture_output=True, env=dict(os.environ, OMP_THREAD_LIMIT="1"), timeout=45, check=True)
    return proc.stdout.decode()


def refine_region(gray, line, languages):
    # Sparse page layout locates text. Rectifying a small region for paragraph
    # recognition avoids dropping words at either end, especially in RTL scripts.
    h, w = gray.shape
    x0, y0, x1, y1 = line.box
    pad = max(3, round(min(w, h) * .007))
    crop = gray[max(0, int(y0*h)-pad):min(h, round(y1*h)+pad+1),
                max(0, int(x0*w)-pad):min(w, round(x1*w)+pad+1)]
    if crop.shape[0] < 85:
        crop = crop.repeat(2, axis=0).repeat(2, axis=1)
    counts = {pack for pack in languages.split("+") if pack in {"fas", "ara", "urd"}}
    if not counts:
        return line
    packs = next(pack for pack in languages.split("+") if pack in counts)
    data = transcribe(crop, packs, 6)
    words = [row.split("\t", 11) for row in data.splitlines() if row.startswith("5\t")]
    if not words:
        return line
    text = " ".join(w[11].strip() for w in words)
    text = "".join(c for c in text if not unicodedata.category(c).startswith("C") or c == "\u200c").strip()
    conf = sum(float(w[10]) for w in words) / len(words)
    if conf >= 73 and script(text) == "ARABIC" and SequenceMatcher(None, key(line.text), key(text)).ratio() >= .65:
        return Line(text, line.box, max(line.confidence, conf))
    return line


def modern_rows(results):
    trusted = []
    for result in results or ():
        text = result["text"].strip()
        confidence = result["confidence"] * 100
        if (not result.get("weak_detection") and confidence >= 85 and script(text) in {"ARABIC", "LATIN", "CJK"} and len(set(key(text))) >= 2
                and len(key(text)) >= 2 and not text.startswith(("@", "#"))):
            trusted.append(Line(text, tuple(result["box"]), confidence, verified=confidence >= 95,
                                alternatives=tuple(result.get("alternatives", ()))))
    return trusted


def reread_detected_line(gray, box, languages):
    """Retry a detector's unreadable row using the installed Tesseract scripts."""
    height, width = gray.shape
    x0, y0, x1, y1 = box
    crop = gray[max(0, int(y0*height)-2):min(height, round(y1*height)+3),
                max(0, int(x0*width)-2):min(width, round(x1*width)+3)]
    if min(crop.shape) < 10 or crop.shape[1] < .025 * width:
        return None
    if crop.shape[0] < 65:
        crop = crop.repeat(2, axis=0).repeat(2, axis=1)
    rows = parse_words(transcribe(crop, languages, 7), crop.shape[1], crop.shape[0], sparse=False)
    if len(rows) == 1 and rows[0].confidence >= 89 and len(key(rows[0].text)) >= 4:
        return Line(rows[0].text, tuple(box), rows[0].confidence)
    return None


def grayscale(image):
    if image.ndim == 2:
        return image
    # RGB from FFmpeg. Keep the color channels for the learned text detector.
    return ((77 * image[:, :, 0].astype(np.uint16) + 150 * image[:, :, 1].astype(np.uint16)
            + 29 * image[:, :, 2].astype(np.uint16)) >> 8).astype(np.uint8)


def read_frame(image, languages, reader=None, broad=True, engines=None, *, primary_only=False):
    gray = grayscale(image)
    height, width = gray.shape
    modern = reader(image) if reader is not None else None
    trusted = modern_rows(modern)
    # Keep plausible but weak visual reads separate from accepted text. A
    # reflection or decoration must never become a subtitle from one read.
    uncertain = []
    for item in modern or ():
        text = item["text"].strip()
        value = key(text)
        family = script(text)
        floor = .48 if family == "ARABIC" else .70
        if (floor <= item["confidence"] and (item["confidence"] < .85 or item.get("weak_detection")) and family in {"ARABIC", "LATIN", "CJK"}
                and len(set(value)) >= 3 and all(c.isalpha() for c in value)
                and not text.startswith(("@", "#"))):
            uncertain.append(Line(text, tuple(item["box"]), 100 * item["confidence"], uncertain=True,
                                  alternatives=tuple(item.get("alternatives", ()))))
    if modern is not None and engines is not None:
        engines.update(getattr(reader, "engines", ()) or ("PaddleOCR",))
    if primary_only and modern is not None:
        return paragraphs(trusted + uncertain)
    if not broad and modern is not None:
        # The detector sees all scripts. Run the general reader when it finds
        # text which the specialized recognizer cannot identify confidently.
        if all(not item.get("weak_detection") and float(item["confidence"]) >= .85 and script(item["text"]) in {"LATIN", "ARABIC", "CJK"}
               for item in modern):
            return paragraphs(trusted)
    lines = []
    if engines is not None:
        engines.add("Tesseract")
    for pixels in (gray, light_letters(gray)):
        lines.extend(parse_words(transcribe(pixels, languages, 11), width, height))
    chosen = []
    for candidate in sorted(lines, key=lambda item: item.confidence, reverse=True):
        old = next((line for line in chosen if overlap(candidate.box, line.box) >= .38), None)
        if not old:
            chosen.append(candidate)
        elif script(old.text) == script(candidate.text):
            old.box = union(old.box, candidate.box)
    for item in modern or ():
        box = item["box"]
        if any(same_region(line.box, box) for line in trusted):
            continue
        # A full image may lose words behind a moving background. When the
        # sparse reader sees a different script, retry the complete detected row.
        nearby = [line for line in chosen if same_region(line.box, box)]
        families = {script(line.text) for line in nearby}
        extra = {"CYRILLIC": ("rus", "ukr"), "HANGUL": ("kor",)}
        allowed = [name for name in languages.split("+") if any(name in extra.get(f, ()) for f in families)]
        if allowed:
            reread = reread_detected_line(gray, box, "+".join(allowed))
            if reread and script(reread.text) in families:
                trusted.append(reread)
    remaining = [line for line in chosen if not any(same_region(line.box, best.box) for best in trusted)]
    # Keep the high-accuracy recognizer's rows intact. Sparse text from a
    # separate detector must not become part of that same translated sentence.
    blocks = paragraphs(remaining)
    extra_rows = [row for row in uncertain
                  if not any(same_region(row.box, line.box) for line in trusted + remaining)]
    return paragraphs(trusted + extra_rows) + [refine_region(gray, line, languages) if script(line.text) == "ARABIC" else line
                                  for line in blocks]


def fingerprint(frame, box=(0., 0., 1., 1.)):
    height, width = frame.shape
    left, top, right, bottom = box
    xs = np.linspace(left * (width-1), right * (width-1), 40).astype(int).clip(0, width-1)
    ys = np.linspace(top * (height-1), bottom * (height-1), 20).astype(int).clip(0, height-1)
    return frame[np.ix_(ys, xs)].astype(np.float32)


def similarity(a, b):
    if np.abs(a-b).mean() < 5:
        return 1.
    a = a - a.mean()
    b = b - b.mean()
    return float(np.sum(a*b) / max(1., np.linalg.norm(a)*np.linalg.norm(b)))


class Track:
    def __init__(self, line, time, frame):
        self.latest = line
        self.sections = []
        self.readings = Counter()
        self.anchors = 0
        self.strong_reads = Counter()
        self.verified_reads = Counter()
        self.confidence_sum = 0.
        self.hypotheses = Counter()
        self.word_alternatives = Counter()
        self.word_spellings = {}
        self.samples = 0
        self.update(line, frame, time)

    def matches(self, line, frame=None):
        extent = overlap(self.latest.box, line.box)
        if extent < .42:
            return False
        prior = key(self.latest.text)
        current = key(line.text)
        match = SequenceMatcher(None, prior, current, autojunk=False)
        if match.ratio() >= .68:
            return True
        # Stylized lettering can lose different glyphs as the pen or camera
        # moves. Compare the actual patch before combining partial readings.
        return (self.latest.uncertain and line.uncertain and frame is not None and extent >= .68
                and match.find_longest_match().size >= 2 and match.ratio() >= .40
                and script(prior) == script(current)
                and similarity(self.stamp, fingerprint(frame, line.box)) >= .52)

    def update(self, line, frame, time):
        box = line.box
        self.stamp = fingerprint(frame, box)
        # The perimeter usually contains the writing surface rather than the ink.
        border = np.concatenate((self.stamp[0], self.stamp[-1], self.stamp[:,0], self.stamp[:,-1]))
        lightness = round(float(np.median(border)))
        if not self.sections or overlap(self.sections[-1][1], box) < .76:
            self.sections.append([time, box, box, lightness])
        else:
            self.sections[-1][2] = union(self.sections[-1][2], box)

        self.latest = line
        self.samples += 1
        self.anchors += 1
        if line.verified:
            self.verified_reads[line.text] += line.confidence
        if not line.uncertain:
            self.strong_reads[line.text] += line.confidence
        self.confidence_sum += line.confidence
        self.readings[line.text] += line.confidence
        if len(line.text.split()) >= 2:
            # Count each independent frame once regardless of how many
            # photometric variants reproduced the same reading.
            frame_spellings = {}
            for variant in (line.text, *line.alternatives):
                normalized = key(variant)
                frame_spellings[normalized] = 1
                if len(variant.split()) > len(self.word_spellings.get(normalized, "").split()):
                    self.word_spellings[normalized] = variant
            self.word_alternatives.update(frame_spellings)
        if line.uncertain:
            self.hypotheses[tuple(dict.fromkeys((line.text, *line.alternatives)))] += line.confidence

    def finish(self, end, candidates=None):
        # Repeated evidence is required before a region of the user's footage is obscured.
        # Sparse CJK shapes and tiny clock/UI glyphs are especially prone to
        # confident, single-frame misreads against a textured background.
        if not self.strong_reads:
            if self.anchors >= 2 and candidates is not None:
                candidates.append((self, end))
            return []
        if self.anchors < 2 and not self.latest.verified:
            value = key(self.latest.text)
            short_cjk = script(value) == "CJK" and len(value) <= 4
            tiny = self.latest.box[3] - self.latest.box[1] < .024
            if short_cjk or (tiny and self.latest.confidence < 97):
                return []
        threshold = 82 if self.samples >= 3 and len(self.latest.text.split()) >= 2 else 88
        if not self.latest.verified and (self.samples < 2 or (self.anchors < 2 and self.latest.confidence < threshold)):
            return []
        return self.regions(self.spelling(), end)

    def spelling(self):
        if self.verified_reads:
            return self.verified_reads.most_common(1)[0][0]
        if len(self.latest.text.split()) < 2:
            return (self.strong_reads or self.readings).most_common(1)[0][0]
        # Different glyphs of a sentence can be briefly occluded. Choose an
        # observed sentence with broad agreement across the independent reads.
        weighted = [(text, key(text), weight) for text, weight in self.readings.most_common(12)]
        def agreement(candidate):
            _, value, direct = candidate
            shared = sum(weight * SequenceMatcher(None, value, other, autojunk=False).ratio()
                         for _, other, weight in weighted)
            return shared, direct
        return max(weighted, key=agreement)[0]

    def regions(self, text, end):
        extras = {}
        if len(text.split()) >= 2 and self.word_alternatives:
            value = key(text)
            variants = [self.word_spellings[spelling] for spelling, count in self.word_alternatives.most_common()
                        if count >= 2 and spelling != value
                        and SequenceMatcher(None, value, spelling, autojunk=False).ratio() >= .78]
            if variants:
                extras["alternatives"] = variants[:8]
        return [{"text": text, "start": section[0], "end": stop, "box": list(section[2]),
                 "background_luma": section[3], **extras}
                for section, stop in zip(self.sections, [s[0] for s in self.sections[1:]] + [end])]


def resolve_uncertain(tracks, candidates):
    # A repeated sentence can supply a reliable spelling for the same word
    # seen earlier in moving or decorative type. Index observed words by
    # four-letter runs so long videos don't compare every word to every other.
    vocab = defaultdict(set)

    window = 30

    def remember(text, start, end):
        for word in re.findall(r"[^\W\d_]+", text, flags=re.UNICODE):
            word = unicodedata.normalize("NFKC", word)
            value = key(word)
            if 4 <= len(value) <= 40:
                for i in range(len(value) - 3):
                    for bucket in range(int(start // window), int(end // window) + 1):
                        vocab[bucket, value[i:i + 4]].add((word, start, end))

    for item in tracks:
        remember(item["text"], item["start"], item["end"])
    singles = []
    for track, end in candidates:
        text = track.spelling()
        if len(text.split()) < 2:
            singles.append((track, end))
            continue
        repeated = sum(weight for spelling, weight in track.readings.items() if key(spelling) == key(text)) > 100
        if (track.anchors >= 3 and repeated and len(key(text)) >= 7
                and track.confidence_sum / track.anchors >= 79):
            tracks.extend(track.regions(text, end))
            remember(text, track.sections[0][0], end)

    for track, end in singles:
        votes = Counter()
        start = track.sections[0][0]
        buckets = range(int(start // window) - 1, int(end // window) + 2)
        original = {key(text) for text in track.readings}
        for readings, weight in track.hypotheses.items():
            support = {}
            for reading in readings:
                value = key(reading)
                if len(value) < 4:
                    continue
                possible = {word for i in range(len(value) - 3) for bucket in buckets
                            for word, t0, t1 in vocab.get((bucket, value[i:i + 4]), ())
                            if t1 >= start - window and t0 <= end + window}
                for word in possible:
                    target = key(word)
                    # Consistent local spelling can only confirm itself.
                    if len(original) == 1 and target not in original:
                        continue
                    if len(target) >= len(value) - 1 and abs(len(target) - len(value)) <= 2:
                        fit = SequenceMatcher(None, value, target, autojunk=False).ratio()
                        if fit >= .72:
                            support[word] = max(fit, support.get(word, 0))
            for word, fit in support.items():
                votes[word] += fit * weight
        ranked = votes.most_common(2)
        if ranked and ranked[0][1] > 100 and (len(ranked) == 1 or ranked[0][1] > 1.35 * ranked[1][1]):
            tracks.extend(track.regions(ranked[0][0], end))


def sampled_frames(media, remote, *, color=False):
    from render import source_input_options, source_time_limit

    filt = (f"setpts=PTS-STARTPTS,fps={SAMPLE_FPS}:round=up:eof_action=pass,"
            "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *source_input_options(remote),
               "-i", os.fspath(media), "-map", "0:v:0", *source_time_limit(), "-vf", filt, "-c:v", "ppm" if color else "pgm",
               "-pix_fmt", "rgb24" if color else "gray", "-f", "image2pipe", "pipe:1"]
    with subprocess.Popen(command, stdout=subprocess.PIPE) as proc:
        try:
            frame_index = 0
            while magic := proc.stdout.readline(8):
                if magic != (b"P6\n" if color else b"P5\n"):
                    raise ValueError("Invalid video frame from FFmpeg.")
                width, height = map(int, proc.stdout.readline(40).split())
                if not (0 < width <= 1280 and 0 < height <= 1280) or proc.stdout.readline(8) != b"255\n":
                    raise ValueError("Invalid video frame dimensions.")
                size = width * height * (3 if color else 1)
                data = proc.stdout.read(size)
                if len(data) != size:
                    raise ValueError("Incomplete video frame from FFmpeg.")
                shape = (height, width, 3) if color else (height, width)
                yield frame_index / SAMPLE_FPS, np.frombuffer(data, dtype=np.uint8).reshape(shape)
                frame_index += 1
            if proc.wait():
                raise RuntimeError("Could not decode frames for on-screen translation.")
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.stdout.close()


def detect_tracks(media, folder, source_language=None, *, remote=False):
    """Return timed text regions in FFmpeg display orientation, with coordinates in [0,1]."""
    languages = ocr_languages(source_language)
    from paddle_screen import paddle_reader
    engines = set()
    with paddle_reader(source_language) as reader:
        tracks = track_frames(sampled_frames(media, remote, color=reader is not None), languages, reader, engines)
    Path(folder, "screen-ocr-engine.txt").write_text(" + ".join(sorted(engines)) or "No video frames", encoding="utf-8")
    return tracks


def track_frames(frames, languages, reader=None, engines=None):
    active, result, candidates = [], [], []
    previous = None
    last_read = last_broad = last_full = -2.
    end = 0.
    for time, image in frames:
        frame = grayscale(image)
        view = fingerprint(frame)
        cut = previous is None or similarity(previous, view) < .52
        frozen = previous is not None and np.abs(previous - view).mean() < .65
        previous = view
        end = time + 1 / SAMPLE_FPS
        if cut:
            for track in active:
                result.extend(track.finish(time, candidates))
            active = []
        scores = {track: similarity(track.stamp, fingerprint(frame, track.latest.box)) for track in active}
        lost = any(score < .59 for score in scores.values())
        uncertain = [track.latest for track in active if track.latest.uncertain or track.latest.alternatives]
        read_interval = (1 / SAMPLE_FPS if any(len(line.text.split()) >= 2 for line in uncertain)
                         else .25 if uncertain else .5)
        if not cut and not lost and (time - last_read < read_interval or frozen):
            for track in active:
                track.samples += 1
            continue
        broad = cut or time - last_broad >= 2.
        if broad:
            last_broad = time
        # Sample ambiguous sentences more densely. Independent OCR at the
        # ordinary cadence keeps scripts which need Tesseract in the scan.
        primary_only = (reader is not None and not broad and bool(uncertain) and time - last_full < .5
                        and all(script(track.latest.text) in {"ARABIC", "CJK", "LATIN"} for track in active))
        if not primary_only:
            last_full = time
        lines = read_frame(image, languages, reader, broad, engines, primary_only=primary_only)
        last_read = time
        new = []
        for line in lines:
            matched = next((track for track in active if track.matches(line, frame)), None)
            if matched:
                active.remove(matched)
                matched.update(line, frame, time)
                new.append(matched)
            else:
                new.append(Track(line, time, frame))
        for track in active:
            if scores[track] > .86:
                track.samples += 1
                new.append(track)
            else:
                result.extend(track.finish(time, candidates))
        active = new
    for track in active:
        result.extend(track.finish(end, candidates))
    resolve_uncertain(result, candidates)
    ordered = sorted(result, key=lambda item: (item["start"], -item["end"], -len(item["text"])))
    visible, active = [], []
    for item in ordered:
        active = [other for other in active if other["end"] > item["start"]]
        # A sparse reader can report one row again while a complete tracked
        # paragraph already covers the same time and position.
        value = key(item["text"])
        box = item["box"]
        area = (box[2] - box[0]) * (box[3] - box[1])
        def covers(other):
            if other["end"] < item["end"] or len(other["text"].split()) <= len(item["text"].split()):
                return False
            left, top, right, bottom = other["box"]
            inside = (max(0, min(right, box[2]) - max(left, box[0]))
                      * max(0, min(bottom, box[3]) - max(top, box[1])))
            return inside >= .85 * area and value in key(other["text"])
        if not any(covers(other) for other in active):
            visible.append(item)
            active.append(item)
    return visible
