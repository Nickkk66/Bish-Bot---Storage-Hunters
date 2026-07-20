"""Work out the BID button and bar boxes by watching the screen, instead of dragging.

Two different signals, because the two things look nothing alike:

  the BID button   when its outline lights, it draws two long white horizontal lines
                   sharing an x-span -- the top and bottom of one rectangle.
  the bar          it is the only thing inside that panel that MOVES. Accumulating
                   changed pixels over a few rounds paints in exactly the strip the
                   marker sweeps, and nothing else.

Motion is the right signal for the bar: matching on colour would need to know whether
the zone is green or orange, and both change with the round.
"""

import time

import mss
import numpy as np
import win32api

from engine import _press, move_to

WHITE_MIN = 200
WHITE_SPREAD = 25
MIN_BUTTON_RUN = 180     # the lit outline is hundreds of px wide
MIN_BUTTON_H = 40
MAX_BUTTON_H = 260       # taller than this is a panel, not a button
MOTION_DELTA = 40        # per-pixel change that counts as movement
ACTIVE_FRAC = 0.5        # how busy a row must be, relative to the busiest one
IDLE_GIVEUP = 8.0        # seconds with no cue before we call the bidding done


def _white(a):
    lo = a.min(axis=2)
    return (lo > WHITE_MIN) & ((a.max(axis=2) - lo) < WHITE_SPREAD)


def _long_rows(mask, min_len):
    """-> [(row, start, end)] for every horizontal white run at least min_len wide."""
    d = np.diff(mask.astype(np.int8), axis=1, prepend=0, append=0)
    rs, cs = np.where(d == 1)
    _, ce = np.where(d == -1)
    lens = ce - cs
    k = lens >= min_len
    return list(zip(rs[k].tolist(), cs[k].tolist(), ce[k].tolist()))


def find_button(img):
    """The lit BID outline -> its box, or None if it isn't lit in this frame.

    Cheap prefilter first. Testing every one of two million pixels for whiteness cost
    78ms a scan, which left barely ten looks a second -- slow enough to blink straight
    past an outline that is only lit for a moment, and that is a miss you cannot see.
    One channel is enough to shortlist rows that could hold a long bright run; the real
    test then only touches those few rows.
    """
    a = img[:, :, :3]
    hot = np.flatnonzero((a[:, :, 1] > WHITE_MIN).sum(axis=1) >= MIN_BUTTON_RUN)
    if hot.size < 2:
        return None
    runs = [(int(hot[r]), s, e) for r, s, e in _long_rows(_white(a[hot]), MIN_BUTTON_RUN)]
    if len(runs) < 2:
        return None

    # Group runs that share an x-span: the top and bottom edges of one rectangle.
    groups = {}
    for r, s, e in runs:
        groups.setdefault((s // 12, e // 12), []).append((r, s, e))

    # Check EVERY group, not just the biggest. A thin UI border spanning the screen has
    # more rows than the button's two edges, so picking by size alone hands back a line
    # with no height, and the whole scan reports "found nothing" while staring at it.
    best = None
    for g in groups.values():
        rows = [r for r, _, _ in g]
        top, bot = min(rows), max(rows)
        if not (MIN_BUTTON_H <= bot - top <= MAX_BUTTON_H):
            continue
        left = min(s for _, s, _ in g)
        right = max(e for _, _, e in g)
        cand = {"left": int(left), "top": int(top),
                "width": int(right - left), "height": int(bot - top)}
        if best is None or cand["width"] > best["width"]:
            best = cand
    return best


def _longest_run(mask):
    """Longest horizontal run of True anywhere in the mask, in pixels."""
    if not mask.any():
        return 0
    d = np.diff(mask.astype(np.int8), axis=1, prepend=0, append=0)
    s = np.flatnonzero(d.ravel() == 1)
    e = np.flatnonzero(d.ravel() == -1)
    return int((e - s).max()) if s.size else 0


def bar_from_motion(active, seen, band):
    """Bounding box of the sweeping marker -> the bar.

    `active` counts, per row, how many FRAMES had movement in it. That -- not how much
    of the row moved -- is what separates the bar from the price text above it: the
    marker moves every single frame, while text changes once a bid. Coverage fails
    here because the marker jumps ~20px per frame, so it only ever paints the discrete
    positions it landed on and can never cover most of a row however long you watch.

    The bar is then every row nearly as busy as the busiest one, which self-calibrates
    instead of guessing an absolute number.
    """
    if active.max() < 4:
        return None
    rows = np.where(active >= active.max() * ACTIVE_FRAC)[0]
    if rows.size < 4:
        return None
    top, bot = int(rows.min()), int(rows.max())
    # Extent, not coverage: the marker sweeps the bar end to end, so where it has ever
    # been spans the bar's width even though the gaps between hops stay untouched.
    cols = np.where(seen[top:bot + 1].any(axis=0))[0]
    if cols.size < 40:
        return None
    left, right = int(cols.min()), int(cols.max())
    return {"left": band["left"] + left, "top": band["top"] + top,
            "width": right - left, "height": bot - top}


def auto_calibrate(seconds=30, on_progress=None, should_stop=None,
                   click=False, hold=0.04, max_seconds=150):
    """Watch the screen -> a config dict, or None if the button never showed.

    Needs a live auction: the outline has to light at least once, and the marker has
    to sweep. Everything else is derived from those two.

    With click=True it also BIDS on every cue it sees, and keeps going until the
    auction goes quiet rather than stopping at `seconds`. That turns calibration into
    the real thing end to end -- detecting a cue proves half of it, and the half it
    skips is the half that has actually been broken. `seconds` still caps the hunt for
    the button; `max_seconds` is the backstop so a lively auction can't run forever.
    """
    say = on_progress or (lambda *a: None)
    pw, ph = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    full = {"left": 0, "top": 0, "width": pw, "height": ph}

    btn = None
    t0 = time.perf_counter()
    with mss.mss() as sct:
        # 1. The button, from its lit outline.
        while time.perf_counter() - t0 < seconds:
            if should_stop and should_stop():
                return None
            btn = find_button(np.asarray(sct.grab(full))[:, :, :3])
            if btn:
                break
            say(f"{seconds - (time.perf_counter() - t0):.0f}s  "
                f"looking for the BID outline...", False, False, 0)
        if btn is None:
            return None

        # 2 & 3. The bar, from motion -- and at the same time, prove the box we just
        # found actually catches the cue. One grab covers both: they sit in one panel,
        # and two grabs would halve the frame rate for nothing.
        top = max(0, btn["top"] - 240)
        band = {"left": max(0, btn["left"] - 40), "top": top,
                "width": min(pw - max(0, btn["left"] - 40), btn["width"] + 80),
                "height": max(20, btn["top"] - top - 4)}
        panel = dict(band, height=min(ph - band["top"],
                                      btn["top"] + btn["height"] + 14 - band["top"]))
        seen = np.zeros((band["height"], band["width"]), bool)
        active = np.zeros(band["height"], np.int32)
        by0 = max(0, btn["top"] - panel["top"] - 12)
        bx0 = max(0, btn["left"] - panel["left"] - 12)
        bx1 = min(panel["width"], bx0 + btn["width"] + 24)
        prev, cues, was_lit, jig = None, 0, False, 0
        cx = btn["left"] + btn["width"] // 2
        cy = btn["top"] + btn["height"] // 2
        last_act = time.perf_counter()
        # Clicking means playing it out, so the button hunt's clock no longer applies.
        deadline = max_seconds if click else seconds

        while time.perf_counter() - t0 < deadline:
            if should_stop and should_stop():
                return None
            now = time.perf_counter()

            # Same 1px jiggle the engine uses. Without a real move event the game's
            # pointer never learns where the cursor is and the press lands nowhere.
            if click:
                jig ^= 1
                move_to(cx + jig, cy)

            img = np.asarray(sct.grab(panel))[:, :, :3]

            cur = img[:band["height"]].astype(np.int16)
            if prev is not None:
                d = np.abs(cur - prev).max(axis=2) > MOTION_DELTA
                seen |= d
                active += d.any(axis=1)
            prev = cur

            # Does the cue register through the box we found -- and does clicking it
            # actually do anything? Rising edges only, exactly like the real loop.
            lit = _longest_run(_white(img[by0:, bx0:bx1])) >= max(60, btn["width"] // 3)
            if lit and not was_lit:
                cues += 1
                last_act = now
                if click:
                    _press(hold)
            was_lit = lit

            bar = bar_from_motion(active, seen, band)
            say(f"{max(0, deadline - (now - t0)):.0f}s  "
                f"{'bidding' if click else 'watching'} - {cues} cue(s)",
                True, bar is not None, cues)

            # The cue stopping is the end of the auction. Deciding it from pixels --
            # "does this still look like the panel?" -- cannot work: the dark dirt and
            # grey concrete behind it are also dark and flat, so the panel never seems
            # to leave. Cues only exist while an auction runs.
            if bar is not None and cues >= 1 and now - last_act > IDLE_GIVEUP:
                say("done - bidding stopped", True, True, cues)
                break

    bar = bar_from_motion(active, seen, band)
    if bar is None:
        # Never saw it sweep. Guess a strip above the button; the editor can fix it.
        bar = {"left": btn["left"], "top": max(0, btn["top"] - 90),
               "width": btn["width"], "height": 60}
    return {
        "bid_box": btn,
        "bid_xy": [btn["left"] + btn["width"] // 2, btn["top"] + btn["height"] // 2],
        "track": bar,
        "cues_seen": cues,
    }
