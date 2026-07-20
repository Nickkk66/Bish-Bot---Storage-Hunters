# Storage Hunters Bot

Plays the two grindy minigames in [Storage Hunters: Open World](https://www.roblox.com/games/98800969324557/Storage-Hunters-Open-World) for you:

- **Bidding** — the game flashes a white outline around the BID button when it's time to
  bid. This watches for that flash and clicks on it.
- **Fishing** — the reel minigame wants you to keep a grey box on a gold marker. This
  holds and releases the mouse to do exactly that, and stops when the fish is landed.

One at a time, since they both drive the same mouse.

## What this is

An autoclicker with eyes. It screenshots a small patch of your screen, looks for the cue
the game is already showing you, and moves the mouse. That's the entire trick.

What it does **not** do: inject code, read or write game memory, modify or repack the
client, patch any files, abuse bugs, or touch anyone else's account. Nothing about the
game is altered. Unplug the bot and sit there yourself and you'd be making the same
clicks — just slower, and more bored.

**It is still automation, and Roblox's Terms of Use don't permit that.** Botting is
bannable however politely it's done, and this README isn't going to tell you otherwise.
It's a convenience toy for grinding a repetitive solo minigame, not a loophole and not an
exploit. Run it on an account you wouldn't mind losing, and don't leave it going
unattended for hours.

## Demo

<video src="https://github.com/Nickkk66/Bish-Bot---Storage-Hunters/raw/main/demo.mp4" controls width="100%"></video>

If the video doesn't load, [watch it here](https://github.com/Nickkk66/Bish-Bot---Storage-Hunters/raw/main/demo.mp4).

## What you need

- Windows
- Python 3.9 or newer — from https://www.python.org/downloads/, ticking
  **"Add python.exe to PATH"** during install
- Roblox running on your **primary monitor**, 1920×1080 fullscreen

The primary-monitor bit matters: the mouse control addresses the primary display only, so
on a second screen it would read pixels correctly and click in the wrong place.

Everything else installs itself into its own folder on first run, so it won't disturb
anything else on your machine.

## Getting it running

1. Download the repo (green **Code** button → **Download ZIP**) and unzip it anywhere.
2. Double-click **run.bat**. The first run installs what it needs, then opens the window.
   After that it just opens.

The window deliberately never takes focus, so clicking its sliders won't pull you out of
the game.

## Bidding

**Set up once:** get in game, start an auction so the BID button is on screen, then click
**Auto-calibrate** and let a round or two play. It finds the button and the bar by itself,
and with Dry run off it'll test-bid to prove the clicking works. If it can't find the
button, use **Setup by hand** and drag a box around it.

**Run it:** turn off **Dry run**, hit **START** (you get 3 seconds to click back into the
game) or press your hotkey. It bids every round and stops on its own once the auction
ends.

### If it's missing bids

- **Click hold (ms)** — drop it toward 0 if fast rounds aren't registering
- **Cue at (dark↔white)** — lower it to react a touch earlier
- **White line (px)** — how much of the outline must light up before it fires; the live
  readout shows the current number and the peak it has seen

### If it stops too early or too late

**Stop after (s idle)** is how long a silence counts as "the auction is over". It can't be
zero — the only end signal is a cue that never arrives, so the bot has to outwait one
round before silence means anything.

Read **longest round** in the diagnostics panel: that's the real gap between bids, measured
live. Set the slider a couple of seconds above it. As a safety net the bot will never stop
sooner than the longest round it has actually seen, so a too-tight setting can't make it
quit mid-auction once it has a round to go on. It can't help before the *first* stop,
though — nothing can. If that bites, turn auto-stop off.

## Fishing

**Set up once:** cast so the reel bar is on screen, then click **Calibrate reel bar**.

**Run it:** press **START FISHING** or your hotkey, then cast. The moment the reel bar
appears it takes over, and it lets go when the catch is done.

- **Grip (brake)** — how hard it brakes coming into the target. Too low overshoots, too
  high crawls.
- **Steadiness** — how big a wobble it tolerates before correcting. Raise it if the mouse
  chatters.

## Controls

| | |
|---|---|
| **START / STOP** | on/off, always works |
| **F8** or **P** | panic stop, from anywhere, always |
| **QUIT** | closes it — same place on every tab |
| **Dry run** | watches and detects but never touches the mouse |
| **Auto-stop** | on: stops itself when done (bidding when the cue stops, fishing when the catch ends). Off: keeps going until you stop it |

### Hotkeys tab

One key each for bidding and fishing. Press it in game to start that mode, press again to
stop; starting one stops the other, so swapping is a single tap. They only fire while
Roblox is focused, so you won't set one off while typing elsewhere.

A green ✓ means the key is bound — **clear** unbinds it, and unbound is off. Beside that,
**active / inactive** shows which mode is actually running right now.

## About speed

It can only read the screen as fast as your monitor refreshes. On a 60Hz panel that's
about 60 looks a second, so it reacts in roughly 20ms — plenty for normal rounds. The
really fast ones are the only place a higher-refresh monitor would help.

## Troubleshooting

There's a diagnostics panel on each tab showing what it's actually seeing, not just
whether it fired — "not detecting" and "detecting but missing" look identical from the
outside, and those numbers are the difference.

If fishing isn't detecting anything, run:

```
python fishprobe.py
```

It watches the saved reel-bar box for 20 seconds while you cast, prints what the grey and
gold detection find each frame, and saves `fishprobe.png` (what the bot sees) plus
`fishprobe_full.png` (the whole screen). Comparing those two settles "wrong box" against
"wrong colours" immediately.

## License

MIT. See [LICENSE](LICENSE).
