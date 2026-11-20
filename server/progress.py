"""Estimate running jobs from completed renders near the same source duration."""
import json
import math
from statistics import median

# A stage boundary indicates completed work. Synthesis reports actual completed lines.
STEPS = {"Starting": .01, "Downloading": .02, "Separating audio": .07, "Transcribing": .20,
         "Cleaning background voices": .30, "Identifying voices": .36, "Translating": .43,
         "Voicing": .58, "Mixing": .90, "Encoding": .94, "Saving": .98}
PREFIX = "REELDUB_PROGRESS "


def report(**values):
    print(PREFIX + json.dumps(values), flush=True)


def nearby_runtime(rows, seconds, target):
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    samples = []
    for row in rows:
        length, start, finish = row.get("media_seconds"), row.get("started"), row.get("finished")
        if not length or not start or not finish or row.get("attempt", 1) != 1:
            continue
        runtime = (finish-start).total_seconds()
        if runtime >= 5 and 0.4 <= length / seconds <= 2.5:
            samples.append((abs(math.log(length / seconds)), row.get("target_language") or "en", runtime))
    if len(samples) < 3:
        return None
    same_language = [sample for sample in samples if sample[1] == target]
    closest = sorted(same_language if len(same_language) >= 3 else samples)[:7]
    return median(x[2] for x in closest)


def line_progress(event):
    completed, total = event.get("completed"), event.get("total")
    if type(total) is int and type(completed) is int and 0 <= completed <= total and total > 0:
        return STEPS["Voicing"] + (STEPS["Mixing"] - STEPS["Voicing"]) * completed / total
