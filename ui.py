"""Bid bar bot — control window. Run this file.

    python ui.py
"""

import ctypes

# Must happen before anything reads the screen or moves the cursor: without it
# Windows display scaling offsets captured pixels from real cursor coordinates.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

# Reaction is a race against a ~16ms frame, so being descheduled for a few ms at the
# wrong moment is a real cost. HIGH (not REALTIME, which can starve input handling).
# restype matters: the process pseudo-handle is pointer-sized, and letting ctypes
# default it to a 32-bit int truncates it and the call silently fails.
try:
    _k32 = ctypes.windll.kernel32
    _k32.GetCurrentProcess.restype = ctypes.c_void_p
    _k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _k32.SetPriorityClass(_k32.GetCurrentProcess(), 0x00000080)  # HIGH_PRIORITY_CLASS
except Exception:
    pass

import json
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox

import mss
import win32api

from calibrate import auto_calibrate
from engine import Engine, State, bid_box, panic_pressed

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
BG = "#1e1e1e"
FG = "#e8e8e8"


def claim_single_instance():
    """False if a Bid Bot is already running.

    Stacked copies are worse than they look: each one parks the cursor and clicks the
    same button, so they fight each other -- and closing the top one just reveals the
    next, which reads as a window that refuses to close.
    """
    k = ctypes.windll.kernel32
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateMutexW(None, False, "BidBot_SingleInstance_9f3a")
    return k.GetLastError() != 183   # ERROR_ALREADY_EXISTS


_VK_SPECIAL = {0x20: "Space", 0x0D: "Enter", 0x09: "Tab", 0x08: "Backspace",
               0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
               0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'"}


def vk_name(vk):
    """A readable label for a virtual-key code."""
    if not vk:
        return "(none)"
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:   # 0-9, A-Z
        return chr(vk)
    if 0x70 <= vk <= 0x87:                          # F1-F24
        return f"F{vk - 0x6F}"
    return _VK_SPECIAL.get(vk, f"#{vk}")


# Keys a toggle can't be: mouse buttons, and the two panic keys (reserved for the
# emergency stop). Binding the toggle to a panic key would make one press both stop
# and start, which is exactly the fight the user was worried about.
_RESERVED_VK = {0x01, 0x02, 0x04, 0x05, 0x06, 0x77, 0x50}   # mouse, F8, P


def load_store():
    """-> {"profiles": {name: cfg}, "active": name}.

    One saved setup per auction location: the junk yard and the shop front put their
    bar and button in different places, and re-dragging boxes every time you move is
    what makes the tool feel broken.
    """
    if not os.path.exists(CONFIG_PATH):
        return {"profiles": {}, "active": None}
    with open(CONFIG_PATH) as f:
        raw = json.load(f)
    if "profiles" in raw:
        return raw
    # migrate the old single flat config so an existing setup isn't thrown away
    return {"profiles": {"default": raw}, "active": "default"}


def save_store(store):
    with open(CONFIG_PATH, "w") as f:
        json.dump(store, f, indent=2)


def _ago(ts):
    """'calibrated 5m ago'. Relative, because 'is this stale?' is the only question
    anyone asks of it -- a wall-clock time would need doing the sum yourself."""
    if not ts:
        return "never calibrated"
    s = max(0, time.time() - ts)
    if s < 60:
        return "calibrated just now"
    if s < 3600:
        return f"calibrated {int(s // 60)}m ago"
    if s < 86400:
        return f"calibrated {int(s // 3600)}h ago"
    return f"calibrated {int(s // 86400)}d ago"


def run_setup(parent):
    """Two drags: the BID button, then the bar. -> cfg dict, or None if cancelled.

    Primary monitor only. Spanning the whole virtual desktop centred the instructions
    on the seam between the two screens. The primary always sits at (0,0) in screen
    coordinates, so canvas coordinates are already screen coordinates.
    """
    from PIL import Image, ImageTk

    pw = win32api.GetSystemMetrics(0)   # SM_CXSCREEN -- primary monitor, not virtual
    ph = win32api.GetSystemMetrics(1)   # SM_CYSCREEN

    # A frozen screenshot instead of a see-through window. A translucent overlay dims
    # the instructions and the game equally, so making the text readable meant hiding
    # what you're aiming at. An opaque snapshot shows the game at full clarity AND lets
    # the text be solid -- and the things being boxed don't move anyway.
    with mss.mss() as sct:
        shot = sct.grab({"left": 0, "top": 0, "width": pw, "height": ph})
    base = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    out = {}
    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.geometry(f"{pw}x{ph}+0+0")
    win.attributes("-topmost", True)
    cv = tk.Canvas(win, width=pw, height=ph, highlightthickness=0, bg="black",
                   cursor="crosshair")
    cv.pack()
    photo = ImageTk.PhotoImage(base)
    cv.create_image(0, 0, anchor="nw", image=photo)
    cv.image = photo  # keep a reference or tkinter garbage-collects it away

    # Both steps are drags, and the click point is the button box's centre rather than
    # a separate click: one less thing to aim at, and it can't land on a box corner.
    STEPS = [
        ("bid_box", "#4da6ff",
         "STEP 1 of 2   -   Drag a box around the BID BUTTON   -   loose is fine, just "
         "make sure the whole button, white outline and all, is inside"),
        ("track", "#00ff66",
         "STEP 2 of 2   -   Drag a box around the DARK BAR the white line slides along"
         "   -   Esc to skip (only used by Timing mode)"),
    ]
    ph_ = {"i": 0, "at": 0.0}
    drag = {"x": 0, "y": 0, "rect": None}

    # A solid banner behind the text, so it reads over whatever the game is showing.
    cv.create_rectangle(0, 24, pw, 116, fill="#0d0d0d", outline=STEPS[0][1], width=3,
                        tags="banner")
    tip = cv.create_text(pw // 2, 70, fill="white", font=("Segoe UI", 20, "bold"),
                         width=pw - 160, justify="center", text=STEPS[0][2])

    def on_press(e):
        drag.update(x=e.x, y=e.y)
        drag["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y,
                                           outline=STEPS[ph_["i"]][1], width=2)

    def on_move(e):
        if drag["rect"]:
            cv.coords(drag["rect"], drag["x"], drag["y"], e.x, e.y)

    def on_release(e):
        if not drag["rect"] or time.monotonic() - ph_["at"] < 0.3:
            return
        l, t = min(drag["x"], e.x), min(drag["y"], e.y)
        w, h = abs(e.x - drag["x"]), abs(e.y - drag["y"])
        if w < 20 or h < 8:
            cv.delete(drag["rect"])
            drag["rect"] = None
            return  # stray click, keep waiting for a real drag
        out[STEPS[ph_["i"]][0]] = {"left": l, "top": t, "width": w, "height": h}
        cv.itemconfig(drag["rect"], width=3)
        drag["rect"] = None
        ph_["i"] += 1
        ph_["at"] = time.monotonic()
        if ph_["i"] >= len(STEPS):
            finish()
        else:
            cv.itemconfig(tip, text=STEPS[ph_["i"]][2])
            cv.itemconfig("banner", outline=STEPS[ph_["i"]][1])

    def finish():
        b = out["bid_box"]
        out["bid_xy"] = [b["left"] + b["width"] // 2, b["top"] + b["height"] // 2]
        out.setdefault("track", {"left": b["left"], "top": max(0, b["top"] - 90),
                                 "width": b["width"], "height": 60})
        win.destroy()

    def on_esc(e):
        if "bid_box" in out:
            finish()          # the bar box is optional; keep what we have
        else:
            win.destroy()     # nothing useful yet: cancel outright

    cv.bind("<ButtonPress-1>", on_press)
    cv.bind("<B1-Motion>", on_move)
    cv.bind("<ButtonRelease-1>", on_release)
    win.bind("<Escape>", on_esc)
    win.focus_force()
    parent.wait_window(win)

    return out if "bid_xy" in out else None


def drag_box(parent, prompt, colour="#4da6ff"):
    """Drag one box over a frozen screenshot. -> {left,top,width,height} or None."""
    from PIL import Image, ImageTk

    pw, ph = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    with mss.mss() as sct:
        shot = sct.grab({"left": 0, "top": 0, "width": pw, "height": ph})
    base = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    out, drag = {}, {"x": 0, "y": 0, "rect": None}
    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.geometry(f"{pw}x{ph}+0+0")
    win.attributes("-topmost", True)
    cv = tk.Canvas(win, width=pw, height=ph, highlightthickness=0, bg="black",
                   cursor="crosshair")
    cv.pack()
    photo = ImageTk.PhotoImage(base)
    cv.create_image(0, 0, anchor="nw", image=photo)
    cv.image = photo
    cv.create_rectangle(0, 24, pw, 116, fill="#0d0d0d", outline=colour, width=3)
    cv.create_text(pw // 2, 70, fill="white", font=("Segoe UI", 19, "bold"),
                   width=pw - 160, justify="center", text=prompt)

    def press(e):
        drag.update(x=e.x, y=e.y)
        drag["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline=colour, width=3)

    def move(e):
        if drag["rect"]:
            cv.coords(drag["rect"], drag["x"], drag["y"], e.x, e.y)

    def release(e):
        w, h = abs(e.x - drag["x"]), abs(e.y - drag["y"])
        if w < 30 or h < 10:
            return
        out.update(left=min(drag["x"], e.x), top=min(drag["y"], e.y), width=w, height=h)
        win.destroy()

    cv.bind("<ButtonPress-1>", press)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", release)
    win.bind("<Escape>", lambda e: win.destroy())
    win.focus_force()
    parent.wait_window(win)
    return out or None


def run_editor(parent, config):
    """Show where the bot looks and clicks, over a frozen shot of the screen. Drag the
    boxes or the crosshair to fix them. -> updated config, or None if cancelled.

    Frozen rather than live: an opaque snapshot keeps the game perfectly readable and
    every pixel clickable, which a translucent window can't do -- and the things being
    positioned don't move anyway.
    """
    from PIL import Image, ImageTk

    pw, ph = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    with mss.mss() as sct:
        shot = sct.grab({"left": 0, "top": 0, "width": pw, "height": ph})
    base = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    t, ob = config["track"], bid_box(config)
    boxes = [
        {"key": "track", "color": "#00ff66",
         "label": "BAR - the strip the line slides along (Timing mode only)",
         "box": [t["left"], t["top"], t["left"] + t["width"], t["top"] + t["height"]]},
        {"key": "bid_box", "color": "#4da6ff",
         "label": "BID BUTTON - must contain the white outline (loose is fine)",
         "box": [ob["left"], ob["top"], ob["left"] + ob["width"], ob["top"] + ob["height"]]},
    ]
    bid = list(config["bid_xy"])
    drag = {"mode": None, "data": None}
    out = {}

    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.geometry(f"{pw}x{ph}+0+0")
    win.attributes("-topmost", True)
    cv = tk.Canvas(win, width=pw, height=ph, highlightthickness=0, bg="black")
    cv.pack()
    photo = ImageTk.PhotoImage(base)
    cv.create_image(0, 0, anchor="nw", image=photo)
    cv.image = photo  # keep a reference or tkinter garbage-collects it away

    def handles(box):
        x0, y0, x1, y1 = box
        mx, my = (x0 + x1) // 2, (y0 + y1) // 2
        return [(x0, y0), (mx, y0), (x1, y0), (x1, my),
                (x1, y1), (mx, y1), (x0, y1), (x0, my)]

    def draw():
        cv.delete("ov")
        for item in boxes:
            x0, y0, x1, y1 = item["box"]
            c = item["color"]
            cv.create_rectangle(x0, y0, x1, y1, outline=c, width=3, tags="ov")
            cv.create_text(x0 + 2, y0 - 16, anchor="w", fill=c, tags="ov",
                           font=("Segoe UI", 11, "bold"), text=item["label"])
            for hx, hy in handles(item["box"]):
                cv.create_rectangle(hx - 5, hy - 5, hx + 5, hy + 5,
                                    fill=c, outline="black", tags="ov")
        bx, by = bid
        cv.create_line(bx - 24, by, bx + 24, by, fill="#ff3b30", width=3, tags="ov")
        cv.create_line(bx, by - 24, bx, by + 24, fill="#ff3b30", width=3, tags="ov")
        cv.create_oval(bx - 10, by - 10, bx + 10, by + 10, outline="#ff3b30", width=3, tags="ov")
        cv.create_text(bx + 28, by - 16, anchor="w", fill="#ff3b30", tags="ov",
                       font=("Segoe UI", 11, "bold"), text="BID CLICK - drag me")
        tb, bb = boxes[0]["box"], boxes[1]["box"]
        info.config(text=f"bar {tb[2]-tb[0]}x{tb[3]-tb[1]} at ({tb[0]},{tb[1]})   "
                         f"button {bb[2]-bb[0]}x{bb[3]-bb[1]} at ({bb[0]},{bb[1]})   "
                         f"click ({bx},{by})")

    def on_press(e):
        # The crosshair is small and sits on top of a box, so it gets first refusal.
        if (e.x - bid[0]) ** 2 + (e.y - bid[1]) ** 2 <= 24 ** 2:
            drag.update(mode="bid", data=None)
            return
        for bi, item in enumerate(boxes):
            for i, (hx, hy) in enumerate(handles(item["box"])):
                if abs(e.x - hx) <= 8 and abs(e.y - hy) <= 8:
                    drag.update(mode="handle", data=(bi, i))
                    return
        for bi, item in enumerate(boxes):
            x0, y0, x1, y1 = item["box"]
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                drag.update(mode="move", data=(bi, e.x - x0, e.y - y0))
                return
        drag.update(mode=None, data=None)

    def on_move(e):
        if drag["mode"] == "bid":
            bid[0], bid[1] = e.x, e.y
        elif drag["mode"] == "move":
            bi, ox, oy = drag["data"]
            box = boxes[bi]["box"]
            w, h = box[2] - box[0], box[3] - box[1]
            box[0], box[1] = e.x - ox, e.y - oy
            box[2], box[3] = box[0] + w, box[1] + h
        elif drag["mode"] == "handle":
            bi, i = drag["data"]
            box = boxes[bi]["box"]
            if i in (0, 6, 7):
                box[0] = min(e.x, box[2] - 30)
            if i in (0, 1, 2):
                box[1] = min(e.y, box[3] - 10)
            if i in (2, 3, 4):
                box[2] = max(e.x, box[0] + 30)
            if i in (4, 5, 6):
                box[3] = max(e.y, box[1] + 10)
        else:
            return
        draw()

    def save():
        out["cfg"] = dict(config)
        for item in boxes:
            b = item["box"]
            out["cfg"][item["key"]] = {"left": b[0], "top": b[1],
                                       "width": b[2] - b[0], "height": b[3] - b[1]}
        out["cfg"]["bid_xy"] = [bid[0], bid[1]]
        win.destroy()

    bar = tk.Frame(win, bg="#1e1e1e")
    tk.Button(bar, text="Save", command=save, bg="#2e7d32", fg="white", relief="flat",
              font=("Segoe UI", 11, "bold"), width=10).pack(side="left", padx=4, pady=4)
    tk.Button(bar, text="Cancel", command=win.destroy, bg="#444", fg="white",
              relief="flat", font=("Segoe UI", 11), width=10).pack(side="left", padx=4, pady=4)
    info = tk.Label(bar, text="", bg="#1e1e1e", fg="#ddd", font=("Consolas", 10))
    info.pack(side="left", padx=12)
    cv.create_window(20, 20, anchor="nw", window=bar)

    cv.bind("<ButtonPress-1>", on_press)
    cv.bind("<B1-Motion>", on_move)
    win.bind("<Escape>", lambda e: win.destroy())
    draw()
    win.focus_force()
    parent.wait_window(win)
    return out.get("cfg")


def no_activate(win):
    """Let this window be clicked without it taking focus from the game.

    Windows gives the first click on an unfocused app to *activating* it, not to what's
    under the cursor. So touching a slider here used to hand focus to the bot, and the
    next BID click was spent re-activating Roblox instead of pressing the button --
    one wasted click every time you touched the UI. WS_EX_NOACTIVATE means this window
    never takes focus, so the game keeps it and every click counts.
    """
    try:
        win.update_idletasks()
        u = ctypes.windll.user32
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
        getl = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
        setl = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
        getl.restype = ctypes.c_ssize_t
        setl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        setl(hwnd, GWL_EXSTYLE, getl(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE)
        return True
    except Exception:
        return False


def run_setup_for(app):
    """Hide the control window, run setup, bring it back."""
    app.root.withdraw()
    app.root.update()
    time.sleep(0.15)   # let this window vanish before we photograph the screen
    try:
        return run_setup(app.root)
    finally:
        app.root.deiconify()


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Bid Bot")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.store = load_store()
        self.config = self.store["profiles"].get(self.store.get("active"))
        c = self.config or {}
        self.state = State(lead_ms=c.get("lead_ms", 25.0),
                           mode=c.get("mode", "outline"),
                           outline_min_run=c.get("outline_min_run", 150))
        self.engine = None
        self.countdown = 0
        self.cal_running = False
        self.cal_stopped = None

        self._build()
        if self.config:
            self._start_engine()
        else:
            self.root.after(200, self.auto_cal)

    def _build(self):
        # Three tabs sharing one content cell, plus the calibrate view, which takes over
        # the whole window (it's a mode, not a dialog -- it needs room to explain itself).
        self.tabbar = tk.Frame(self.root, bg="#141414")
        self.tabbar.grid(row=0, column=0, sticky="we")
        self.tabs, self.tabbtn = {}, {}
        for key, label in (("bid", "Bidding"), ("fish", "Fishing"), ("keys", "Hotkeys")):
            b = tk.Button(self.tabbar, text=label, relief="flat", bd=0,
                          bg="#2a2a2a", fg=FG, activebackground="#3d6a99",
                          font=("Segoe UI", 10, "bold"),
                          command=lambda k=key: self.show_tab(k))
            b.pack(side="left", fill="x", expand=True, padx=1, pady=1, ipady=3)
            self.tabbtn[key] = b

        self.main = tk.Frame(self.root, bg=BG)
        self.main.grid(row=1, column=0, sticky="nsew")
        # The stacked tab frames share one cell; let it fill so a short tab still
        # stretches to the tallest tab's height and its bottom bar lines up.
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(0, weight=1)
        self.main.grid_columnconfigure(0, weight=1)
        for key in ("bid", "fish", "keys"):
            f = tk.Frame(self.main, bg=BG)
            f.grid(row=0, column=0, sticky="nsew")   # stacked; raised one at a time
            self.tabs[key] = f

        self.cal = tk.Frame(self.root, bg=BG)
        self.cal.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.cal.grid_remove()

        # Every tab's bottom bar registers its own "calibrated ago" label here: the tabs
        # are stacked frames, so each needs its own widget and poll() updates them all.
        self.callbls = []
        self._build_main(self.tabs["bid"])
        self._build_fish(self.tabs["fish"])
        self._build_keys(self.tabs["keys"])
        self._build_cal(self.cal)
        self._refresh_keys()
        self.on_key_flags()
        self.state.set(auto_stop=bool((self.config or {}).get("auto_stop", True)))
        self._refresh_autostop()
        self.show_tab("bid")
        no_activate(self.root)   # touching these sliders must not steal focus from the game
        self.poll()

    def _bottom_bar(self, r, row):
        """The strip every tab ends with: panic hint, calibration age, QUIT.

        Identical widgets in an identical place on all three tabs, so it stays where the
        hand expects it after a tab switch. QUIT especially: this window deliberately
        never takes focus, so the title-bar X is the system's to honour and not reliably
        ours -- the button is the way out, and hunting for it is not an option mid-run.
        """
        bottom = tk.Frame(r, bg=BG)
        tk.Label(bottom, text="F8 or P = emergency stop", bg=BG, fg="#e06c75",
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(bottom, text="QUIT", command=self.quit, bg="#8a2e2e", fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold")
                  ).pack(side="right", padx=(8, 0))
        lbl = tk.Label(bottom, text="", bg=BG, fg="#777", font=("Segoe UI", 8))
        lbl.pack(side="right")
        self.callbls.append(lbl)
        # An empty weighted row above soaks up the slack, so the bar sits at the BOTTOM
        # of the tab rather than just after whatever content that tab happens to have.
        # Without it the button lands at a different height on each tab, which defeats
        # the point of putting it in the same place.
        r.grid_rowconfigure(row, weight=1)
        # Span past the widest tab's column count. Spanning too few columns makes the
        # grid charge this bar's whole width to those columns alone and stack the rest
        # beside it, which silently widens the window.
        bottom.grid(row=row + 1, column=0, columnspan=9, sticky="swe", padx=10, pady=(0, 8))
        return bottom

    def show_tab(self, key):
        self.tabs[key].tkraise()
        for k, b in self.tabbtn.items():
            b.config(bg="#3d6a99" if k == key else "#2a2a2a")
        self.cur_tab = key

    def _build_fish(self, r):
        opts = dict(bg=BG, fg=FG, font=("Segoe UI", 10))
        self.fstatus = tk.Label(r, text="idle", width=36, anchor="w",
                                font=("Consolas", 10), bg="#111", fg="#4ec9b0")
        self.fstatus.grid(row=0, column=0, columnspan=2, sticky="we", padx=10, pady=(10, 6))

        self.fbtn = tk.Button(r, text="START FISHING", font=("Segoe UI", 15, "bold"),
                              bg="#2e7d32", fg="white", activebackground="#2e7d32",
                              relief="flat", command=self.toggle_fish)
        self.fbtn.grid(row=1, column=0, columnspan=2, sticky="we", padx=10, pady=4, ipady=8)

        tk.Label(r, justify="left", wraplength=300, bg=BG, fg="#aaa",
                 font=("Segoe UI", 9),
                 text="You cast. The moment the reel bar appears it takes over, holds "
                      "the grey box on the gold, and stops when the fish is caught."
                 ).grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 6))

        tk.Label(r, text="Grip (brake)", **opts).grid(row=3, column=0, sticky="w", padx=10)
        self.fgrip = tk.Scale(r, from_=400, to=2400, resolution=50, orient="horizontal",
                              bg=BG, fg=FG, troughcolor="#333", highlightthickness=0,
                              command=self.on_fish_tune)
        self.fgrip.set((self.config or {}).get("fish_accel", 900))
        self.fgrip.grid(row=3, column=1, sticky="we", padx=10)

        tk.Label(r, text="Steadiness", **opts).grid(row=4, column=0, sticky="w", padx=10)
        self.fdead = tk.Scale(r, from_=1, to=30, resolution=1, orient="horizontal",
                              bg=BG, fg=FG, troughcolor="#333", highlightthickness=0,
                              command=self.on_fish_tune)
        self.fdead.set((self.config or {}).get("fish_deadband", 4))
        self.fdead.grid(row=4, column=1, sticky="we", padx=10)

        self.fdry = tk.Checkbutton(r, text="Dry run (watch, don't touch the mouse)",
                                   variable=self.dry, command=self.on_flags,
                                   bg=BG, fg=FG, selectcolor="#111", activebackground=BG,
                                   activeforeground=FG, font=("Segoe UI", 9))
        self.fdry.grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 0))

        self.fdiag = tk.Label(r, text="", justify="left", anchor="nw",
                              font=("Consolas", 8), bg="#141414", fg="#7ec8a9")
        self.fdiag.grid(row=6, column=0, columnspan=2, sticky="we", padx=10, pady=6)

        tk.Button(r, text="Calibrate reel bar", command=self.setup_fish, bg="#2e5c8a",
                  fg="white", relief="flat", font=("Segoe UI", 10, "bold")
                  ).grid(row=7, column=0, columnspan=2, sticky="we", padx=10, pady=(2, 4),
                         ipady=3)
        self.fboxlbl = tk.Label(r, text="", bg=BG, fg="#777", font=("Consolas", 8))
        self.fboxlbl.grid(row=8, column=0, columnspan=2, sticky="w", padx=10)

        self._bottom_bar(r, row=9)
        r.grid_columnconfigure(1, weight=1)

    def _build_keys(self, r):
        opts = dict(bg=BG, fg=FG, font=("Segoe UI", 10))
        # These wrap-text labels span ALL FOUR columns. At columnspan=3 their width was
        # charged to columns 0-2 alone, which pushed column 3 -- the clear buttons --
        # off the right edge of the window.
        tk.Label(r, text="Hotkeys", bg=BG, fg="#4da6ff",
                 font=("Segoe UI", 13, "bold")).grid(row=0, column=0, columnspan=5,
                                                     sticky="w", padx=12, pady=(10, 2))
        tk.Label(r, justify="left", wraplength=285, bg=BG, fg="#aaa", font=("Segoe UI", 9),
                 text="One key each. Press it in game to start that mode, press again to "
                      "stop. Starting one stops the other, so you can swap instantly.\n"
                      "They only fire while Roblox is focused."
                 ).grid(row=1, column=0, columnspan=5, sticky="w", padx=12, pady=(0, 8))

        self.keyrows = {}
        for i, (which, label) in enumerate((("toggle_vk", "Bidding"),
                                            ("fish_vk", "Fishing"))):
            tk.Label(r, text=label, width=7, anchor="w", **opts).grid(
                row=2 + i, column=0, sticky="w", padx=(12, 0), pady=3)
            btn = tk.Button(r, text="(none)", width=10, relief="flat", bg="#333", fg=FG,
                            font=("Segoe UI", 10, "bold"),
                            command=lambda w=which: self.set_key(w))
            btn.grid(row=2 + i, column=1, sticky="w")
            # A readout, not a switch. Bound = live, cleared = off, and the tick just
            # reports which. The old "on" tickbox was a second control for the same
            # state, which is how you end up with a key that looks bound and does
            # nothing because the other switch is off.
            dot = tk.Label(r, text="✗", width=2, bg=BG, fg="#666",
                           font=("Segoe UI", 12, "bold"))
            dot.grid(row=2 + i, column=2, sticky="w", padx=(6, 0))
            clear = tk.Button(r, text="clear", relief="flat", bg="#333", fg=FG,
                              font=("Segoe UI", 8),
                              command=lambda w=which: self.clear_key(w))
            clear.grid(row=2 + i, column=3, sticky="w", padx=(4, 0))
            # Which mode is running right now. The two are mutually exclusive (they
            # share one mouse), so at most one of these ever reads active.
            live = tk.Label(r, text="inactive", anchor="w", width=8, bg=BG, fg="#666",
                            font=("Segoe UI", 8))
            live.grid(row=2 + i, column=4, sticky="w", padx=(6, 10))
            self.keyrows[which] = dict(btn=btn, dot=dot, live=live)

        # Its own frame, not the key-row grid: "Auto-stop" is wider than the "Bidding" /
        # "Fishing" labels, and sharing column 0 with them stretched every row -- which
        # pushed the clear buttons out past the window edge.
        asrow = tk.Frame(r, bg=BG)
        tk.Label(asrow, text="Auto-stop", **opts).pack(side="left")
        self.autostop = tk.Button(asrow, text="ON", width=6, relief="flat",
                                  font=("Segoe UI", 10, "bold"),
                                  command=self.toggle_autostop)
        self.autostop.pack(side="left", padx=(8, 0))
        tk.Label(asrow, text="Dry run", **opts).pack(side="left", padx=(16, 0))
        self.drybtn = tk.Button(asrow, text="OFF", width=6, relief="flat",
                                font=("Segoe UI", 10, "bold"),
                                command=self.toggle_dry)
        self.drybtn.pack(side="left", padx=(8, 0))
        asrow.grid(row=4, column=0, columnspan=5, sticky="w", padx=12, pady=(14, 2))
        tk.Label(r, justify="left", wraplength=285, bg=BG, fg="#aaa",
                 font=("Segoe UI", 9),
                 text="The bot stops itself when it's done - bidding when the cue stops "
                      "arriving, fishing when the catch ends. Turn auto-stop off to keep "
                      "going until you stop it yourself. Dry run watches without "
                      "touching the mouse."
                 ).grid(row=5, column=0, columnspan=5, sticky="w", padx=12, pady=(0, 2))

        # No status line down here any more: which mode is live now reads off the
        # active/inactive column beside each key, and the bindings are the buttons
        # themselves. Restating both in prose underneath was the same thing twice.
        self._bottom_bar(r, row=6)
        r.grid_columnconfigure(4, weight=1)

    def _build_cal(self, r):
        opts = dict(bg=BG, fg=FG, font=("Segoe UI", 10))
        tk.Label(r, text="Auto-calibrate", bg=BG, fg="#4da6ff",
                 font=("Segoe UI", 14, "bold")).grid(row=0, column=0, sticky="w",
                                                     padx=12, pady=(12, 2))
        self.cal_help = tk.Label(r, justify="left", wraplength=300, bg=BG, fg=FG,
                                 font=("Segoe UI", 9))
        self.cal_help.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

        self.cal_status = tk.Label(r, text="starting...", width=34, anchor="w",
                                   font=("Consolas", 9), bg="#111", fg="#4ec9b0")
        self.cal_status.grid(row=2, column=0, sticky="we", padx=12)

        box = tk.Frame(r, bg=BG)
        self.cal_btn_lbl = tk.Label(box, text="BID button   looking...", anchor="w",
                                    font=("Consolas", 10), bg=BG, fg="#888", width=28)
        self.cal_btn_lbl.pack(anchor="w")
        self.cal_bar_lbl = tk.Label(box, text="bar          looking...", anchor="w",
                                    font=("Consolas", 10), bg=BG, fg="#888", width=28)
        self.cal_bar_lbl.pack(anchor="w")
        self.cal_cue_lbl = tk.Label(box, text="cue test     waiting...", anchor="w",
                                    font=("Consolas", 10), bg=BG, fg="#888", width=28)
        self.cal_cue_lbl.pack(anchor="w")
        box.grid(row=3, column=0, sticky="w", padx=12, pady=10)

        tk.Button(r, text="Cancel", command=self.cancel_cal, bg="#8a2e2e", fg="white",
                  relief="flat", font=("Segoe UI", 11, "bold")
                  ).grid(row=4, column=0, sticky="we", padx=12, pady=(4, 12), ipady=4)
        r.grid_columnconfigure(0, weight=1)

    def _build_main(self, r):
        opts = dict(bg=BG, fg=FG, font=("Segoe UI", 10))

        self.status = tk.Label(r, text="idle", width=36, anchor="w",
                               font=("Consolas", 10), bg="#111", fg="#4ec9b0")
        self.status.grid(row=0, column=0, columnspan=2, sticky="we", padx=10, pady=(10, 6))

        self.btn = tk.Button(r, text="START", font=("Segoe UI", 16, "bold"),
                             bg="#2e7d32", fg="white", activebackground="#2e7d32",
                             relief="flat", command=self.toggle)
        self.btn.grid(row=1, column=0, columnspan=2, sticky="we", padx=10, pady=4, ipady=8)

        # Timing mode is gone from the UI: it never once worked on this setup, and a
        # dead switch next to a live one is worse than no switch. The engine still has
        # the predictive path if a round ever outruns the cue.
        self.mode = tk.StringVar(value="outline")

        self.tune_lbl = tk.Label(r, text="White line (px)", **opts)
        self.tune_lbl.grid(row=3, column=0, sticky="w", padx=10)
        self.lead = tk.Scale(r, from_=0, to=80, resolution=0.5)   # kept, never shown
        self.othr = tk.Scale(r, from_=40, to=600, resolution=10, orient="horizontal",
                             bg=BG, fg=FG, troughcolor="#333", highlightthickness=0,
                             command=self.on_othr)
        self.othr.set(self.state.outline_min_run)
        self.othr.grid(row=3, column=1, sticky="we", padx=10)

        self.cue_lbl = tk.Label(r, text="Cue at (dark<->white)", **opts)
        self.cue_lbl.grid(row=4, column=0, sticky="w", padx=10)
        self.cue = tk.Scale(r, from_=100, to=250, resolution=5, orient="horizontal",
                            bg=BG, fg=FG, troughcolor="#333", highlightthickness=0,
                            command=self.on_cue)
        self.cue.set(self.state.cue_bright)
        self.cue.grid(row=4, column=1, sticky="we", padx=10)

        # The one knob that actually affects reaction speed, so it gets a slider.
        tk.Label(r, text="Click hold (ms)", **opts).grid(row=5, column=0, sticky="w", padx=10)
        self.hold = tk.Scale(r, from_=0, to=80, resolution=5, orient="horizontal",
                             bg=BG, fg=FG, troughcolor="#333", highlightthickness=0,
                             command=self.on_hold)
        self.hold.set((self.config or {}).get("click_hold_ms", 40))
        self.hold.grid(row=5, column=1, sticky="we", padx=10)

        # How long a silence counts as "the auction is over". It can't be zero: the only
        # end signal is a cue that never comes, so the bot has to outwait one round
        # before it can tell the end from the pause between bids. Set it just above the
        # "longest round" figure in the diagnostics.
        tk.Label(r, text="Stop after (s idle)", **opts).grid(row=6, column=0, sticky="w", padx=10)
        self.stopafter = tk.Scale(r, from_=2, to=20, resolution=1, orient="horizontal",
                                  bg=BG, fg=FG, troughcolor="#333", highlightthickness=0,
                                  command=self.on_stopafter)
        self.stopafter.set((self.config or {}).get("stop_after_s", 10))
        self.stopafter.grid(row=6, column=1, sticky="we", padx=10)

        self.dry = tk.BooleanVar(value=True)
        self.auto = tk.BooleanVar(value=False)   # auto-tune only meant anything for lead
        cb = dict(bg=BG, fg=FG, selectcolor="#111", activebackground=BG,
                  activeforeground=FG, font=("Segoe UI", 9))
        tk.Checkbutton(r, text="Dry run (no clicks)", variable=self.dry,
                       command=self.on_flags, **cb).grid(row=7, column=0, sticky="w", padx=8)
        self.autocb = tk.Checkbutton(r, text="", variable=self.auto, **cb)  # never shown

        self.counts = tk.Label(r, text="shots 0   skipped 0   0 fps", **opts)
        self.counts.grid(row=8, column=0, columnspan=2, sticky="w", padx=10)

        self.last = tk.Label(r, text="-", width=46, anchor="w", justify="left",
                             font=("Consolas", 8), bg=BG, fg="#999")
        self.last.grid(row=9, column=0, columnspan=2, sticky="we", padx=10)

        self.bidlbl = tk.Label(r, text="", bg=BG, fg="#999", font=("Consolas", 8))
        self.bidlbl.grid(row=10, column=0, columnspan=2, sticky="w", padx=10)

        # Auto-calibrate gets the wide, obvious button: it is the easy path.
        tk.Button(r, text="Auto-calibrate", command=self.auto_cal,
                  bg="#2e5c8a", fg="white", relief="flat", font=("Segoe UI", 10, "bold")
                  ).grid(row=11, column=0, columnspan=2, sticky="we", padx=10, pady=(8, 2),
                         ipady=3)

        small = tk.Frame(r, bg=BG)
        tk.Button(small, text="Setup by hand", command=self.setup, bg="#333", fg=FG,
                  relief="flat", font=("Segoe UI", 8)).pack(side="left")
        tk.Button(small, text="Show / edit regions", command=self.edit, bg="#333", fg=FG,
                  relief="flat", font=("Segoe UI", 8)).pack(side="left", padx=4)
        arrow = dict(bg="#333", fg=FG, relief="flat", font=("Segoe UI", 8, "bold"), width=2)
        tk.Button(small, text="↓", command=self.export_profile, **arrow).pack(side="left", padx=(8, 1))
        tk.Button(small, text="↑", command=self.import_profile, **arrow).pack(side="left")
        # (the start/stop hotkey now lives on the Hotkeys tab, alongside fishing's)
        small.grid(row=12, column=0, columnspan=2, sticky="w", padx=10, pady=(2, 4))

        # Live internals. Worth the space: "not firing" and "firing and missing" look
        # identical from the outside, and these numbers are the difference.
        self.diag = tk.Label(r, text="", justify="left", anchor="nw",
                             font=("Consolas", 8), bg="#141414", fg="#7ec8a9")
        self.diag.grid(row=13, column=0, columnspan=2, sticky="we", padx=10, pady=(2, 4))

        self._bottom_bar(r, row=14)

        r.grid_columnconfigure(1, weight=1)
        self.on_mode()

    def _start_engine(self):
        if self.engine:
            self.engine.stop()
        self.engine = Engine(self.state, self.config)
        self.engine.start()

    def auto_cal(self):
        """Switch the window into the calibrate view and watch the screen."""
        live = not self.dry.get()
        self.cal_stopped = None
        # Say plainly that it will spend money. Dry run is the switch either way, so
        # the behaviour matches the checkbox the rest of the tool already obeys.
        self.cal_help.config(text=(
            "It finds the BID button and the bar by watching your screen.\n\n"
            "1.  Go to an auction and start bidding.\n"
            "2.  Let the BID outline light up, and the line sweep across.\n"
            "3.  Leave the game on screen and don't cover it.\n\n"
            + ("IT WILL BID for real on every cue, and play the auction out\n"
               "until it ends. Tick 'Dry run' first if you don't want that.\n"
               if live else
               "Dry run is on, so it will only WATCH - it won't click.\n"
               "Untick 'Dry run' if you want it to bid and prove the click.\n")))
        self.state.set(running=False)
        self.cal_running = True
        self.cal_status.config(text="starting...")
        self._cal_marks(False, False)
        self.main.grid_remove()
        self.cal.grid()
        threading.Thread(target=self._cal_worker, daemon=True).start()

    def cancel_cal(self):
        self.cal_running = False
        self.cal_stopped = "cancelled"

    def _cal_should_stop(self):
        """F8/P must kill calibration too. It bids for real, so a kill switch that only
        works in the main loop isn't a kill switch -- and Cancel is unreachable while
        the game has focus, which is exactly when it's running."""
        key = panic_pressed()
        if key and self.cal_running:
            self.cal_running = False
            self.cal_stopped = key
        return not self.cal_running

    def _cal_marks(self, found_btn, found_bar, cues=0):
        for lbl, name, ok in ((self.cal_btn_lbl, "BID button", found_btn),
                              (self.cal_bar_lbl, "bar", found_bar)):
            lbl.config(text=f"{name:<12} {'FOUND' if ok else 'looking...'}",
                       fg="#4ec9b0" if ok else "#888")
        self.cal_cue_lbl.config(
            text=f"{'cue test':<12} " + (f"{cues} seen" if cues else "waiting..."),
            fg="#4ec9b0" if cues else "#888")

    def _cal_worker(self):
        cfg = None
        try:
            cfg = auto_calibrate(30, on_progress=self._cal_progress,
                                 should_stop=self._cal_should_stop,
                                 click=not self.dry.get(),
                                 hold=self.hold.get() / 1000.0)
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self.cal_status.config(text=f"failed: {msg}"))
        self.cal_running = False
        self.root.after(0, self._cal_done, cfg)

    def _cal_progress(self, msg, found_btn=False, found_bar=False, cues=0):
        # Worker thread: hop back to the UI thread before touching widgets.
        self.root.after(0, lambda: (self.cal_status.config(text=msg),
                                    self._cal_marks(found_btn, found_bar, cues)))

    def _cal_done(self, cfg):
        self.cal.grid_remove()
        self.main.grid()
        if not cfg and self.cal_stopped:
            self.state.set(status=f"calibrate stopped ({self.cal_stopped})")
            return
        if not cfg:
            self.state.set(status="calibrate: never saw the BID outline")
            messagebox.showinfo("Auto-calibrate",
                                "Didn't find the BID button.\n\nIt has to actually see "
                                "the white outline light up during those 30 seconds. "
                                "Run it again with an auction in progress, or use "
                                "'Setup by hand'.")
            return
        name = self.store.get("active") or "default"
        self.store["active"] = name
        cues = cfg.pop("cues_seen", 0)
        cfg.update(lead_ms=self.lead.get(), mode="outline",
                   outline_min_run=self.othr.get(), click_hold_ms=self.hold.get(),
                   cue_bright=self.cue.get(), stop_after_s=self.stopafter.get(),
                   calibrated_at=time.time())
        self.store["profiles"][name] = cfg
        self.config = cfg
        save_store(self.store)
        self._start_engine()
        self.state.set(status=(f"calibrated - cue tested {cues}x" if cues
                               else "calibrated - cue NOT tested"))
        b = cfg["bid_box"]
        if cues:
            messagebox.showinfo("Auto-calibrate", (
                f"Success.\n\n"
                f"BID button   {b['width']}x{b['height']} at {b['left']},{b['top']}\n"
                f"click point  {cfg['bid_xy'][0]},{cfg['bid_xy'][1]}\n"
                f"cue tested   {cues} time(s)"
                + ("\n\nIt bid on each one, so the clicking works. Press START."
                   if not self.dry.get() else
                   "\n\nIt only watched (Dry run). Untick Dry run to bid for real.")))
        else:
            messagebox.showwarning("Auto-calibrate", (
                "Found the BID button, but never saw its outline light again, so the "
                "cue was never tested.\n\nIt should still work - but re-run this during "
                "a busier auction if you want it proven."))

    def export_profile(self):
        """Down arrow: save your setup to a file you can send someone."""
        from tkinter import filedialog
        if not self.config:
            return
        self._stash()
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export setup", defaultextension=".json",
            initialfile="bidbot_setup.json", filetypes=[("Bid Bot setup", "*.json")])
        if not path:
            return
        with open(path, "w") as f:
            json.dump({"bidbot_location": "setup", "config": self.config}, f, indent=2)
        self.state.set(status="exported")

    def import_profile(self):
        """Up arrow: load a setup someone sent you."""
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.root, title="Import setup",
                                          filetypes=[("Bid Bot setup", "*.json"),
                                                     ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path) as f:
                raw = json.load(f)
            cfg = raw.get("config", raw)
            if not all(k in cfg for k in ("bid_box", "bid_xy", "track")):
                raise ValueError("not a Bid Bot setup file")
        except Exception as e:
            messagebox.showerror("Bid Bot", f"Couldn't import that file:\n{e}")
            return
        name = self.store.get("active") or "default"
        self.store["active"] = name
        self.store["profiles"][name] = cfg
        self.config = cfg
        self.lead.set(cfg.get("lead_ms", 25.0))
        self.othr.set(cfg.get("outline_min_run", 150))
        self.hold.set(cfg.get("click_hold_ms", 40))
        self.cue.set(cfg.get("cue_bright", 200))
        self.stopafter.set(cfg.get("stop_after_s", 10))
        self.mode.set(cfg.get("mode", "outline"))
        self.on_mode()
        save_store(self.store)
        self._start_engine()
        self.state.set(status="imported")

    def _stash(self):
        """Push the current slider values back into the saved setup."""
        if self.config:
            self.config.update(lead_ms=self.lead.get(), mode=self.mode.get(),
                               outline_min_run=self.othr.get(),
                               click_hold_ms=self.hold.get(),
                               cue_bright=self.cue.get(),
                               stop_after_s=self.stopafter.get())

    def setup(self):
        """Re-drag the boxes for the CURRENT location."""
        was = self.state.snapshot()["running"]
        self.state.set(running=False, status="setup")
        cfg = run_setup_for(self)
        if cfg:
            cfg.update(lead_ms=self.lead.get(), mode=self.mode.get(),
                       outline_min_run=self.othr.get(), click_hold_ms=self.hold.get(),
                       stop_after_s=self.stopafter.get())
            self.store.setdefault("active", "default")
            self.store["profiles"][self.store["active"]] = cfg
            self.config = cfg
            save_store(self.store)
            self._start_engine()
            self.state.set(status="setup saved")
        if was and self.config:
            self.state.set(running=True)

    def edit(self):
        """Freeze the screen and show exactly what the bot reads and where it clicks."""
        if not self.config:
            messagebox.showinfo("Bid Bot", "Run Setup region first.")
            return
        was = self.state.snapshot()["running"]
        self.state.set(running=False, status="editing regions")
        self.root.withdraw()
        self.root.update()
        time.sleep(0.15)   # let this window actually vanish before we photograph the screen
        cfg = run_editor(self.root, self.config)
        self.root.deiconify()
        if cfg:
            self.config = cfg
            self.store["profiles"][self.store["active"]] = cfg
            save_store(self.store)
            self._start_engine()
            self.state.set(status="regions saved")
        if was:
            self.state.set(running=True)

    def toggle(self):
        if self.state.snapshot()["running"] or self.countdown:
            self.countdown = 0
            self.state.set(running=False, status="stopped")
            return
        # Say what is missing and what to press. Starting into a config that was never
        # set up just sits there reading 0px forever, which looks like a broken tool
        # rather than an unfinished one.
        problem = self._why_not_ready()
        if problem:
            self.state.set(status="not calibrated")
            messagebox.showerror("Can't start yet", problem)
            return
        self.countdown = 3
        self._tick()

    def _why_not_ready(self):
        """-> a plain explanation of what's missing, or None if good to go."""
        c = self.config
        if not c:
            return ("Nothing is set up yet.\n\n"
                    "Open an auction in the game, then press Auto-calibrate and let a "
                    "round play out. It finds the BID button on its own.")
        if not c.get("bid_box") or not c.get("bid_xy"):
            return ("The BID button hasn't been located.\n\n"
                    "Press Auto-calibrate with an auction on screen, or use "
                    "Setup by hand.")
        b = c["bid_box"]
        if b.get("width", 0) < 40 or b.get("height", 0) < 20:
            return (f"The saved BID button box is too small "
                    f"({b.get('width')}x{b.get('height')}px) to be real.\n\n"
                    "Run Auto-calibrate again.")
        sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
        x, y = c["bid_xy"]
        if not (0 <= x < sw and 0 <= y < sh):
            return (f"The saved click point ({x},{y}) is off screen.\n\n"
                    "Your resolution probably changed - run Auto-calibrate again.")
        return None

    def _tick(self):
        # The UI has focus right now, so an immediate click would only re-focus the
        # game instead of pressing BID. Give the user time to click back in.
        if self.countdown <= 0:
            return
        self.btn.config(text=f"click into the game... {self.countdown}", bg="#b8860b")
        self.countdown -= 1
        if self.countdown == 0:
            self.state.set(running=True, feature="auction", status="watching")
            return
        self.root.after(1000, self._tick)

    def on_mode(self):
        self.state.set(mode="outline")
        if self.config:
            self.config["mode"] = "outline"

    def on_othr(self, v):
        self.state.set(outline_min_run=int(float(v)))

    def on_cue(self, v):
        self.state.set(cue_bright=int(float(v)))

    def on_hold(self, v):
        # The engine reads this straight off the shared config dict, so it applies
        # mid-round without a restart.
        if self.config:
            self.config["click_hold_ms"] = int(float(v))

    def on_stopafter(self, v):
        if self.config:
            self.config["stop_after_s"] = int(float(v))

    def on_lead(self, v):
        self.state.set(lead_ms=float(v))

    def on_flags(self):
        self.state.set(dry_run=self.dry.get(), auto_tune=self.auto.get())

    # --- fishing ---------------------------------------------------------------
    def toggle_fish(self):
        """START/STOP on the Fishing tab. Unlike the hotkey this isn't focus-gated --
        it's a deliberate click, and it gives you 3s to get back into the game."""
        s = self.state.snapshot()
        if s["running"] and s["feature"] == "fishing":
            self.state.set(running=False, status="fishing stopped")
            return
        if not (self.config or {}).get("fish_box"):
            messagebox.showerror("Can't start fishing", (
                "The reel bar hasn't been calibrated yet.\n\n"
                "Cast once so the reel bar is on screen, then press "
                "'Calibrate reel bar' and drag a box around it."))
            return
        self.state.set(feature="fishing")
        self.countdown = 3
        self._tick_fish()

    def _tick_fish(self):
        if self.countdown <= 0:
            return
        self.fbtn.config(text=f"click into the game... {self.countdown}", bg="#b8860b")
        self.countdown -= 1
        if self.countdown == 0:
            self.state.set(running=True, feature="fishing",
                           status="fishing - waiting for you to cast")
            return
        self.root.after(1000, self._tick_fish)

    def setup_fish(self):
        """Drag a box around the reel bar (the dark strip with the grey box and gold)."""
        was = self.state.snapshot()["running"]
        self.state.set(running=False)
        self.root.withdraw()
        self.root.update()
        time.sleep(0.15)
        try:
            box = drag_box(self.root,
                           "Drag a box around the REEL BAR - the dark strip with the "
                           "grey box and the gold marker. Include the whole strip; "
                           "loose is fine. Esc to cancel.", "#4da6ff")
        finally:
            self.root.deiconify()
        if box:
            self.config = self.config or {}
            self.config["fish_box"] = box
            self.store.setdefault("active", "default")
            self.store["profiles"][self.store["active"]] = self.config
            save_store(self.store)
            self._start_engine()
            self.state.set(status="reel bar saved")
        if was:
            self.state.set(running=True)

    def on_fish_tune(self, _v=None):
        if self.config:
            self.config["fish_accel"] = float(self.fgrip.get())
            self.config["fish_deadband"] = float(self.fdead.get())
            # the controller reads these at engine start, so restart it to apply
            if self.engine:
                self._start_engine()

    # --- hotkeys ---------------------------------------------------------------
    def on_key_flags(self):
        """Both hotkeys are live whenever they're bound -- 'clear' is the off switch.

        Also migrates configs saved while the old per-key tickbox was off: that control
        is gone, so a False left in the file would strand the key permanently disabled
        with nothing in the UI able to turn it back on.
        """
        self.state.set(auction_key_on=True, fish_key_on=True)
        if self.config:
            if not (self.config.get("auction_key_on", True)
                    and self.config.get("fish_key_on", True)):
                self.config["auction_key_on"] = True
                self.config["fish_key_on"] = True
                save_store(self.store)

    def toggle_autostop(self):
        on = not self.state.snapshot()["auto_stop"]
        self.state.set(auto_stop=on)
        if self.config:
            self.config["auto_stop"] = on
            save_store(self.store)
        self._refresh_autostop()

    def _refresh_autostop(self):
        on = self.state.snapshot()["auto_stop"]
        self.autostop.config(text="ON" if on else "OFF",
                             bg="#2e7d32" if on else "#333",
                             fg="white" if on else "#aaa",
                             activebackground="#2e7d32" if on else "#333")

    def toggle_dry(self):
        """Third way into the same flag, alongside both tabs' tick boxes.

        It drives the shared self.dry var rather than a copy of it, so the checkboxes
        and this button can never disagree about whether the mouse is live.
        """
        self.dry.set(not self.dry.get())
        self.on_flags()

    def clear_key(self, which):
        if self.config:
            self.config[which] = 0
            save_store(self.store)
        self._capturing = False
        self._refresh_keys()

    def set_key(self, which):
        """Capture the next key pressed as this feature's hotkey."""
        if getattr(self, "_capturing", False):
            return
        if not self.config:
            messagebox.showinfo("Hotkey", "Calibrate something first, then set a hotkey.")
            return
        self._capturing = which
        self.keyrows[which]["btn"].config(text="press a key...", fg="#e0a030")
        self._cap_held = {vk for vk in range(0x08, 0xFF)
                          if win32api.GetAsyncKeyState(vk) & 0x8000}
        self._cap_t0 = time.time()
        self._poll_capture()

    def _refresh_keys(self):
        for which, row in self.keyrows.items():
            vk = (self.config or {}).get(which, 0)
            row["btn"].config(text=vk_name(vk), fg="#7ec8a9" if vk else FG)
            row["dot"].config(text="✓" if vk else "✗",
                              fg="#3fb950" if vk else "#666")
        self.state.set(toggle_vk=(self.config or {}).get("toggle_vk", 0),
                       fish_vk=(self.config or {}).get("fish_vk", 0))

    def _toggle_vk(self):
        return (self.config or {}).get("toggle_vk", 0)

    def _poll_capture(self):
        which = self._capturing
        if not which:
            return
        if win32api.GetAsyncKeyState(0x1B) & 0x8000:      # Esc cancels
            self._capturing = False
            self._refresh_keys()
            return
        for vk in range(0x08, 0xFF):
            if not (win32api.GetAsyncKeyState(vk) & 0x8000):
                self._cap_held.discard(vk)                # released; now eligible
                continue
            if vk in self._cap_held or vk == 0x1B:
                continue
            if vk in _RESERVED_VK:
                self.keyrows[which]["btn"].config(text=f"{vk_name(vk)} reserved",
                                                  fg="#e06c75")
                self._cap_held.add(vk)                    # ignore until released
                continue
            other = "fish_vk" if which == "toggle_vk" else "toggle_vk"
            if self.config.get(other) == vk:
                # One key can't drive both modes -- it would toggle whichever it hit
                # first and the two would fight over the same mouse.
                self.keyrows[which]["btn"].config(text="already used", fg="#e06c75")
                self._cap_held.add(vk)
                continue
            self._capturing = False
            self.config[which] = vk
            save_store(self.store)
            self._refresh_keys()
            return
        if time.time() - self._cap_t0 > 8:                # gave up
            self._capturing = False
            self._refresh_keys()
            return
        self.root.after(25, self._poll_capture)

    def poll(self):
        s = self.state.snapshot()
        self.status.config(text=s["status"])
        self.counts.config(text=f"shots {s['hits']}   skipped {s['misses']}   {s['fps']:.0f} fps")
        self.last.config(text=s["last"])
        bid = self.config["bid_xy"] if self.config else None
        self.bidlbl.config(text=f"BID at {bid[0]},{bid[1]}" if bid else "no region set")
        ago = _ago((self.config or {}).get("calibrated_at"))
        for lbl in self.callbls:
            lbl.config(text=ago)

        # Hotkeys tab live indicators. Driven from engine state, not from whichever
        # button was clicked last -- a hotkey press or a self-stop moves this too.
        for which, feat in (("toggle_vk", "auction"), ("fish_vk", "fishing")):
            on = s["running"] and s["feature"] == feat
            self.keyrows[which]["live"].config(text="active" if on else "inactive",
                                               fg="#3fb950" if on else "#666")
        dry = self.dry.get()
        self.drybtn.config(text="ON" if dry else "OFF",
                           bg="#8a6d2e" if dry else "#333",
                           fg="white" if dry else "#aaa",
                           activebackground="#8a6d2e" if dry else "#333")

        c = self.config or {}
        ob = bid_box(c) if c.get("bid_box") or c.get("track") else None
        since = (f"{time.perf_counter() - s['last_fire']:5.1f}s ago"
                 if s["last_fire"] else "never")
        self.diag.config(text="\n".join((
            f"loop   {s['loop_ms']:5.1f}ms  scan {s['scan_ms']:4.2f}ms  {s['fps']:4.1f}fps",
            f"cue    {s['run_px']:4d}px  fire>={s['outline_min_run']}px  "
            f"white>{s['cue_bright']}  peak {s['outline_peak']}px",
            f"state  {'ARMED' if s['armed'] else 'fired'}   "
            f"{'DRY RUN' if s['dry_run'] else 'live'}   hold {c.get('click_hold_ms', 40)}ms",
            (f"end    stop after {c.get('stop_after_s', 10)}s idle   " if s["auto_stop"]
             else "end    auto-stop OFF          ")
            + (f"longest round {s['max_gap']:.1f}s" if s['max_gap']
               else "longest round --"),
            f"watch  {ob['width']}x{ob['height']} @{ob['left']},{ob['top']}" if ob
            else "watch  -- not calibrated --",
            f"click  {c.get('bid_xy', ['-', '-'])[0]},{c.get('bid_xy', ['-', '-'])[1]}"
            f"   last {since}   focus {s['focus']}",
        )))
        bidding = s["running"] and s["feature"] == "auction"
        fishing_on = s["running"] and s["feature"] == "fishing"
        if not self.countdown:
            self.btn.config(text="STOP" if bidding else "START",
                            bg="#c62828" if bidding else "#2e7d32")
            self.fbtn.config(text="STOP FISHING" if fishing_on else "START FISHING",
                             bg="#c62828" if fishing_on else "#2e7d32")
        # auto-tune writes lead back, so mirror it into the slider
        if abs(self.lead.get() - s["lead_ms"]) > 0.2:
            self.lead.set(round(s["lead_ms"] * 2) / 2)

        # --- fishing tab ---
        self.fstatus.config(text=s["status"] if s["feature"] == "fishing" else "idle")
        fb = c.get("fish_box")
        self.fboxlbl.config(text=(f"reel bar {fb['width']}x{fb['height']} "
                                  f"@{fb['left']},{fb['top']}") if fb
                            else "reel bar not calibrated yet")
        self.fdiag.config(text="\n".join((
            f"off target {s['fish_err']:6.1f}px  of {s['fish_margin']:.0f}px slack",
            f"on target  {s['fish_on_pct']:5.1f}%   this catch {s['fish_secs']:4.1f}s",
            f"mouse      {'HELD (pulling right)' if s['fish_hold'] else 'released (drifting left)'}",
            f"loop       {s['fps']:.0f} fps   {'DRY RUN' if s['dry_run'] else 'live'}",
        )))

        self.root.after(100, self.poll)

    def quit(self):
        """Close, whatever else is going on.

        Nothing in here may raise. A failure part-way through used to leave the window
        open with no way to shut it, which reads as a tool that refuses to die. Saving
        settings is a nicety; closing is not.
        """
        try:
            self.state.set(running=False)
            self.cal_running = False
            if self.engine:
                self.engine.stop()
            self._stash()
            save_store(self.store)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)   # don't negotiate with a wedged mainloop or a stuck thread

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if not claim_single_instance():
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo("Bid Bot", "Bid Bot is already running.\n\nLook for the "
                                       "window that's already open - it may be behind "
                                       "the game, or minimised.")
        r.destroy()
    else:
        App().run()
