"""Build timestamped masks and shaped labels for foreign text in the video frame."""
import json
import math
import re
import subprocess
import unicodedata


def display_size(media, input_options):
    probe = subprocess.check_output([
        "ffprobe", "-v", "error", *input_options, "-select_streams", "v:0",
        "-show_entries", "stream=width,height:stream_side_data=rotation", "-of", "json", str(media)
    ])
    stream = json.loads(probe)["streams"][0]
    width, height = stream["width"], stream["height"]
    rotation = next((side["rotation"] for side in stream.get("side_data_list", []) if "rotation" in side), 0)
    if round(rotation / 90) % 2:
        return height, width
    return width, height


def timestamp(seconds):
    ticks = round(seconds * 100)
    hours, ticks = divmod(ticks, 360000)
    minutes, ticks = divmod(ticks, 6000)
    return f"{hours}:{minutes:02}:{ticks // 100:02}.{ticks % 100:02}"


def ass_header(width, height, target):
    family = {"ja": "Noto Sans CJK JP", "ko": "Noto Sans CJK KR", "zh": "Noto Sans CJK SC"}.get(target, "Noto Sans")
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Label,{family},24,&H00FFFFFF,&H00FFFFFF,&H00202020,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def width_units(text):
    # Approximate advance in em, allowing font fallback and shaping.
    def advance(char):
        if unicodedata.combining(char):
            return 0
        if unicodedata.east_asian_width(char) in ("W", "F"):
            return 1.0
        if char in " il.,:;'!|":
            return .33
        return .78 if char.isupper() or char in "mw" else .56
    return sum(map(advance, text))


def wrap_label(text, width):
    # Break space-separated words and scripts without spaces such as Japanese.
    tokens = re.findall(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]|[^\s\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]+|\s+", text)
    rows = [""]
    for token in tokens:
        if rows[-1].strip() and width_units(rows[-1] + token) > width:
            rows[-1] = rows[-1].rstrip()
            rows.append("")
        for char in token:
            if not rows[-1] and char.isspace():
                continue
            if rows[-1] and width_units(rows[-1] + char) > width:
                rows.append("")
            rows[-1] += char
    return [row.strip() for row in rows if row.strip()]


def relative_span(center, span, limit):
    start = max(0, min(round(center - span / 2), limit - span))
    return start, start + span


def label_columns(tracks, width):
    """Let labels expand into gaps, without covering a simultaneous neighboring column."""
    columns = [[0, width] for _ in tracks]
    active = []
    for i in sorted(range(len(tracks)), key=lambda n: tracks[n]["start"]):
        track = tracks[i]
        left, top, right, bottom = track["box"]
        active = [j for j in active if tracks[j]["end"] > track["start"]]
        for j in active:
            l2, t2, r2, b2 = tracks[j]["box"]
            overlap = min(bottom, b2) - max(top, t2)
            if overlap <= 0 or overlap < .4 * min(bottom - top, b2 - t2):
                continue
            if right <= l2 or r2 <= left:
                lhs, rhs, midpoint = (i, j, (right + l2)/2) if right <= l2 else (j, i, (r2 + left)/2)
                boundary = round(midpoint * width)
                columns[lhs][1] = min(columns[lhs][1], boundary)
                columns[rhs][0] = max(columns[rhs][0], boundary)
        active.append(i)
    return columns


def layout(text, box, width, height, column):
    x1, y1, x2, y2 = box
    source_w, source_h = max(1, x2-x1), max(1, y2-y1)
    margin = max(2, min(6, round(source_h * .07)))
    ideal = max(8, min(round(source_h * .84), max(14, round(min(width, height) * .075))))
    col_left, col_right = column
    # Short controls should stay inside their original column even when they have no OCR neighbor.
    short = len(text.split()) == 1 and width_units(text) <= 7
    room = source_w if short else max(source_w, min(round(width * .62), round(source_w * 1.35)))
    area_w = min(col_right - col_left, room + margin * 2)
    size = ideal
    while True:
        rows = wrap_label(text, max(1.05, (area_w - margin * 2) / size))
        fits = len(rows) * size * 1.08 <= max(12, source_h + margin * 2)
        if fits or size <= 8:
            break
        size -= 1
    # The source blur and translated label have independent extents. In particular,
    # replacing a long source word with a short one must not draw a full-width plate.
    ink_w = math.ceil(max(width_units(row) for row in rows) * size * 1.11)
    area_w = min(area_w, ink_w + margin * 2)
    area_h = min(height, math.ceil(len(rows) * size * 1.12) + margin * 2)
    left, right = relative_span((x1 + x2) / 2 - col_left, area_w, col_right - col_left)
    top, bottom = relative_span((y1 + y2) / 2, area_h, height)
    return (left + col_left, top, right + col_left, bottom), size, rows


def rectangle(box, color, alpha):
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    return rf"{{\an7\pos({x1},{y1})\bord0\shad0\1c&H{color}&\1a&H{alpha}&\p1}}m 0 0 l {w} 0 {w} {h} 0 {h} 0 0"


def screen_filter(tracks, folder, width, height, target):
    """Return an FFmpeg graph using only generated, relative filenames and trusted filter syntax."""
    header = ass_header(width, height, target)
    masks, labels = [], []
    tallest = 0
    for track, column in zip(tracks, label_columns(tracks, width)):
        # No source/translated text goes into a filter expression or an ASS override block.
        text = " ".join((track.get("translation") or "").split())
        text = text.translate(str.maketrans({"\\": "＼", "{": "｛", "}": "｝"}))
        if not text or text == " ".join(track.get("text", "").split()):
            continue
        left, top, right, bottom = track["box"]
        start, end = track["start"], track["end"]
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1 and 0 <= start < end < float("inf")):
            continue
        start, end = timestamp(start), timestamp(end)
        if start == end:
            continue
        padding = max(2, min(8, round((bottom - top) * height * .08)))
        glyph_box = (int(left * width), int(top * height), math.ceil(right * width), math.ceil(bottom * height))
        box = (max(0, int(left * width) - padding), max(0, int(top * height) - padding),
               min(width, math.ceil(right * width) + padding), min(height, math.ceil(bottom * height) + padding))
        tallest = max(tallest, box[3] - box[1])
        interval = f"{start},{end},Label,,0,0,0,,"
        masks.append("Dialogue: 0," + interval + rectangle(box, "FFFFFF", "00"))
        area, size, rows = layout(text, glyph_box, width, height, column)
        x1, y1, x2, y2 = area
        luma = track.get("background_luma")
        light = luma is not None and luma >= 155
        ink, outline = ("171717", "FFFFFF") if light else ("FFFFFF", "141414")
        # Blurring removes the writing. A light tint and subtle outline give the replacement
        # enough contrast while preserving the original surface, including colored chat headers.
        # Bright paper or a dark screen already supplies a contrasting surface. Drawing
        # a plate there can spill across the boundary of a small UI card.
        if luma is None or 50 < luma < 185:
            plate_alpha = "DB" if luma is not None else "28"
            labels.append("Dialogue: 0," + interval + rectangle(area, outline, plate_alpha))
        border = max(.45, min(1.3, size * .024))
        placement = (rf"{{\an5\pos({(x1 + x2)//2},{(y1 + y2)//2})\fs{size}\1c&H{ink}&"
                     rf"\bord{border:.2f}\3c&H{outline}&\3a&H70&\clip({x1},{y1},{x2},{y2})}}")
        labels.append("Dialogue: 1," + interval + placement + r"\N".join(rows))
    if not masks:
        return None
    (folder / "screen-mask.ass").write_text(header + "\n".join(masks) + "\n", encoding="utf-8")
    (folder / "screen-labels.ass").write_text(header + "\n".join(labels) + "\n", encoding="utf-8")
    # Reuse the source as the mask's clock. A synthetic constant-frame-rate mask can shift labels on VFR video.
    # A local median removes high-contrast strokes without smearing a nearby face or
    # the edge of a white chat card into its paper. Downsampling keeps the repair cheap.
    radius = max(2, min(9, round(tallest / 28)))
    sigma = max(1.4, min(5., tallest * .024))
    return (f"[0:v:0]split=3[base][blur][matte];[blur]scale={max(2,width//4)}:{max(2,height//4)}:flags=area,"
            f"median=radius={radius},gblur=sigma={sigma:.2f},scale={width}:{height}:flags=bilinear[soft];"
            "[matte]format=gray,lut=y=0,ass=screen-mask.ass,format=gray[mask];"
            "[soft][mask]alphamerge[cover];[base][cover]overlay,ass=screen-labels.ass,"
            "pad=ceil(iw/2)*2:ceil(ih/2)*2[screen]")
