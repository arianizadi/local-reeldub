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


def union(a, b):
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def overlap(a, b):
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    common = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return common / max(1e-9, area_a + area_b - common)


def parse_words(tsv, width, height):
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
        text = " ".join(item[0] for item in words)
        alphabet = script(text)
        if alphabet and len(words) > 1:
            words = [word for word in words if len(key(word[0])) > 1 or script(word[0]) in ("", alphabet)]
            text = " ".join(item[0] for item in words)
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
        if box[3] - box[1] < .85:
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
                          (prior.confidence * rows + line.confidence) / (rows + 1), prior.verified and line.verified)
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
        if (confidence >= 85 and script(text) in {"ARABIC", "LATIN"} and len(set(key(text))) >= 2
                and len(key(text)) >= 2 and not text.startswith(("@", "#"))):
            trusted.append(Line(text, tuple(result["box"]), confidence, verified=confidence >= 95))
    return trusted


def read_frame(gray, languages, reader=None, broad=True, engines=None):
    height, width = gray.shape
    modern = reader(gray) if reader is not None else None
    trusted = modern_rows(modern)
    if modern is not None and engines is not None:
        engines.add("PP-OCRv5 Arabic")
    if not broad and modern is not None:
        # The detector sees all scripts. Run the general reader when it finds
        # text which the specialized recognizer cannot identify confidently.
        if all(float(item["confidence"]) >= .85 and script(item["text"]) in {"LATIN", "ARABIC", ""}
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
    remaining = [line for line in chosen if not any(overlap(line.box, best.box) >= .28 for best in trusted)]
    # Keep the high-accuracy recognizer's rows intact. Sparse text from a
    # separate detector must not become part of that same translated sentence.
    blocks = paragraphs(remaining)
    return paragraphs(trusted) + [refine_region(gray, line, languages) if script(line.text) == "ARABIC" else line
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
        self.samples = 0
        self.update(line, frame, time)

    def matches(self, line):
        return (overlap(self.latest.box, line.box) >= .42
                and SequenceMatcher(None, key(self.latest.text), key(line.text), autojunk=False).ratio() >= .68)

    def update(self, line, frame, time):
        self.stamp = fingerprint(frame, line.box)
        # The perimeter usually contains the writing surface rather than the ink.
        border = np.concatenate((self.stamp[0], self.stamp[-1], self.stamp[:,0], self.stamp[:,-1]))
        lightness = round(float(np.median(border)))
        if not self.sections or overlap(self.sections[-1][1], line.box) < .76:
            self.sections.append([time, line.box, line.box, lightness])
        else:
            self.sections[-1][2] = union(self.sections[-1][2], line.box)
        self.latest = line
        self.samples += 1
        self.anchors += 1
        self.readings[line.text] += line.confidence

    def finish(self, end):
        # Repeated evidence is required before a region of the user's footage is obscured.
        # Sparse CJK shapes and tiny clock/UI glyphs are especially prone to
        # confident, single-frame misreads against a textured background.
        if self.anchors < 2 and not self.latest.verified:
            value = key(self.latest.text)
            short_cjk = script(value) == "CJK" and len(value) <= 4
            tiny = self.latest.box[3] - self.latest.box[1] < .024
            if short_cjk or (tiny and self.latest.confidence < 97):
                return []
        threshold = 82 if self.samples >= 3 and len(self.latest.text.split()) >= 2 else 88
        if not self.latest.verified and (self.samples < 2 or (self.anchors < 2 and self.latest.confidence < threshold)):
            return []
        text = self.readings.most_common(1)[0][0]
        return [{"text": text, "start": section[0], "end": stop, "box": list(section[2]),
                 "background_luma": section[3]}
                for section, stop in zip(self.sections, [s[0] for s in self.sections[1:]] + [end])]


def sampled_frames(media, remote):
    from render import source_input_options, source_time_limit

    filt = (f"setpts=PTS-STARTPTS,fps={SAMPLE_FPS}:round=up:eof_action=pass,"
            "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *source_input_options(remote),
               "-i", os.fspath(media), "-map", "0:v:0", *source_time_limit(), "-vf", filt, "-c:v", "pgm",
               "-pix_fmt", "gray", "-f", "image2pipe", "pipe:1"]
    with subprocess.Popen(command, stdout=subprocess.PIPE) as proc:
        try:
            frame_index = 0
            while magic := proc.stdout.readline(8):
                if magic != b"P5\n":
                    raise ValueError("Invalid video frame from FFmpeg.")
                width, height = map(int, proc.stdout.readline(40).split())
                if not (0 < width <= 1280 and 0 < height <= 1280) or proc.stdout.readline(8) != b"255\n":
                    raise ValueError("Invalid video frame dimensions.")
                data = proc.stdout.read(width * height)
                if len(data) != width * height:
                    raise ValueError("Incomplete video frame from FFmpeg.")
                yield frame_index / SAMPLE_FPS, np.frombuffer(data, dtype=np.uint8).reshape(height, width)
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
        tracks = track_frames(sampled_frames(media, remote), languages, reader, engines)
    Path(folder, "screen-ocr-engine.txt").write_text(" + ".join(sorted(engines)) or "No video frames", encoding="utf-8")
    return tracks


def track_frames(frames, languages, reader=None, engines=None):
    active, result = [], []
    previous = None
    last_read = last_broad = -2.
    end = 0.
    for time, frame in frames:
        view = fingerprint(frame)
        cut = previous is None or similarity(previous, view) < .52
        frozen = previous is not None and np.abs(previous - view).mean() < .65
        previous = view
        end = time + 1 / SAMPLE_FPS
        if cut:
            for track in active:
                result.extend(track.finish(time))
            active = []
        scores = {track: similarity(track.stamp, fingerprint(frame, track.latest.box)) for track in active}
        lost = any(score < .59 for score in scores.values())
        if not cut and not lost and (time - last_read < .5 or frozen):
            for track in active:
                track.samples += 1
            continue
        broad = cut or time - last_broad >= 2.
        if broad:
            last_broad = time
        lines = read_frame(frame, languages, reader, broad, engines)
        last_read = time
        new = []
        for line in lines:
            matched = next((track for track in active if track.matches(line)), None)
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
                result.extend(track.finish(time))
        active = new
    for track in active:
        result.extend(track.finish(end))
    return sorted(result, key=lambda track: track["start"])
