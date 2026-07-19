"""Catch the BID button's white outline and measure it.

    python catch_cue.py

Run it, then play an auction round so the outline lights up at least once. It watches
the BID box for 30 seconds, remembers the single best frame it saw, saves that frame as
cue_peak.png, and reports how long a white line it found at a range of brightness
thresholds. That tells us three things at once:

  * whether the outline is inside the box at all (a long run appears)
  * how white it really is (which thresholds still see it)
  * what to set "White line (px)" to

Press Ctrl+C to stop early.
"""

import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import json
import os
import time

import mss
import numpy as np
from PIL import Image

from engine import bid_box

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(HERE, "config.json")))
obox = bid_box(cfg)
THRESHOLDS = (120, 150, 170, 190, 200, 220, 235, 250)
SECONDS = int(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 30


def longest_run(lo, spread, th, sp=25):
    w = (lo > th) & (spread < sp)
    if not w.any():
        return 0
    m = np.pad(w, ((0, 0), (1, 1)), constant_values=False).astype(np.int8)
    d = np.diff(m, axis=1)
    s = np.flatnonzero(d.ravel() == 1)
    e = np.flatnonzero(d.ravel() == -1)
    return int((e - s).max()) if s.size else 0


print(f"watching {obox['width']}x{obox['height']} at ({obox['left']},{obox['top']})")
print(f"for {SECONDS}s -- play a round so the BID outline lights up at least once\n")

best = {th: 0 for th in THRESHOLDS}
peak_img, peak = None, -1
t0 = time.perf_counter()
try:
    with mss.mss() as sct:
        shown = 0.0
        while time.perf_counter() - t0 < SECONDS:
            img = np.asarray(sct.grab(obox))[:, :, :3]
            a = img.astype(np.int16)
            lo = a.min(axis=2)
            spread = a.max(axis=2) - lo
            runs = {th: longest_run(lo, spread, th) for th in THRESHOLDS}
            for th in THRESHOLDS:
                best[th] = max(best[th], runs[th])
            if runs[200] > peak:
                peak, peak_img = runs[200], img.copy()
            now = time.perf_counter()
            if now - shown >= 0.5:
                shown = now
                print(f"  {SECONDS - (now - t0):4.0f}s left   now={runs[200]:4d}px"
                      f"   best so far={best[200]:4d}px")
except KeyboardInterrupt:
    pass

print("\n\nlongest white line seen, by brightness threshold:")
for th in THRESHOLDS:
    bar = "#" * min(40, best[th] // 15)
    print(f"   white_min={th:3d} -> {best[th]:4d} px  {bar}")

if peak_img is not None:
    Image.fromarray(peak_img[:, :, ::-1]).save(os.path.join(HERE, "cue_peak.png"))
    print("\nbest frame saved to cue_peak.png -- open it and check the outline is in shot")

top = best[235]
print()
if top >= 150:
    print(f"GOOD: the outline reads {top}px at the default threshold. Outline mode should")
    print("      fire. Set 'White line (px)' to about half that.")
elif best[150] >= 150:
    print(f"The outline IS there but is not pure white -- it only reads {best[150]}px at")
    print(f"white_min=150 and {top}px at 235. Add this to config.json:")
    print(f'      "white_min": 140')
else:
    print("No long white line found at ANY brightness. The outline is outside the box.")
    print("Open 'Show / edit regions' during a round and check the blue box covers the")
    print("whole button, then run this again.")
