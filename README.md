# Desk Presence Automation

Samples the webcam about once per second; when you sit down it opens your work
apps and starts your music, and when you leave for 30+ seconds it pauses the
music and quits **only** the apps it opened itself. A menu-bar icon turns the
whole thing on and off, so the camera is used only while you have it enabled.

Detection is fully on-device. A frame is analysed in memory and dropped
immediately — no image is ever written to disk, encoded, or transmitted. The
only things persisted are a state-transition log, a session file recording which
apps the automation launched, and a small status heartbeat.

## Install

```bash
cd ~/Developments/DeskPresenceAutomation
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

> `opencv-python` is pinned to `<5` on purpose: OpenCV 5.0 removed
> `cv2.CascadeClassifier` and `cv2.data.haarcascades`, leaving only YuNet,
> which requires downloading an external ONNX model. The 4.x pin keeps
> detection offline with zero extra assets.

## Menu bar (recommended)

Install the menu-bar LaunchAgent so the icon appears at login:

```bash
mkdir -p ~/.local/state/deskpresence ~/Library/LaunchAgents
cp launchd/com.user.deskpresence.menubar.plist ~/Library/LaunchAgents/
# edit the absolute paths inside if your checkout differs, then:
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.user.deskpresence.menubar.plist
```

To try it immediately without logging out:

```bash
./.venv/bin/python -m presence_automation.menubar
```

A **DP** item appears in the menu bar. Clicking it shows:

| Menu item | What it does |
| --- | --- |
| `Status: …` | `Stopped`, `Starting…`, `Running (Present)`, `Running (Away)`, or `Unresponsive`. Not clickable. |
| `Enable Automation` / `Disable Automation` | Starts or stops the background worker. A checkmark shows it is running. |
| `Show Camera Preview` | Opens a small window showing what the camera sees plus the live absence countdown. Off by default; see below. |
| `Launch at Login` | Checkmark = the menu-bar app starts automatically at login. |
| `Quit` | Exits the menu-bar app, stopping the worker first so nothing is left using the camera. |

The menu-bar title mirrors the state at a glance: `DP off`, `DP ...`, `DP on`,
`DP ?`.

### Turning the camera off

**Disable Automation** terminates the worker process (SIGTERM, escalating to
SIGKILL after 5s). The camera device is reclaimed by macOS when that process
exits. Note the honest limit of this guarantee: the app verifies the *worker
process has exited*, which is what releases the device — it does not query the
camera hardware's state directly.

While the automation is disabled, no worker process or camera client of this
project is running and the camera is untouched. The menu-bar process itself
stays alive (that is how the menu stays available) but it **never opens the
camera** — only the worker does — so there is exactly one camera permission to
approve, not two.

### Camera preview (opt-in)

**Show Camera Preview** opens a small window with the live camera image, a box
around a detected face, and a status line such as
`Away - closing apps in 18s`. It is **off by default** and must be turned on
explicitly each time; closing the window stops the preview but never stops the
automation.

How it stays private:

* The worker copies a downscaled (320×240) frame into a **fixed-size** block of
  shared memory, overwritten in place every tick. It never grows, never
  appends, and holds exactly one frame.
* Nothing is written to disk, encoded to an image file, or sent anywhere. The
  menu-bar process maps that same block read-only and blits it to the screen.
* The block is destroyed when the preview is switched off, when the worker
  stops, and when the app quits. A segment left by a crashed worker is unlinked
  and recreated rather than reused, so no frame outlives the worker.
* Turning preview on does not change detection: the frame is published **after**
  inference, so the presence result is identical either way.

**The honest privacy trade-off:** with preview off, no image of you exists
anywhere outside the worker's momentary memory — it cannot be screenshotted or
screen-shared because nothing is ever drawn. Turning preview on renders your
camera feed to your own screen, which means screen-recording or screen-sharing
software could now capture it. It is still never saved or transmitted by this
app, but the on-screen window is a new (small, local, opt-in) surface that did
not exist before.

### How the 30-second departure works

Arrival needs 3 consecutive positive samples, and the return path is debounced
the same way: once the absence countdown is running, **3 consecutive positive
samples** are needed to cancel it. A single false-positive frame — background
clutter, a photo, a reflection — no longer resets the timer, and it does not
rewind absence already accrued. Without this, a lone stray detection reset the
whole 30s window and departure never fired.

### Launch at Login

The toggle uses `launchctl enable|disable`, deliberately **not**
`bootout`/`bootstrap`: booting out the agent would kill the very menu-bar
process hosting the menu. The state shown is re-read from
`launchctl print-disabled` after every change, so the checkmark reflects what
launchd actually recorded rather than what the command returned.

## Grant camera permission

The first run triggers the macOS camera prompt for the Python binary. If you
see `cannot open camera index 0`:

1. **System Settings → Privacy & Security → Camera**
2. Enable the entry for your Python interpreter (or for `launchd`-spawned
   `python` after installing the LaunchAgent).
3. Re-run — a terminal that was denied once needs the toggle flipped manually.

Apple Events permission is also required to quit apps and control Spotify; the
first `osascript` call prompts for it. All AppleScript calls are timeout-guarded,
so a pending prompt slows a transition but never crashes the loop.

Verify everything with the hardware/app probe:

```bash
./.venv/bin/python -m presence_automation.main --self-test --verbose
```

Expected output: each tracked app's running state, the session file contents,
and `camera ok, 3 samples -> [...]`.

## Advanced: headless mode (no menu bar)

The original terminal-only path still works and is unchanged:

```bash
./.venv/bin/python -m presence_automation.main --verbose
# bounded manual test:
./.venv/bin/python -m presence_automation.main --max-ticks 60 --verbose
```

Run it at login without any UI:

```bash
cp launchd/com.user.deskpresence.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.deskpresence.plist
# stop (this is the only way to disable the headless agent):
launchctl bootout gui/$(id -u)/com.user.deskpresence
```

Use either the menu-bar agent or the headless agent — not both, or two workers
will compete for the camera.

Logs: `~/.local/state/deskpresence/deskpresence.log`, plus `menubar.out.log` /
`menubar.err.log` or `launchd.out.log` / `launchd.err.log`.

## Configuration

Everything tunable lives in `presence_automation/config.py`:

| Constant | Default | Meaning |
| --- | --- | --- |
| `ARRIVAL_CONSECUTIVE_SAMPLES` | `3` | consecutive positive frames before "arrived" fires |
| `ABSENCE_SECONDS` | `30.0` | continuous absence before "departed" fires |
| `COOLDOWN_SECONDS` | `60.0` | post-departure window where re-launch is blocked |
| `SAMPLE_INTERVAL_SECONDS` | `1.0` | seconds between webcam samples |
| `CAMERA_INDEX` | `0` | which camera to use |
| `TRACKED_BUNDLE_IDS` | ChatGPT, Claude | apps launched on arrival |
| `SPOTIFY_URI` | `spotify:album:1uwOU5ZjczJJozQkhzrGJr` | what to play on arrival |
| `HEARTBEAT_STALE_SECONDS` | `5.0` | heartbeat age after which status reads `Unresponsive` |
| `WORKER_STARTUP_GRACE_SECONDS` | `15.0` | window where a heartbeat-less worker shows `Starting…` |
| `WORKER_STOP_GRACE_SECONDS` | `5.0` | SIGTERM grace before SIGKILL on Disable |

**Change the music:** copy a Spotify link, take the ID out of it, and set
`SPOTIFY_URI` to `spotify:album:<id>`, `spotify:playlist:<id>`, or
`spotify:track:<id>`.

**Change the apps:** add bundle IDs to `TRACKED_BUNDLE_IDS`. Find one with:

```bash
osascript -e 'id of app "AppName"'
```

Bundle IDs verified on this machine: ChatGPT.app is `com.openai.codex` (not
`com.openai.chat`) and Claude.app is `com.anthropic.claudefordesktop`.

## How the "don't touch my apps" guard works

Ownership is per **process instance**, not per bundle ID.

1. `launch_tracked()` skips any app that already has a running PID — that is the
   user's instance, and it is never recorded.
2. For an app it does start, it waits for a PID that was not running before the
   launch and records *that exact PID*. If the launch fails or no new PID ever
   appears, nothing is claimed.
3. `close_tracked()` terminates only a recorded `(bundle_id, pid)` pair that is
   still alive and still owned by that bundle. If our instance exited and the
   user later opened the app, the replacement has a different PID, so the record
   is silently discarded and the user's app is left alone. A PID recycled by a
   different app is likewise never touched.

Spotify follows the same rule, and on departure the automation only *pauses*
playback — it quits Spotify only if it cold-started it itself.

The worker the menu bar supervises is tracked the same way: `{pid, start_time}`
is persisted, so after the menu-bar app restarts it recognises its own worker as
a non-child process, and a recycled PID (later start time) is never signalled.

## Status reporting

The worker rewrites `~/.local/state/deskpresence/heartbeat.json` atomically once
per tick with `{state, present, pid, start_time, ts}`. The menu reads only that
file, so it never presents stale presence as live: a heartbeat older than
`HEARTBEAT_STALE_SECONDS`, one whose `pid`/`start_time` do not both match the
tracked worker exactly, a heartbeat missing either identity field, or a corrupt
file all render as `Unresponsive`. `Starting…` is reserved for the single case
where the worker has not written a heartbeat file *at all* yet (within
`WORKER_STARTUP_GRACE_SECONDS`); a heartbeat that exists but fails validation is
always `Unresponsive`, never "still starting".

Ownership of a worker this process spawned is additionally held in memory
(`pid` + `start_time`), independent of the persisted file. A corrupted or
deleted `menubar.json` therefore cannot orphan a running worker: Disable
terminates that child through its own process handle and reports success only
once the process is confirmed exited.

## Tests

```bash
./.venv/bin/python -m unittest discover -s . -p "test_*.py" -v   # 183 tests, no hardware
```

- `tests/test_state_machine.py` (34) — the state machine is pure (no camera, no
  subprocess, no clock reads; the caller passes each sample plus its timestamp),
  so every debounce, absence, and cooldown edge is deterministic. Includes the
  return-debounce regressions: a stray positive cannot cancel or rewind an
  absence countdown, and a replay of the real logged flicker pattern must reach
  `DEPARTED`.
- `tests/test_app_manager.py` (15) — PID ownership against a fake process table:
  user-opened replacements, PID reuse, failed launches, failed quits, legacy
  records, mixed ownership.
- `tests/test_spotify_control.py` (9) — ownership is never claimed (or is rolled
  back) when `open location` fails or Spotify never appears.
- `tests/test_worker_supervisor.py` (68) — worker identity across restarts with
  fail-closed schema validation (invalid PID, missing/empty `start_time`,
  non-dict or corrupt state), direct `Popen` termination of our own child even
  when persisted state is corrupted, SIGTERM→SIGKILL escalation, heartbeat
  freshness and full-identity matching, preview request/segment cleanup on stop,
  and `launchctl enable|disable` login-item handling (asserting `bootout` is
  never used).
- `tests/test_menubar.py` (26) — menu structure, label/checkmark rendering for
  every status, and action wiring, with no GUI event loop; preview open/close
  never starts or stops the worker.
- `tests/test_preview.py` (31) — shared-memory lifecycle (fixed size across 200
  writes, overwrite-in-place, unlink on close, stale segment from a crashed
  worker is replaced), the preview-off default touching no buffer, identical
  detection results with preview on and off, and countdown text rendering.

## Layout

```
presence_automation/
  config.py             constants only, no I/O
  state_machine.py      pure IDLE/ARRIVING/PRESENT/LEAVING/COOLDOWN machine
  detector.py           one webcam frame -> bool, nothing retained
  app_manager.py        launch/quit with per-PID ownership
  spotify_control.py    spotify: URI playback + pause
  main.py               loop; side effects on transition edges only
  worker_supervisor.py  worker process lifecycle + login item + heartbeat
  menubar.py            rumps status item; no camera, no process logic
  preview_buffer.py     fixed-size shared-memory slot, one frame, in place
  preview_window.py     opt-in preview window + countdown text
tests/test_state_machine.py
tests/test_app_manager.py
tests/test_spotify_control.py
tests/test_worker_supervisor.py
tests/test_menubar.py
tests/test_preview.py
launchd/com.user.deskpresence.menubar.plist   menu bar (recommended)
launchd/com.user.deskpresence.plist           headless (advanced)
```
