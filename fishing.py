"""Fishing reel minigame: detection + controller.

Measured from real footage (4 games, 30fps):
  - The grey player box accelerates RIGHT while the mouse is held (~1600 px/s^2) and
    LEFT while released (~1200 px/s^2). Momentum, no snap -- a double integrator.
  - The gold target is stationary ~56% of the time, then darts (usually gentle, up to
    ~2700 px/s). Continuous, no teleports.
  - The grey box is ~250px wide, the gold ~30px, so there's ~110px of slack each side
    before the gold leaves the box.
  - The progress bar above is green when the gold is inside the box, red when it isn't.

The controller is bang-bang (hold or release, the only inputs) with a phase-plane
switching line and hysteresis, so it settles into a small hover around the gold instead
of chattering the mouse at 60Hz.
"""

import numpy as np

# --- detection ---------------------------------------------------------------
# Grey player box: bright-ish, near-grey (low saturation).
GREY_MIN = 110
GREY_MAX = 235
GREY_MAX_SAT = 55
GREY_MIN_WIDTH = 60
# Gold target: saturated yellow.
GOLD_MIN_R = 170
GOLD_MIN_G = 115
GOLD_MAX_B = 125
GOLD_MIN_RB = 85
# The gold is ~30px wide; allow a bit more so a gold sitting inside the grey box
# (or straddling its edge) never splits the box into fragments.
GREY_MERGE_GAP = 60

# CHANNEL ORDER. The constants above were measured off RGB video, but live frames come
# from mss, which hands back B,G,R,A (verified against mss's own .rgb conversion). Only
# the functions that index a channel by POSITION care -- the grey and track tests use
# per-pixel min/max across channels and are order-agnostic. That asymmetry is why the
# grey box always read fine while the gold was never found even once: find_gold was
# testing the BLUE channel against GOLD_MIN_R, and gold has almost no blue in it.
#
# So: bgr=True is the live default, and offline callers reading RGB frames (decoded
# video, PIL images) must pass bgr=False.


def _channels(bgr):
    """-> (red_index, blue_index) for the frame layout in use."""
    return (2, 0) if bgr else (0, 2)


def _profile(img):
    """Average the track-interior rows to a per-column colour profile.

    A middle band rather than the whole box: averaging the full height mixes in the
    dark border (and, on a loosely-drawn box, the progress bar above) and washes the
    grey box out entirely -- learned the hard way. 25-85% lands inside the track
    whether the box is drawn tight on it or a little generously."""
    h = img.shape[0]
    band = img[int(h * 0.25):int(h * 0.85)]
    return band[:, :, :3].astype(np.int16).mean(axis=0)


# Measured off real footage: the reel track is a flat, very dark strip, and its columns
# are uniform top-to-bottom. Scenery at the same height is textured, which is what
# separates the bar from the game behind it.
TRACK_MAX_MEAN = 70
TRACK_MAX_STD = 22


def find_reel_bar(img, bgr=True):
    """Locate the reel track on a full screenshot -> {left,top,width,height} or None.

    Requires the signature of the real thing -- a dark uniform strip containing BOTH a
    wide grey box and a gold marker -- so other dark UI can't match it.
    """
    a = img[:, :, :3].astype(np.float32)
    ri, bi = _channels(bgr)
    H, W, _ = a.shape
    bright = a.max(axis=2)
    lo = a.min(axis=2)
    dark = bright < 90
    grey = (lo > GREY_MIN) & (bright < GREY_MAX) & ((bright - lo) < GREY_MAX_SAT)
    gold = (a[:, :, ri] > GOLD_MIN_R) & (a[:, :, 1] > GOLD_MIN_G) & (a[:, :, bi] < GOLD_MAX_B)

    rows = np.flatnonzero((dark | grey | gold).sum(axis=1) > W * 0.30)
    if rows.size == 0:
        return None
    groups = np.split(rows, np.flatnonzero(np.diff(rows) > 3) + 1)
    for band_rows in sorted(groups, key=len, reverse=True):
        y0, y1 = int(band_rows.min()), int(band_rows.max())
        if y1 - y0 < 20:
            continue
        band = a[y0:y1 + 1]
        bb = band.max(axis=2)
        bl = band.min(axis=2)
        colmean, colstd = bb.mean(axis=0), bb.std(axis=0)
        g = ((bl > GREY_MIN) & (bb < GREY_MAX) & ((bb - bl) < GREY_MAX_SAT)).mean(axis=0) > 0.5
        y = ((band[:, :, ri] > GOLD_MIN_R) & (band[:, :, bi] < GOLD_MAX_B)).mean(axis=0) > 0.4
        if g.sum() < 60 or y.sum() < 3:
            continue                      # no player box / no target -> not the reel bar
        track = ((colmean < TRACK_MAX_MEAN) & (colstd < TRACK_MAX_STD)) | g | y
        idx = np.flatnonzero(track)
        if idx.size == 0:
            continue
        runs = np.split(idx, np.flatnonzero(np.diff(idx) > 12) + 1)
        run = max(runs, key=len)
        if run.max() - run.min() < 250:
            continue
        return {"left": int(run.min()), "top": y0,
                "width": int(run.max() - run.min()), "height": y1 - y0}
    return None


def _runs(mask):
    d = np.diff(mask.astype(np.int8), prepend=0, append=0)
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def _largest_run(mask, merge_gap=0):
    """Widest run of True, optionally stitching runs separated by a small gap.

    The gap matters for the grey box: when the gold sits INSIDE it -- which is exactly
    the state we're trying to hold -- the gold's columns aren't grey, so the box reads
    as two fragments. Taking the bigger fragment would report a box half the real width
    with an off-centre middle, and the centre would jump sideways the moment the gold
    crossed it. Stitching across a gold-sized gap keeps the box whole.
    """
    rs = _runs(mask)
    if not rs:
        return None
    if merge_gap > 0:
        merged = [list(rs[0])]
        for s, e in rs[1:]:
            if s - merged[-1][1] <= merge_gap:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        rs = [tuple(r) for r in merged]
    s, e = max(rs, key=lambda r: r[1] - r[0])
    return int(s), int(e)


def find_grey(img):
    """-> (center_x, width) of the grey player box, or None."""
    c = _profile(img)
    lo, hi = c.min(axis=1), c.max(axis=1)
    mask = (lo > GREY_MIN) & (hi < GREY_MAX) & ((hi - lo) < GREY_MAX_SAT)
    run = _largest_run(mask, merge_gap=GREY_MERGE_GAP)
    if run is None or (run[1] - run[0]) < GREY_MIN_WIDTH:
        return None
    return (run[0] + run[1] - 1) / 2.0, run[1] - run[0]


def find_gold(img, bgr=True):
    """-> center_x of the gold target, or None."""
    c = _profile(img)
    ri, bi = _channels(bgr)
    r, g, b = c[:, ri], c[:, 1], c[:, bi]
    mask = (r > GOLD_MIN_R) & (g > GOLD_MIN_G) & (b < GOLD_MAX_B) & (r - b > GOLD_MIN_RB)
    run = _largest_run(mask)
    if run is None:
        return None
    return (run[0] + run[1] - 1) / 2.0


def reel_active(img, bgr=True):
    """Is the reel minigame on screen? Needs both a wide grey box and a gold target."""
    return find_grey(img) is not None and find_gold(img, bgr) is not None


# --- controller --------------------------------------------------------------
class FishController:
    """Decides hold vs release each frame to keep the grey box centred on the gold.

    The plant is a double integrator with only two inputs (accelerate right = hold,
    accelerate left = release), so the right rule is the time-optimal *switching curve*,
    not a linear PD. With position error e = gold - grey and grey velocity v, the grey
    would need distance v*|v|/(2A) to brake to a stop. So we hold while

        s = e - v*|v| / (2A)   > 0

    and release otherwise: hold when we still have ground to cover toward the gold even
    after accounting for the braking we'll need, release once our momentum will carry us
    the rest of the way. That braking term is what stops it overshooting on fast darts.
    A deadband holds the last decision in a small window so the mouse doesn't toggle
    every frame -- it settles into a slow hover instead.
    """

    def __init__(self, a_eff=900.0, deadband=4.0, lead=0.03):
        self.a_eff = a_eff        # effective accel used for the braking curve (px/s^2)
        self.deadband = deadband  # px of hysteresis around the switch curve
        self.lead = lead          # s of target-motion lead
        self._hist = []           # (t, grey_c)
        self._gold_hist = []
        self._hold = False

    def reset(self):
        self._hist.clear()
        self._gold_hist.clear()
        self._hold = False

    @staticmethod
    def _vel(hist):
        if len(hist) < 2:
            return 0.0
        (t0, x0), (t1, x1) = hist[0], hist[-1]
        return (x1 - x0) / (t1 - t0) if t1 > t0 else 0.0

    def decide(self, grey_c, gold_c, t):
        self._hist.append((t, grey_c))
        self._gold_hist.append((t, gold_c))
        while self._hist and t - self._hist[0][0] > 0.06:
            self._hist.pop(0)
        while self._gold_hist and t - self._gold_hist[0][0] > 0.06:
            self._gold_hist.pop(0)

        v = self._vel(self._hist)
        gv = self._vel(self._gold_hist)
        target = gold_c + gv * self.lead          # lead the gold a touch
        e = target - grey_c
        s = e - (v * abs(v)) / (2.0 * self.a_eff)  # time-optimal switching curve

        if s > self.deadband:
            self._hold = True
        elif s < -self.deadband:
            self._hold = False
        # else: keep the previous decision (hysteresis)
        return self._hold
