"""Estimate dialogue and the stereo music/effects bed at the source timeline's 48 kHz."""
from pathlib import Path
import sys
from server.gpu import stage

import numpy as np
import soundfile as sf
from bandit_infer import BanditSession


def main():
    prepare = sys.argv[1] == "--prepare"
    with BanditSession("v2-multi", family="v2", device="cpu" if prepare else "cuda") as model:
        if prepare:
            return
        folder = Path(sys.argv[1])
        with (sf.SoundFile(folder / "source.wav") as src,
              sf.SoundFile(folder / "speech.wav", "w", src.samplerate, 2, "FLOAT", format="RF64") as speech,
              sf.SoundFile(folder / "background.wav", "w", src.samplerate, 2, "FLOAT", format="RF64") as background):
            # Give each block surrounding context, then keep only its central region.
            rate = src.samplerate
            for start in range(0, len(src), 30 * rate):
                stop = min(len(src), start + 30 * rate)
                left = max(0, start - 4 * rate)
                src.seek(left)
                block = src.read(min(len(src), stop + 4 * rate) - left, dtype="float32", always_2d=True)
                if len(block) < rate:
                    block = np.pad(block, ((0, rate - len(block)), (0, 0)))
                stems = model.infer(block.T, sample_rate=rate)
                keep = slice(start - left, stop - left)
                speech.write(stems["speech"][:, keep].T)
                background.write((stems["music"][:, keep] + stems["effects"][:, keep]).T)


if __name__ == "__main__":
    if sys.argv[1] == "--prepare":
        main()
    else:
        with stage("exclusive"):
            print("GPU stage: Separating audio", flush=True)
            main()
