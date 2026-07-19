"""Autonomous capture/predict/fire loop. Runs on a background thread."""

import threading
import time
from collections import deque

import mss
import numpy as np
import win32api
import win32con
import win32gui

import detect
import fishing

VK_F8 = 0x77
VK_P = 0x50
# Two panic keys: F8 is a reach mid-game, P is already under your hand. P also means
# any stray P -- chat, a keybind -- stops the bot. That's the right way round for a
# kill switch: stopping when you didn't mean to costs nothing, failing to stop does.
PANIC_KEYS = ((VK_F8, "F8"), (VK_P, "P"))


def panic_pressed():
    """Name of a panic key currently held, or None. Anything that clicks on its own
    must check this -- calibration included, which bids for real."""
    return next((n for vk, n in PANIC_KEYS
                 if win32api.GetAsyncKeyState(vk) & 0x8000), None)


def game_focused():
    """Is Roblox the foreground window right now?

    The toggle hotkey is gated on this so a stray press elsewhere -- typing on YouTube,
    a keybind in another app -- can't start or stop the bot. Fails closed: if we can't
    confirm Roblox is in front, the hotkey does nothing. The START button is NOT gated
    (it's a deliberate in-app action), and neither are the panic keys (a stop must
    always work).
    """
    try:
        return "roblox" in win32gui.GetWindowText(win32gui.GetForegroundWindow()).lower()
    except Exception:
        return False

# Screen capture is capped at the monitor's refresh rate -- the pixels only change
# that often, so no capture method can see the marker more than ~60x/sec on a 60Hz
# panel. Every constant below is sized around that ~16.7ms sampling floor.
SAMPLE_WINDOW = 0.110   # ~6 frames at 60Hz: enough to fit a slope through
MIN_SAMPLES = 3
MIN_SPEED = 60.0        # px/s below this we assume the bar isn't running
MAX_FIT_RMS = 6.0       # px off a straight line before we distrust the track
REARM_GAP = 0.25        # min seconds between shots, whatever else happens
MARKER_LOST_REARM = 0.3
FISH_END_GAP = 1.0      # reel bar gone this long = the catch is over
IDLE_STOP = 10.0        # default seconds with no cue before we call the bidding done.
                        # Per-profile override lives in config as "stop_after_s", set
                        # from the Bidding tab -- round length is a property of the
                        # game mode, not something one constant can be right about.
                        # Erring long on purpose: stopping early loses bids, stopping
                        # late costs a few idle seconds. Those are not equal mistakes.
GAP_MARGIN = 2.5        # never stop sooner than the longest gap actually seen + this.
                        # A setting tighter than a real round would read the pause
                        # between two bids as the end and quit mid-auction; the floor
                        # makes an over-eager slider harmless from the second cue on.
                        # It cannot help before the second cue, though -- nothing can,
                        # since one gap is the minimum evidence of how long a round is.
                        # That blind spot is why auto-stop has an off switch.


def _refresh_period():
    try:
        hz = win32api.EnumDisplaySettings(None, win32con.ENUM_CURRENT_SETTINGS).DisplayFrequency
        if 20 <= hz <= 500:
            return 1.0 / hz
    except Exception:
        pass
    return 1.0 / 60.0


class State:
    """Shared between UI thread and engine thread. Guarded by .lock."""

    def __init__(self, lead_ms=25.0, mode="outline", outline_min_run=150, cue_bright=200):
        self.lock = threading.Lock()
        # UI -> engine
        self.running = False
        self.dry_run = True
        self.auto_tune = False
        self.lead_ms = lead_ms
        self.mode = mode                  # "outline" (react to the cue) | "timing"
        self.outline_min_run = outline_min_run
        self.cue_bright = cue_bright      # lower = fires on a dimmer, earlier cue
        self.toggle_vk = 0                # hotkey that starts/stops BIDDING; 0 = unset
        self.fish_vk = 0                  # hotkey that starts/stops FISHING
        self.auction_key_on = True        # per-hotkey enables (global tab)
        self.fish_key_on = True
        self.auto_stop = True             # bidding stops itself when the cue stops
        self.feature = "auction"          # "auction" | "fishing" -- one at a time,
                                          # they both drive the same mouse
        # fishing telemetry
        self.fish_err = 0.0               # |grey centre - gold centre| px
        self.fish_margin = 0.0            # how much slack before the gold escapes
        self.fish_on_pct = 0.0            # % of this catch spent on target
        self.fish_hold = False            # is the mouse currently held down
        self.fish_secs = 0.0              # length of the current catch
        # engine -> UI
        self.status = "idle"
        self.hits = 0
        self.misses = 0
        self.last = "-"
        self.fps = 0.0
        self.outline_peak = 0
        self.max_gap = 0.0       # longest seen between two cues = longest round
        # Raw internals, for the diagnostics readout. Cheap to keep, and the only way
        # to tell "not firing" apart from "firing and missing" without guessing.
        self.run_px = 0          # white line length seen this frame
        self.scan_ms = 0.0       # time to analyse one frame
        self.loop_ms = 0.0       # whole iteration, grab included
        self.armed = True
        self.focus = "-"
        self.last_fire = 0.0     # perf_counter of the last press

    def snapshot(self):
        with self.lock:
            return dict(self.__dict__, lock=None)

    def set(self, **kw):
        with self.lock:
            self.__dict__.update(kw)


def _fit(samples):
    """Least-squares over (t, x) samples -> (px/s, rms residual in px).

    The residual is the honesty check: the real line travels in a straight line, so a
    track that a speck has crept into won't fit one, and a bad fit means don't shoot.
    """
    ts = np.array([s[0] for s in samples], dtype=np.float64)
    xs = np.array([s[1] for s in samples], dtype=np.float64)
    ts -= ts[0]
    a = np.vstack([ts, np.ones_like(ts)]).T
    sol = np.linalg.lstsq(a, xs, rcond=None)[0]
    rms = float(np.sqrt(np.mean((xs - a @ sol) ** 2)))
    return float(sol[0]), rms


def _slope(samples):
    return _fit(samples)[0]


CLICK_HOLD = 0.040      # press-to-release. Constant, so lead tuning absorbs it.


def _press(hold=CLICK_HOLD):
    """One solid click wherever the cursor already is.

    Down and up with nothing in between reads as a glitch to some UI: the button
    never observes a held state. Holding briefly makes it an unambiguous click.
    The delay is harmless even if the game acts on release rather than press --
    a *constant* offset is exactly what lead_ms absorbs.
    """
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(hold)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def move_to(x, y):
    """Move the cursor with a real input event rather than SetCursorPos.

    SetCursorPos teleports the OS cursor WITHOUT generating a mouse input event. A game
    that tracks the pointer through raw input never learns it moved: its own cursor
    stays where it last saw it, so our click hit-tests wherever the game still thinks
    the mouse is -- not where we put it. That is exactly why shaking the physical mouse
    made clicks suddenly start landing; real hardware movement told the game the truth.

    mouse_event with MOVE|ABSOLUTE goes through the same path as a real mouse, so the
    game sees it. Coordinates are 0..65535 across the primary monitor.
    """
    sw = max(1, win32api.GetSystemMetrics(0) - 1)
    sh = max(1, win32api.GetSystemMetrics(1) - 1)
    win32api.mouse_event(win32con.MOUSEEVENTF_MOVE | win32con.MOUSEEVENTF_ABSOLUTE,
                         int(x * 65535 / sw), int(y * 65535 / sh), 0, 0)


def click_once(xy, hold=CLICK_HOLD):
    """Move and click, with time for the button to notice the hover. For testing:
    not timing-critical, so it can afford to settle."""
    move_to(*xy)
    time.sleep(0.05)
    _press(hold)


BOX_MARGIN = 12   # grown on every side; see below


def bid_box(config):
    """The patch watched for the lit outline, grown by a margin on each side.

    The margin matters: drawing the box by hand lands it *on* the button's edge, and a
    box a pixel inside the outline excludes the very thing we're looking for -- the bot
    then reads 0px forever with nothing visibly wrong. Growing it costs nothing, since
    the measure is a run length that doesn't care how big the box is, and background
    almost never contains a white line hundreds of pixels long.
    """
    b = config.get("bid_box")
    if not b:
        t, (x, y) = config["track"], config["bid_xy"]
        b = {"left": t["left"], "top": y - 45, "width": t["width"], "height": 90}
    return {"left": max(0, int(b["left"]) - BOX_MARGIN),
            "top": max(0, int(b["top"]) - BOX_MARGIN),
            "width": int(b["width"]) + 2 * BOX_MARGIN,
            "height": int(b["height"]) + 2 * BOX_MARGIN}


def fish_box(config):
    """The reel-bar strip watched while fishing, or None if not calibrated yet."""
    b = config.get("fish_box")
    if not b:
        return None
    return {k: int(v) for k, v in b.items()}


def slice_region(track):
    """Middle 80% of the track height: trims the borders but keeps enough of the bar
    that a full-height marker is clearly taller than a speck."""
    h = max(4, int(track["height"] * 0.8))
    return {
        "left": int(track["left"]),
        "top": int(track["top"] + (track["height"] - h) // 2),
        "width": int(track["width"]),
        "height": h,
    }


class Engine(threading.Thread):
    daemon = True

    def __init__(self, state, config):
        super().__init__()
        self.state = state
        self.config = config
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        # Tunable without a code edit, in case nothing is as white as assumed.
        detect.WHITE_MIN_BRIGHT = self.config.get("white_min", detect.WHITE_MIN_BRIGHT)
        detect.MARKER_MIN_FILL = self.config.get("min_fill", detect.MARKER_MIN_FILL)
        detect.OUTLINE_MIN_BRIGHT = self.config.get("outline_white_min",
                                                    detect.OUTLINE_MIN_BRIGHT)

        region = slice_region(self.config["track"])
        obox = bid_box(self.config)
        bid_xy = tuple(self.config["bid_xy"])
        width = region["width"]
        fish_region = fish_box(self.config)
        self._out_armed = True
        self._last_cue = 0.0
        self._max_gap = 0.0         # longest gap between two cues seen this run
        self._key_down = {}         # per-feature hotkey edge state
        self._key_seen = {}         # per-feature last-seen binding
        self._mouse_down = False
        self._fish_active = False
        self._fish_seen = 0.0
        self._fish_start = 0.0
        self._fish_on = self._fish_n = 0
        self._ctrl = fishing.FishController(
            a_eff=self.config.get("fish_accel", 900.0),
            deadband=self.config.get("fish_deadband", 4.0),
            lead=self.config.get("fish_lead", 0.03))

        hist = deque()
        gaps = deque(maxlen=12)
        jiggle = 0
        frame_dt = _refresh_period()
        green_seen = None
        fired_at = 0.0
        armed = True
        last_seen = 0.0
        frames = 0
        fps_t = time.perf_counter()
        loop_t = 0.0

        with mss.mss() as sct:
            while not self._stop.is_set():
                st = self.state.snapshot()

                # One toggle key: press flips running on<->off. Edge-detected against
                # our own last-seen state, so holding it can't rapid-fire and a single
                # tap toggles exactly once -- which is why one key for both directions
                # never fights itself. Polled here, before the running check, so it can
                # turn the bot ON, not only off.
                # One hotkey per feature. Pressing a feature's key while it's already
                # running stops it; pressing it otherwise switches to that feature and
                # starts. That's what makes swapping bidding<->fishing a single tap,
                # and they can never both run, since they share one mouse.
                for vk, feat, on_flag in ((st["toggle_vk"], "auction", st["auction_key_on"]),
                                          (st["fish_vk"], "fishing", st["fish_key_on"])):
                    prev = self._key_down.get(feat, False)
                    if not vk or not on_flag:
                        self._key_down[feat] = False
                        self._key_seen[feat] = vk
                        continue
                    if self._key_seen.get(feat, -1) != vk:
                        # Binding just changed; the keypress that SET it is still held.
                        # Seed as already-down so it doesn't count as a toggle.
                        self._key_seen[feat] = vk
                        self._key_down[feat] = bool(win32api.GetAsyncKeyState(vk) & 0x8000)
                        continue
                    down = bool(win32api.GetAsyncKeyState(vk) & 0x8000)
                    if down and not prev:
                        if game_focused():
                            on = not (st["running"] and st["feature"] == feat)
                            if not on or st["feature"] != feat:
                                self._set_hold(False)   # never switch with mouse held
                            self.state.set(running=on, feature=feat,
                                           status=(f"{feat} started (key)" if on
                                                   else f"{feat} stopped (key)"))
                            st = self.state.snapshot()
                        else:
                            self.state.set(status="hotkey ignored - focus Roblox first")
                    self._key_down[feat] = down

                if not st["running"]:
                    hist.clear()
                    armed = True
                    # Forget when the last cue was. Otherwise pressing START again
                    # inherits a timestamp from minutes ago, the idle test is already
                    # blown the moment it wakes up, and it stops before it can look.
                    self._last_cue = 0.0
                    self._max_gap = 0.0
                    self._out_armed = True
                    self._fish_active = False
                    self._set_hold(False)     # never leave the button stuck down
                    time.sleep(0.05)
                    continue

                panic = panic_pressed()
                if panic:
                    self._set_hold(False)
                    self.state.set(running=False, status=f"stopped ({panic})")
                    continue

                # --- fishing takes over the loop entirely when it's the active feature
                if st["feature"] == "fishing":
                    if fish_region is None:
                        self.state.set(running=False,
                                       status="fishing: calibrate the reel bar first")
                        continue
                    frames += 1
                    tick = time.perf_counter()
                    if tick - fps_t >= 1.0:
                        self.state.set(fps=frames / (tick - fps_t))
                        frames, fps_t = 0, tick
                    self._fishing_step(sct, fish_region, st)
                    continue

                # Hold the cursor on BID with a real move every frame, alternating one
                # pixel. A cursor parked perfectly still sends the game nothing, and a
                # game tracking raw input can lose track of where it is -- which is why
                # shaking the mouse by hand made clicks land. A steady 1px jiggle is
                # what a hand resting on a mouse looks like, and it keeps the game's
                # pointer pinned to the button so every press hit-tests there.
                # Dry run stays hands-off: it never clicks, so it never grabs the mouse.
                if not st["dry_run"]:
                    jiggle ^= 1
                    move_to(bid_xy[0] + jiggle, bid_xy[1])

                # Count every loop, both modes: a permanent "0 fps" reads as a dead bot.
                frames += 1
                tick = time.perf_counter()
                if loop_t:
                    self.state.set(loop_ms=(tick - loop_t) * 1000.0)
                loop_t = tick
                if tick - fps_t >= 1.0:
                    self.state.set(fps=frames / (tick - fps_t))
                    frames, fps_t = 0, tick

                if st["mode"] == "outline":
                    self._outline_step(sct, obox, st)
                    continue

                # Timestamp AFTER the grab: it blocks until the next vsync, so a
                # timestamp taken before it would label the frame up to 16ms early
                # and poison every velocity fit built from it.
                raw = np.asarray(sct.grab(region))
                now = time.perf_counter()
                fresh = detect.find_zone(raw)
                cands = detect.find_markers(raw)

                # The zone is fixed for the whole round, and anything crossing it --
                # the marker, a speck, a grey block -- can only ever hide part of it.
                # So the widest reading seen this round is the truest one; later,
                # narrower looks at the same zone are occlusions, not new information.
                if fresh is not None and (
                    green_seen is None or (fresh[1] - fresh[0]) > (green_seen[1] - green_seen[0])
                ):
                    green_seen = fresh

                if not cands or green_seen is None:
                    hist.clear()
                    if now - last_seen > MARKER_LOST_REARM:
                        armed = True
                        green_seen = None
                        # Report the measurement, not just a verdict: a bare "NO" can't
                        # distinguish "nothing on screen" from "threshold set too high",
                        # and that difference is the whole diagnosis.
                        a = raw[:, :, :3]
                        lo = a.min(axis=2)
                        bright = int(lo.max())
                        sat = int((a.max(axis=2) - lo).max())
                        line = ("line yes" if cands else
                                f"line NO max{bright}/{detect.WHITE_MIN_BRIGHT}")
                        zone = ("zone yes" if fresh else
                                f"zone NO sat{sat}/{detect.ZONE_MIN_SAT}")
                        self.state.set(status=f"{line}  {zone}")
                    time.sleep(0.005)  # nothing happening; don't burn a core
                    continue
                green = green_seen

                # Several specks can pass the geometry test at once. Once the line is
                # tracked, believe the candidate nearest to where it should be by now;
                # a speck elsewhere on the bar can't drag the track off the line.
                # A track we haven't updated in several frames is dead -- clicking and
                # watching where it landed blocks for ~250ms, and extrapolating across
                # that gap opens the gate wide enough to swallow anything on the bar.
                if hist and now - hist[-1][0] > 4 * frame_dt:
                    hist.clear()
                    gaps.clear()

                tallest = max(cands, key=lambda c: c[2])[0]
                if len(hist) < 2:
                    x = tallest          # no track yet: nothing to predict from
                else:
                    v_now = _slope(hist)
                    dt_pred = now - hist[-1][0]
                    x_pred = hist[-1][1] + v_now * dt_pred
                    gate = 25.0 + abs(v_now) * dt_pred * 0.5
                    best = min(cands, key=lambda c: abs(c[0] - x_pred))
                    if abs(best[0] - x_pred) <= gate:
                        x = best[0]
                    else:
                        # The line went somewhere it couldn't have travelled: the round
                        # reset. Start a fresh track from the tallest candidate.
                        x = tallest
                        hist.clear()
                        gaps.clear()
                        armed = True
                        green_seen = fresh or green_seen
                        green = green_seen

                last_seen = now

                # We can poll faster than the screen redraws, so the same rendered
                # frame can be read twice. An identical centroid means identical
                # pixels: appending it would flatten the slope, so drop it.
                if hist and x == hist[-1][1]:
                    continue

                if hist:
                    gap = now - hist[-1][0]
                    if 0.001 < gap < 0.05:
                        gaps.append(gap)
                        if len(gaps) >= 5:
                            frame_dt = float(np.median(gaps))

                hist.append((now, x))
                while hist and now - hist[0][0] > SAMPLE_WINDOW:
                    hist.popleft()

                if not armed and x < width * 0.2 and now - fired_at > REARM_GAP:
                    armed = True

                if len(hist) < MIN_SAMPLES:
                    continue

                v, rms = _fit(hist)
                if abs(v) < MIN_SPEED:
                    self.state.set(status="bar idle")
                    continue

                if rms > MAX_FIT_RMS:
                    self.state.set(status=f"unstable ({rms:.0f}px off a line)")
                    continue

                target = green[2]
                self.state.set(status=f"tracking  v={v:.0f}px/s")

                if not armed or now - fired_at < REARM_GAP:
                    continue

                dt = (target - x) / v
                if dt <= 0:      # moving away, or already past the centre
                    continue

                fire_in = dt - st["lead_ms"] / 1000.0

                # Committing only when fire_in is tiny would be a bug here: the next
                # grab can't return for a whole frame, so we'd sleep straight past the
                # shot. Once no fresher sample can arrive before t_fire, commit and spin.
                if fire_in > frame_dt * 1.1 + 0.002:
                    continue

                if fire_in < 0:
                    # Already late. Firing now lands v*|fire_in| px past centre --
                    # still a hit if that fits inside the green zone, so take it.
                    err_px = abs(v * fire_in)
                    half = (green[1] - green[0]) / 2.0
                    if err_px > half * 0.8:
                        armed = False
                        fired_at = now
                        with self.state.lock:
                            self.state.misses += 1
                            self.state.last = f"skipped: {err_px:.0f}px late, zone is {half:.0f}px"
                        continue

                t_fire = now + max(0.0, fire_in)
                while time.perf_counter() < t_fire:
                    pass         # sleep() is far too coarse; spin the last ~2ms

                # Stamp the press, not the release: _press blocks for the hold, and
                # the press is the instant the shot is actually taken.
                t_click = time.perf_counter()
                if not st["dry_run"]:
                    _press(self.config.get("click_hold_ms", CLICK_HOLD * 1000) / 1000.0)
                armed = False
                fired_at = t_click

                self._report(sct, region, t_click, v, target, x, st)

    def _set_hold(self, want):
        """Press or release the left button, only on a change.

        Tracked rather than re-sent every frame: the game wants a held button, and
        re-issuing LEFTDOWN 60x/second reads as click spam. Also the reason every exit
        path calls _set_hold(False) -- a mouse left stuck down is the worst way to
        hand control back to the player.
        """
        if want == self._mouse_down:
            return
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN if want
                             else win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self._mouse_down = want
        self.state.set(fish_hold=want)

    def _fishing_step(self, sct, region, st):
        """One frame of the reel minigame: look, decide, hold or release."""
        img = np.asarray(sct.grab(region))[:, :, :3]
        now = time.perf_counter()
        grey = fishing.find_grey(img)
        gold = fishing.find_gold(img)

        if grey is None or gold is None:
            # No reel bar on screen. Let go of the mouse and wait for the next cast.
            self._set_hold(False)
            if self._fish_active and now - self._fish_seen > FISH_END_GAP:
                self._fish_active = False
                secs = self._fish_seen - self._fish_start
                pct = 100.0 * self._fish_on / max(1, self._fish_n)
                done = f"catch done - {secs:.1f}s, on target {pct:.0f}%"
                # Same switch as bidding's idle stop: with auto-stop off it stays armed
                # for the next cast instead of handing the mouse back.
                if st["auto_stop"]:
                    self.state.set(running=False, status=done)
                else:
                    self.state.set(status=f"{done}  -  waiting for next cast")
            elif not self._fish_active:
                self.state.set(status="fishing - waiting for you to cast")
            return

        if not self._fish_active:          # a new catch just started
            self._fish_active = True
            self._fish_start = now
            self._fish_on = self._fish_n = 0
            self._ctrl.reset()
        self._fish_seen = now

        grey_c, grey_w = grey
        hold = self._ctrl.decide(grey_c, gold, now)
        if not st["dry_run"]:
            self._set_hold(hold)

        err = abs(grey_c - gold)
        margin = max(1.0, grey_w / 2.0 - 15.0)   # slack before the gold leaves the box
        self._fish_n += 1
        self._fish_on += (err <= margin)
        pct = 100.0 * self._fish_on / self._fish_n
        self.state.set(fish_err=err, fish_margin=margin, fish_on_pct=pct,
                       fish_secs=now - self._fish_start,
                       status=f"REELING  off {err:+.0f}px / {margin:.0f}  "
                              f"on-target {pct:.0f}%  {'HOLD' if hold else 'release'}")

    def _outline_step(self, sct, obox, st):
        """Watch the BID button and click the instant its outline lights up.

        No prediction here: the game is doing the timing, so the cue *is* the moment.
        Fires on the rising edge only, and re-arms once the outline clearly goes out --
        the gap between the two thresholds stops a score hovering at the line from
        machine-gunning the button.
        """
        # Catching the cue while it is still dim fires earlier: if the outline brightens
        # in over a frame or two, waiting for full white throws that time away. Too low
        # and the unlit outline trips it constantly -- hence a live knob, not a guess.
        detect.OUTLINE_MIN_BRIGHT = st["cue_bright"]
        img = np.asarray(sct.grab(obox))
        t_scan = time.perf_counter()
        run = detect.outline_run(img)
        scan_ms = (time.perf_counter() - t_scan) * 1000.0
        on = st["outline_min_run"]

        # The peak is the diagnostic that matters. If it stays near 0 through a round
        # where the outline visibly lit, the bot is not seeing the cue at all -- wrong
        # box or too high a brightness bar -- and no amount of threshold fiddling helps.
        # If it climbs to roughly the button's width, the cue is being seen.
        if run > st["outline_peak"]:
            self.state.set(outline_peak=run)

        if run >= on and self._out_armed:
            self._out_armed = False
            # The gap between consecutive cues IS the round length, so recording the
            # longest one turns the stop delay from a guess into a measurement: it's
            # the number the "stop after" slider has to clear to be safe.
            t_cue = time.perf_counter()
            if self._last_cue and t_cue - self._last_cue > self._max_gap:
                self._max_gap = t_cue - self._last_cue
                self.state.set(max_gap=self._max_gap)
            self._last_cue = t_cue
            # Which window is actually focused decides whether this click presses BID
            # or is swallowed activating the window. Worth recording, not assuming.
            try:
                fg = win32gui.GetWindowText(win32gui.GetForegroundWindow())[:16]
            except Exception:
                fg = "?"
            self.state.set(focus=fg, last_fire=time.perf_counter())
            if not st["dry_run"]:
                _press(self.config.get("click_hold_ms", CLICK_HOLD * 1000) / 1000.0)
            with self.state.lock:
                self.state.hits += 1
                self.state.last = (f"{'DRY' if st['dry_run'] else 'FIRE'} {run}px "
                                   f"-> focus: {fg or '(none)'}")
        elif run < on * 0.6:
            self._out_armed = True

        # Stop by itself once the bidding is over -- the same end as the panic key,
        # reached naturally.
        #
        # The test is "the cue stopped happening", NOT "the panel looks gone". Deciding
        # it from pixels needs the auction panel to be distinguishable from the world
        # behind it, and it isn't: dark dirt and grey concrete are also dark and flat,
        # so the panel never appears to leave. Cues are unambiguous -- they only exist
        # while an auction is running, and one arrives every round.
        #
        # Only after it has actually bid once, or starting the bot before opening an
        # auction would stop it before it ever saw anything.
        #
        # Read live off the config dict so the slider takes effect mid-auction, and
        # floor it at the longest round actually seen -- see GAP_MARGIN.
        now = time.perf_counter()
        idle = now - self._last_cue
        stop_after = max(float(self.config.get("stop_after_s", IDLE_STOP)),
                         self._max_gap + GAP_MARGIN)
        armed_to_stop = st["auto_stop"] and st["hits"] and self._last_cue
        if armed_to_stop and idle > stop_after:
            self.state.set(running=False,
                           status=f"bidding done - stopped after {st['hits']} bid(s)")
            return

        left = (f"   idle {idle:.0f}/{stop_after:.0f}s" if armed_to_stop else
                "   auto-stop OFF" if not st["auto_stop"] else "")
        self.state.set(status=f"white line {run}px / {on}px   "
                              f"peak {st['outline_peak']}px{left}",
                       run_px=run, scan_ms=scan_ms, armed=self._out_armed)

    def _report(self, sct, region, t_click, v, target, x_seen, st):
        tag = "DRY" if st["dry_run"] else "FIRE"
        note = f"{tag}  v={v:.0f}px/s  saw x={x_seen:.1f}  aim={target:.1f}"

        landed = None if st["dry_run"] else self._observe_landing(sct, region, t_click, target)
        if landed is not None:
            ms = landed / v * 1000.0
            note += f"  landed {landed:+.1f}px ({ms:+.1f}ms)"
            if st["auto_tune"]:
                # Landed right of centre => we were late => need more lead.
                new_lead = float(np.clip(st["lead_ms"] + 0.5 * ms, 0.0, 150.0))
                self.state.set(lead_ms=new_lead)
                note += f"  lead->{new_lead:.1f}"

        with self.state.lock:
            self.state.hits += 1
            self.state.last = note

    def _observe_landing(self, sct, region, t_click, target):
        """If the bar freezes where the game registered the click, that position is
        ground truth for how far off we were. Returns None if it doesn't freeze."""
        samples = []
        while time.perf_counter() < t_click + 0.22:
            m = detect.find_marker(np.asarray(sct.grab(region)))
            t = time.perf_counter()
            if m and t - t_click > 0.03:
                samples.append((t, m[0]))
        if len(samples) < 4 or abs(_slope(samples)) > 80:
            return None  # still moving, so it didn't freeze: no usable signal
        return float(np.mean([s[1] for s in samples])) - target
