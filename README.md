# Desk Presence Automation

Samples the webcam about once per second; when you sit down it opens your work
apps and starts your music, and when you leave for 30+ seconds it pauses the
music and quits **only** the apps it opened itself.

Detection is fully on-device. A frame is analysed in memory and dropped
immediately — no image is ever written to disk, encoded, or transmitted. The
only thing persisted is a state-transition log (timestamps and bundle IDs) and a
small session file recording which apps the automation launched.

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

## Run

```bash
./.venv/bin/python -m presence_automation.main --verbose
# bounded manual test:
./.venv/bin/python -m presence_automation.main --max-ticks 60 --verbose
```

### Run at login

```bash
cp launchd/com.user.deskpresence.plist ~/Library/LaunchAgents/
# edit the absolute paths inside if your checkout differs, then:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.deskpresence.plist
# stop:
launchctl bootout gui/$(id -u)/com.user.deskpresence
```

Logs: `~/.local/state/deskpresence/deskpresence.log` plus
`launchd.out.log` / `launchd.err.log`.

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

## Tests

```bash
./.venv/bin/python -m unittest discover -s . -p "test_*.py" -v   # 45 tests, no hardware
```

- `tests/test_state_machine.py` (21) — the state machine is pure (no camera, no
  subprocess, no clock reads; the caller passes each sample plus its timestamp),
  so every debounce, absence, and cooldown edge is deterministic.
- `tests/test_app_manager.py` (15) — PID ownership against a fake process table:
  user-opened replacements, PID reuse, failed launches, failed quits, legacy
  records, mixed ownership.
- `tests/test_spotify_control.py` (9) — ownership is never claimed (or is rolled
  back) when `open location` fails or Spotify never appears.

## Layout

```
presence_automation/
  config.py           constants only, no I/O
  state_machine.py    pure IDLE/ARRIVING/PRESENT/LEAVING/COOLDOWN machine
  detector.py         one webcam frame -> bool, nothing retained
  app_manager.py      launch/quit with per-PID ownership
  spotify_control.py  spotify: URI playback + pause
  main.py             loop; side effects on transition edges only
tests/test_state_machine.py
tests/test_app_manager.py
tests/test_spotify_control.py
launchd/com.user.deskpresence.plist
```
