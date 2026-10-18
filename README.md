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

While the automation is disabled, no process of this project is running and the
camera is untouched. The menu-bar app itself **never opens the camera** — only
the worker does — so there is exactly one camera permission to approve, not two.

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
per tick with `{state, present, pid, ts}`. The menu reads only that file, so it
never presents stale presence as live: a heartbeat older than
`HEARTBEAT_STALE_SECONDS`, a heartbeat from a different PID, or a corrupt file
all render as `Unresponsive` (or `Starting…` during the startup grace).

## Tests

```bash
./.venv/bin/python -m unittest discover -s . -p "test_*.py" -v   # 97 tests, no hardware
```

- `tests/test_state_machine.py` (21) — the state machine is pure (no camera, no
  subprocess, no clock reads; the caller passes each sample plus its timestamp),
  so every debounce, absence, and cooldown edge is deterministic.
- `tests/test_app_manager.py` (15) — PID ownership against a fake process table:
  user-opened replacements, PID reuse, failed launches, failed quits, legacy
  records, mixed ownership.
- `tests/test_spotify_control.py` (9) — ownership is never claimed (or is rolled
  back) when `open location` fails or Spotify never appears.
- `tests/test_worker_supervisor.py` (33) — worker identity across restarts,
  SIGTERM→SIGKILL escalation, heartbeat freshness and status labels, and
  `launchctl enable|disable` login-item handling (asserting `bootout` is never
  used).
- `tests/test_menubar.py` (19) — menu structure, label/checkmark rendering for
  every status, and action wiring, with no GUI event loop.

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
tests/test_state_machine.py
tests/test_app_manager.py
tests/test_spotify_control.py
tests/test_worker_supervisor.py
tests/test_menubar.py
launchd/com.user.deskpresence.menubar.plist   menu bar (recommended)
launchd/com.user.deskpresence.plist           headless (advanced)
```
