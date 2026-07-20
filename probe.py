"""Diagnostic: print what the bot actually sees in your configured region.

Run it with the auction bar on screen and the marker moving:

    python probe.py

The number that matters most is `brightest`: the brightest pixel in the strip. The
marker should be near-pure-white (250+). If `brightest` stays well under
WHITE_MIN_BRIGHT (235) while the line is visibly there, the marker isn't as white as
assumed -- lower "white_min" in config.json to about 15 below that reading.

`cands` is how many full-height white columns were found. Healthy output is a steady
1 with x sweeping across the bar. A 0 while the line is on screen means the detection
thresholds are wrong; a number that jumps around means specks are getting through.
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

import detect
from engine import bid_box, slice_region

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(HERE, "config.json")))
detect.WHITE_MIN_BRIGHT = cfg.get("white_min", detect.WHITE_MIN_BRIGHT)
detect.MARKER_MIN_FILL = cfg.get("min_fill", detect.MARKER_MIN_FILL)

region = slice_region(cfg["track"])
obox = bid_box(cfg)
print(f"strip   {region['width']}x{region['height']} at ({region['left']},{region['top']})")
print(f"outline {obox['width']}x{obox['height']} at ({obox['left']},{obox['top']})")
print(f"white_min={detect.WHITE_MIN_BRIGHT}  min_fill={detect.MARKER_MIN_FILL}")
print("watching for 8s -- have an auction running\n")
print("white_line = longest white horizontal run in the BID box, in pixels. Should sit")
print("low normally and jump to roughly the button's width when the cue lights.\n")

with mss.mss() as sct:
    t0 = time.perf_counter()
    last = 0.0
    while time.perf_counter() - t0 < 8:
        img = np.asarray(sct.grab(region))[:, :, :3]
        a = img.astype(np.int16)
        lo = a.min(axis=2)
        cands = detect.find_markers(img)
        green = detect.find_zone(img)
        run = detect.outline_run(np.asarray(sct.grab(obox))[:, :, :3])

        now = time.perf_counter()
        if now - last < 0.25:
            continue
        last = now
        desc = "  ".join(f"x={c[0]:6.1f} w={c[1]:2d} fill={c[2]:.2f}" for c in cands[:3])
        g = f"{green[0]}-{green[1]} c={green[2]:.0f}" if green else "NONE"
        print(f"white_line={run:3d}px  brightest={int(lo.max()):3d}  cands={len(cands)}  "
              f"{desc:44s}  green={g}")
