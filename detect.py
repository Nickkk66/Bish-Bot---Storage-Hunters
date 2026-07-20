"""Pixel analysis for the bid bar: find the white marker and the green target zone.

The track is littered with light-grey specks that are the same colour as the marker,
so colour alone cannot identify it. What separates them is geometry:

    the marker   full height of the bar, narrow
    specks       short stubs, only a fraction of the bar's height
    grey blocks  tall but far too wide

So every column is scored by its *fill* -- the fraction of rows in it that are white --
and anything that isn't a full-height, narrow column is discarded.

Pure functions over numpy arrays, so they can be tested against a saved screenshot
with no game running:  python detect.py shot.png [left top width height]
"""

import numpy as np

# How white is "white"? A fixed number is a guess about the game's palette, and a bad
# guess is invisible: too high and the line is never seen at all. What's reliable is
# that the line is the BRIGHTEST thing in the strip -- the specks and blocks are always
# dimmer. So the bar is set relative to the brightest pixel present, with an absolute
# floor as a backstop. That adapts whether the line renders at 255 or at 210, while
# still excluding greys sitting well below it.
#
# Excluding them matters for more than tidiness: a grey block bright enough to count as
# "white" fuses with the line into one over-wide run and hides it completely.
# The drop is deliberately tight. Allowing 25 let a grey block within 25 of the line
# count as white, fuse with it into one over-wide run, and hide it entirely -- the same
# failure a fixed threshold caused, just harder to see. The line's core sits at the
# peak, so a narrow band isolates it and leaves every dimmer grey out.
WHITE_MIN_BRIGHT = 170   # absolute floor...
WHITE_REL_DROP = 8       # ...or within this of the strip's brightest pixel, whichever is higher
WHITE_MAX_SPREAD = 25    # and this close to grey, to count as marker-coloured
MARKER_MIN_FILL = 0.60   # column must be at least this tall to be the marker
MARKER_MAX_WIDTH = 40    # anything wider is a background block, not the line

ZONE_MIN_SAT = 40        # how far from grey a pixel must be to count as coloured
ZONE_MIN_BRIGHT = 60
ZONE_MIN_WIDTH = 4
ZONE_MIN_FILL = 0.45
ZONE_MERGE_GAP = MARKER_MAX_WIDTH + 6


def _runs(mask):
    """List of (start, end_exclusive) for each contiguous True run."""
    if not mask.any():
        return []
    d = np.diff(mask.astype(np.int8))
    starts = (np.flatnonzero(d == 1) + 1).tolist()
    ends = (np.flatnonzero(d == -1) + 1).tolist()
    if mask[0]:
        starts.insert(0, 0)
    if mask[-1]:
        ends.append(len(mask))
    return list(zip(starts, ends))


def _merge_runs(runs, max_gap):
    """Join runs separated by a gap no wider than the marker."""
    out = []
    for s, e in runs:
        if out and s - out[-1][1] <= max_gap:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [tuple(r) for r in out]


def find_markers(img):
    """Every full-height narrow white column in the strip.

    -> list of (x, width, fill), x sub-pixel. Returns all candidates rather than a
    single guess: a speck can still pass the geometry test, and only motion can rule
    it out, so the caller picks using where the line is predicted to be.
    """
    a = img[:, :, :3].astype(np.int16)
    lo = a.min(axis=2)
    th = max(WHITE_MIN_BRIGHT, int(lo.max()) - WHITE_REL_DROP)
    white = (lo > th) & ((a.max(axis=2) - lo) < WHITE_MAX_SPREAD)
    fill = white.mean(axis=0)

    lum = lo.mean(axis=0)              # per-column brightness, all rows
    base = float(np.median(lum))       # the track background behind everything
    out = []
    for s, e in _runs(fill >= MARKER_MIN_FILL):
        if (e - s) > MARKER_MAX_WIDTH:
            continue
        # Widen by a column each side: the marker's anti-aliased edges are too dim
        # to pass the white test, but they carry the sub-pixel centre.
        s2, e2 = max(0, s - 1), min(len(fill), e + 1)
        w = np.clip(lum[s2:e2] - base, 0, None)
        if w.sum() <= 0:
            continue
        x = float((np.arange(s2, e2) * w).sum() / w.sum())
        out.append((x, e - s, float(fill[s:e].mean())))
    return out


def find_marker(img):
    """Best single candidate (the tallest). -> (x, width, fill) or None."""
    c = find_markers(img)
    return max(c, key=lambda t: t[2]) if c else None


OUTLINE_MIN_BRIGHT = 200   # deliberately lower than WHITE_MIN_BRIGHT; see below


def outline_run(img):
    """Longest unbroken horizontal run of white pixels, in pixels -- the lit BID
    outline's top or bottom edge. This is the game's own 'click now' cue.

    Uses its own, lower brightness bar than the marker does. The marker's 235 exists to
    separate it from grey specks sharing its strip; nothing like that lives around the
    button, so the only thing to separate here is a lit outline from an unlit one --
    bright vs dim grey, a much wider gap. A game UI's "white" is often not 255, and
    demanding 255 here buys nothing while risking seeing nothing at all.

    Measured as a length rather than a fraction of the box on purpose: a fraction
    silently depends on the box matching the button's width, so a generously-drawn box
    could never score high. A run length doesn't care how big the box is, so the box
    only has to CONTAIN the outline. Nothing else near the button draws a white line
    hundreds of pixels long -- the marker is vertical (runs of ~5px) and the label is
    green -- so this stays unambiguous.
    """
    # Shortlist rows on ONE channel before doing the real work. Testing every pixel of
    # the box for whiteness cost ~3ms, and this runs on every frame of a race against a
    # 16ms deadline -- 3ms of it spent proving that a box which is almost always empty
    # is, in fact, still empty. One compare over the green channel throws out every row
    # that cannot possibly hold a long bright run, and the exact test then only touches
    # the handful left. Same answer, ~10x less work.
    a = img[:, :, :3]
    hot = np.flatnonzero((a[:, :, 1] > OUTLINE_MIN_BRIGHT).sum(axis=1) >= 8)
    if hot.size == 0:
        return 0
    sub = a[hot]
    lo = sub.min(axis=2)
    white = (lo > OUTLINE_MIN_BRIGHT) & ((sub.max(axis=2) - lo) < WHITE_MAX_SPREAD)
    if not white.any():
        return 0
    # Bracket each row with False so every run has a start and an end, then measure
    # them all in one pass instead of looping over rows.
    d = np.diff(white.astype(np.int8), axis=1, prepend=0, append=0)
    starts = np.flatnonzero(d.ravel() == 1)
    ends = np.flatnonzero(d.ravel() == -1)
    return int((ends - starts).max()) if starts.size else 0


def find_zone(img):
    """The target block -- whatever colour the game paints it. -> (left, right, center).

    Matching on "green" was wrong: the zone is green in early rounds and orange by the
    high ones, so hue carries no signal. What actually holds is that the zone is a
    saturated colour against a grey bar, and -- like the marker -- it spans the bar's
    full height while the specks sharing its colour are short stubs.

    The marker paints over the zone as it crosses, splitting it in two. Taking the
    biggest fragment would put the "centre" off to one side exactly when the marker is
    closest to it, so stitch fragments back together across any marker-sized gap first.
    """
    a = img[:, :, :3].astype(np.int16)
    lo, hi = a.min(axis=2), a.max(axis=2)
    coloured = ((hi - lo) > ZONE_MIN_SAT) & (hi > ZONE_MIN_BRIGHT)
    fill = coloured.mean(axis=0)
    runs = _merge_runs(_runs(fill >= ZONE_MIN_FILL), ZONE_MERGE_GAP)
    runs = [x for x in runs if (x[1] - x[0]) >= ZONE_MIN_WIDTH]
    if not runs:
        return None
    s, e = max(runs, key=lambda t: t[1] - t[0])
    return s, e, (s + e - 1) / 2.0


if __name__ == "__main__":
    import sys
    from PIL import Image

    if len(sys.argv) < 2:
        sys.exit("usage: python detect.py shot.png [left top width height]")
    img = np.asarray(Image.open(sys.argv[1]).convert("RGB"))[:, :, ::-1]  # -> BGR
    if len(sys.argv) == 6:
        l, t, w, h = (int(v) for v in sys.argv[2:6])
        img = img[t:t + h, l:l + w]
    print(f"region {img.shape[1]}x{img.shape[0]}")
    print("zone  :", find_zone(img))
    print("candidates (x, width, fill):")
    for c in find_markers(img):
        print(f"   x={c[0]:7.2f}  w={c[1]:3d}  fill={c[2]:.2f}")
