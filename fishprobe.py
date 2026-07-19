"""Watch the saved fish_box while you cast and report what detection actually sees.

Run it, alt-tab to Roblox, cast, and let it watch for 20 seconds. It prints one line
per frame and saves two images:

    fishprobe.png       exactly the pixels the bot feeds to find_grey/find_gold
    fishprobe_full.png  the whole screen, for comparison

Between them those settle "wrong box" against "wrong colours" without guessing. The
probe deliberately re-tests every suspicion at once, including channel order: mss hands
back BGRA, while fishing.py's constants were measured off RGB video, and find_gold()
indexes channels by position where find_grey() only uses per-pixel min/max (which is
order-agnostic, and therefore immune). If the swapped column finds gold and the plain
one never does, that is the whole bug.

    python fishprobe.py
"""

import json
import os
import sys
import time

import mss
import numpy as np
import win32api
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fishing

HERE = os.path.dirname(os.path.abspath(__file__))
SECONDS = 20.0
FPS = 10.0


def load_fish_box():
    with open(os.path.join(HERE, "config.json")) as f:
        store = json.load(f)
    name = store.get("active") or "default"
    prof = store["profiles"][name]
    box = prof.get("fish_box")
    if not box:
        sys.exit(f"profile {name!r} has no fish_box -- calibrate the reel bar first")
    return name, box


def primary_rect():
    """The primary monitor as an absolute rect.

    NOT sct.monitors[1] -- that is whichever display Windows enumerates first, which on
    a two-monitor setup is routinely the side screen (here it sits at left=-1920 with
    is_primary False). Config coordinates are absolute virtual-desktop coordinates, so
    cropping the wrong monitor's buffer by them photographs the wrong screen entirely.
    SM_CXSCREEN/SM_CYSCREEN are the primary, matching how ui.py captures.
    """
    return {"left": 0, "top": 0,
            "width": win32api.GetSystemMetrics(0),
            "height": win32api.GetSystemMetrics(1)}


def swap_rb(img):
    """BGRA -> RGBA (or back). Only the first three channels matter downstream."""
    out = img.copy()
    out[:, :, 0], out[:, :, 2] = img[:, :, 2], img[:, :, 0]
    return out


def band_report(img):
    """find_grey over several row bands -- tests the 25-85% band suspicion directly.

    If the box is only found on some bands, the band is wrong. If it's found on none,
    the band is innocent and the problem is the box or the colours.
    """
    h = img.shape[0]
    out = []
    for name, lo, hi in (("25-85", 0.25, 0.85), ("full", 0.0, 1.0),
                         ("10-90", 0.10, 0.90), ("40-60", 0.40, 0.60)):
        crop = img[int(h * lo):max(int(h * hi), int(h * lo) + 1)]
        got = fishing.find_grey(crop)
        out.append(f"{name}:{'%.0f' % got[0] if got else '--'}")
    return " ".join(out)


def col_stats(img):
    """Where the grey-ish and gold-ish columns actually are, ignoring the thresholds.

    Reported as absolute screen x so it can be compared straight against the box.
    """
    c = fishing._profile(img)
    lo, hi = c.min(axis=1), c.max(axis=1)
    grey = (lo > fishing.GREY_MIN) & (hi < fishing.GREY_MAX) & \
           ((hi - lo) < fishing.GREY_MAX_SAT)
    idx = np.flatnonzero(grey)
    span = f"{idx.min()}-{idx.max()}" if idx.size else "none"
    return (f"grey_cols {idx.size:4d} [{span}]  "
            f"lo {lo.min():3.0f}/{lo.max():3.0f}  "
            f"hi {hi.min():3.0f}/{hi.max():3.0f}  "
            f"sat {(hi - lo).max():3.0f}")


def main():
    name, box = load_fish_box()
    print(f"profile {name!r}   fish_box {box['width']}x{box['height']} "
          f"@{box['left']},{box['top']}")

    scr = primary_rect()
    print(f"primary monitor  {scr['width']}x{scr['height']} @0,0")
    if not (0 <= box["left"] and 0 <= box["top"]
            and box["left"] + box["width"] <= scr["width"]
            and box["top"] + box["height"] <= scr["height"]):
        print(">>> WARNING: fish_box falls outside the primary monitor. Roblox must be")
        print("    on the primary display -- the bot's mouse control is primary-only.")
    print(f"grey thresholds  {fishing.GREY_MIN}..{fishing.GREY_MAX} "
          f"sat<{fishing.GREY_MAX_SAT} minwidth {fishing.GREY_MIN_WIDTH}")
    print(f"gold thresholds  R>{fishing.GOLD_MIN_R} G>{fishing.GOLD_MIN_G} "
          f"B<{fishing.GOLD_MAX_B} R-B>{fishing.GOLD_MIN_RB}")
    for n in (3, 2, 1):
        print(f"  starting in {n}... (alt-tab to Roblox and cast)", flush=True)
        time.sleep(1.0)
    print(f"\nwatching for {SECONDS:.0f}s\n")
    print(f"{'t':>5}  {'grey':>12}  {'gold':>6}  {'gold_old':>8}  {'reel_bar (full screen)':>26}")
    print("-" * 96)

    best = None          # (score, box_img, full_img) -- prefer a frame with a reel bar
    stats = {"frames": 0, "grey": 0, "gold": 0, "gold_sw": 0, "bar": 0}
    bar_seen = []
    t0 = time.perf_counter()
    with mss.mss() as sct:
        screen = primary_rect()
        # Grab the box by absolute coordinates, the way the engine does, rather than
        # cropping a full-screen grab: same pixels, same code path, nothing to get
        # subtly wrong about monitor origins.
        rect = {k: int(box[k]) for k in ("left", "top", "width", "height")}
        while time.perf_counter() - t0 < SECONDS:
            frame_start = time.perf_counter()
            crop = np.asarray(sct.grab(rect))
            full = np.asarray(sct.grab(screen))
            t = time.perf_counter() - t0

            grey = fishing.find_grey(crop)
            gold = fishing.find_gold(crop)                  # fixed path (BGRA-aware)
            gold_sw = fishing.find_gold(crop, bgr=False)    # the old, broken reading
            bar = fishing.find_reel_bar(full)

            stats["frames"] += 1
            stats["grey"] += grey is not None
            stats["gold"] += gold is not None
            stats["gold_sw"] += gold_sw is not None
            stats["bar"] += bar is not None
            if bar:
                bar_seen.append((bar["left"], bar["top"], bar["width"], bar["height"]))

            g = f"{grey[0]:6.1f}w{grey[1]:<4.0f}" if grey else "        --  "
            y = f"{gold:6.1f}" if gold is not None else "    --"
            ys = f"{gold_sw:8.1f}" if gold_sw is not None else "      --"
            b = (f"{bar['width']}x{bar['height']} @{bar['left']},{bar['top']}"
                 if bar else "--")
            print(f"{t:5.1f}  {g}  {y}  {ys}  {b:>26}", flush=True)

            score = (bar is not None) * 4 + (grey is not None) * 2 + (gold_sw is not None)
            if best is None or score > best[0]:
                best = (score, crop.copy(), full.copy())

            time.sleep(max(0.0, 1.0 / FPS - (time.perf_counter() - frame_start)))

    # --- images -------------------------------------------------------------
    _, crop, full = best
    Image.fromarray(swap_rb(crop)[:, :, :3]).save(os.path.join(HERE, "fishprobe.png"))
    Image.fromarray(swap_rb(full)[:, :, :3]).save(os.path.join(HERE, "fishprobe_full.png"))

    # --- verdict ------------------------------------------------------------
    n = max(stats["frames"], 1)
    print("\n" + "=" * 96)
    print(f"frames {stats['frames']}   grey {stats['grey']}   gold {stats['gold']}   "
          f"gold(old RGB reading) {stats['gold_sw']}   reel_bar {stats['bar']}")
    print(f"last frame  {col_stats(crop)}")
    print(f"find_grey by band  {band_report(crop)}")

    if bar_seen:
        ls = sorted(set(bar_seen))
        print(f"reel_bar located at {ls[0][2]}x{ls[0][3]} @{ls[0][0]},{ls[0][1]}"
              + (f"  (+{len(ls) - 1} other reading(s))" if len(ls) > 1 else ""))
        bl, bt, bw, bh = ls[0]
        print(f"saved fish_box      {box['width']}x{box['height']} "
              f"@{box['left']},{box['top']}")
        dx, dy = box["left"] - bl, box["top"] - bt
        if abs(dx) > 12 or abs(dy) > 12:
            print(f">>> BOX IS OFF by dx={dx} dy={dy} -- recalibrate; the crop is "
                  f"missing part of the bar")
        else:
            print(f"    box agrees with the bar (dx={dx} dy={dy}) -- the box is fine")

    print()
    if stats["gold"] and not stats["gold_sw"]:
        print(">>> CHANNEL-ORDER FIX CONFIRMED LIVE. Gold is found on the BGRA-aware")
        print("    path and never on the old RGB reading -- exactly the bug, now fixed.")
    elif not stats["grey"] and not stats["gold"] and not stats["bar"]:
        print(">>> NOTHING FOUND ANYWHERE, not even by find_reel_bar on the full screen.")
        print("    The minigame probably wasn't on screen. Check fishprobe_full.png:")
        print("    if the reel bar IS in it, this is a colour problem, not a box problem.")
    elif stats["grey"] and stats["gold"]:
        print(">>> Both found. Detection works on these frames -- the fault is upstream")
        print("    in the engine (feature/running gating), not in fishing.py.")
    else:
        print(">>> Mixed result. Compare fishprobe.png against fishprobe_full.png:")
        print("    if the bar is cut off or absent in fishprobe.png, the box is wrong.")
    print("\nsaved fishprobe.png and fishprobe_full.png")


if __name__ == "__main__":
    main()
