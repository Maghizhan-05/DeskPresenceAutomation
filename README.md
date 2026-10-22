# Desk Presence Automation — macOS guide

Sit down, and this app can open ChatGPT and Claude and play a Spotify album. Leave the camera's view for about **30 seconds**, and it pauses Spotify and closes **only the app instances the automation opened**. You control it from the **DP** menu-bar item; **Disable Automation** stops the camera worker.

**Mac only.** This version uses Apple Vision, macOS menu-bar APIs, LaunchAgent, and AppleScript. It does **not** run on Windows. There is no Windows installer or equivalent in this repository yet.

> **Already see DP in the menu bar?** This Mac is already installed. Skip the installation steps and go straight to [Daily use](#daily-use). Do not start a second copy of the menu-bar app.

## Before you install on another Mac

- A Mac with a working webcam and `python3` (including `venv` and `pip`). This project has been exercised with Python 3.9 on macOS; other combinations have not been validated here.
- The native ChatGPT and Claude apps, plus Spotify signed in and able to play music. The configured bundle IDs were verified for the installations on the original Mac; check [Troubleshooting](#troubleshooting) if your copies differ.
- A local copy of this **entire project folder** from its owner. It contains `requirements.txt`, `presence_automation/`, and `launchd/`. No public download link or packaged installer is provided here.
- Permission to install Python packages and a LaunchAgent in **your own user account**. No administrator installation is specified.

Keep the project folder in its final location after installation: the LaunchAgent uses absolute paths to it.

## Install once

1. Open **Terminal** and enter the project folder. An easy way is to type `cd ` (with a space), drag the folder from Finder into Terminal, then press Return. Confirm `requirements.txt` and `launchd/` are in that folder.
2. Create the project's virtual environment and install dependencies:

   ```bash
   python3 -m venv .venv
   ./.venv/bin/python -m pip install -r requirements.txt
   ```

   This installs OpenCV, the Apple Vision bridge, and the menu-bar library into this folder's `.venv`; it does not install a camera model or upload frames.

3. Still inside the project folder, copy and tailor the menu-bar LaunchAgent for **your** home directory and the folder's actual path. Paste this whole block:

   ```bash
   export APP_DIR="$PWD"
   python3 - <<'PY'
   import os
   import plistlib
   import subprocess
   from pathlib import Path

   root = Path(os.environ["APP_DIR"]).resolve()
   source = root / "launchd" / "com.user.deskpresence.menubar.plist"
   agent_dir = Path.home() / "Library" / "LaunchAgents"
   state_dir = Path.home() / ".local" / "state" / "deskpresence"
   agent_dir.mkdir(parents=True, exist_ok=True)
   state_dir.mkdir(parents=True, exist_ok=True)
   agent = plistlib.loads(subprocess.check_output(
       ["plutil", "-convert", "binary1", "-o", "-", str(source)]
   ))
   agent["ProgramArguments"][0] = str(root / ".venv" / "bin" / "python")
   agent["WorkingDirectory"] = str(root)
   agent["StandardOutPath"] = str(state_dir / "menubar.out.log")
   agent["StandardErrorPath"] = str(state_dir / "menubar.err.log")
   target = agent_dir / "com.user.deskpresence.menubar.plist"
   with target.open("wb") as stream:
       plistlib.dump(agent, stream)
   print("Installed LaunchAgent:", target)
   PY
   plutil -lint "$HOME/Library/LaunchAgents/com.user.deskpresence.menubar.plist"
   launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.user.deskpresence.menubar.plist"
   ```

   `plutil` should report **OK**. The **DP off** menu-bar item should appear. Run `bootstrap` **only once** for a new install; if the service is already loaded, use the existing DP item instead of creating another process.

4. Click **DP off → Enable Automation**. Allow macOS **Camera** permission for the Python worker when prompted. Allow **Apple Events/Automation** permissions when prompted to control the other apps and Spotify. If you denied either, see [Troubleshooting](#troubleshooting).
5. To start the menu automatically at the next login, open **DP** and look at **Launch at Login**. If it already has a ✓, leave it alone; otherwise click it once to check it. This is separate from **Enable Automation**: the first controls whether the *menu* appears at login; the second controls whether the *camera worker* runs. The menu remembers the automation's last enabled/disabled setting.

## Daily use

| What to do | What you should see |
| --- | --- |
| Click **DP → Enable Automation** | `DP ...` while starting, then `DP on` and `Status: Running (Present)` or `Running (Away)`. The worker uses the webcam at roughly one sample per second. |
| Sit where the webcam sees your face | After three consecutive positive samples, the automation opens ChatGPT/Claude **if not already open**, and requests playback of its configured Spotify album. |
| Leave the camera's view | The preview shows `Away - closing apps in Ns` if open. After roughly 30 seconds of confirmed absence, Spotify is paused and only automation-launched ChatGPT/Claude instances are asked to quit. There is a 60-second cooldown before another arrival can launch apps. |
| Click **DP → Show Camera Preview** | An optional local live window with a face box and countdown. Close with its red button or click the menu item again; the worker keeps running. Preview is **off by default**. |
| Click **DP → Disable Automation** | `DP off` / `Status: Stopped`. The worker exits, which releases its camera access; the menu remains available. Disabling does **not** retroactively close apps. |
| Click **DP → Quit** | Closes the preview, stops the worker, and exits the menu app. |

**Privacy:** Face detection runs locally with Apple's Vision framework. Camera frames are processed in memory, not saved as images or sent by this app. With preview on, exactly one downscaled frame occupies a fixed-size shared-memory slot, overwritten in place; turning preview off destroys it. The preview appears **on your screen**, so screen-sharing or recording software could capture it. Spotify uses its normal service for music playback; the webcam feed is not sent to Spotify.

**Detection is not infallible.** Vision uses a confidence threshold of `0.7`; three consecutive face-positive samples are required to cancel an absence countdown. A stray positive does not reset time already accrued. Test both a seated and an empty-desk period if you tune the threshold; raising it can miss real faces. A detected face does not identify *who* it belongs to.

## Troubleshooting

| Symptom | Check and fix |
| --- | --- |
| **No DP menu item after installation** | Check that `plutil -lint` returned `OK`, then run `launchctl print "gui/$(id -u)/com.user.deskpresence.menubar"`. Check `~/.local/state/deskpresence/menubar.err.log`. Ensure the project folder has not moved since installation. Do **not** run `./.venv/bin/python -m presence_automation.menubar` as a second copy while the LaunchAgent is loaded. |
| **`bootstrap` says the service is already loaded** | This is an existing installation, not a request to start a second copy. Use the DP menu. |
| **`DP ?` / `Status: Unresponsive`** | The worker or its heartbeat is unavailable or stale. Check `~/.local/state/deskpresence/deskpresence.log`; then choose **Disable Automation**, confirm `DP off`, and choose **Enable Automation** again. If it persists, check camera permission and other apps using the webcam. |
| **`cannot open camera index 0` or no detections** | Go to **System Settings → Privacy & Security → Camera** and permit the Python worker. Check that the webcam works elsewhere and is not exclusively occupied. To run a three-sample camera check, first **Disable Automation**, then run `./.venv/bin/python -m presence_automation.main --self-test` from the project folder. It reports booleans and Vision confidence scores, not images. |
| **Apps or music do not open** | Allow the Automation/Apple Events prompts. Make sure the native apps are installed and Spotify is signed in. On this Mac the configured IDs are `com.openai.codex`, `com.anthropic.claudefordesktop`, and `com.spotify.client`; another app build may differ. From Terminal, check `osascript -e 'id of app "ChatGPT"'` (and similarly `Claude`/`Spotify`) and edit `TRACKED_BUNDLE_IDS` or `SPOTIFY_BUNDLE_ID` in `presence_automation/config.py` only if necessary. The Spotify album link is configured there as `SPOTIFY_URI`. |
| **A face is missed, or an empty desk is treated as occupied** | With the worker disabled, run the self-test above while seated and while the desk is empty. Adjust `FACE_CONFIDENCE_THRESHOLD` in `presence_automation/config.py` cautiously: lower for missed faces, higher for false detections. Re-enable the worker to load a changed value. The legacy `DETECTOR_BACKEND = "haar"` option is available, but it was noisier on the tested desk. |
| **Countdown does not reach zero** | Keep the camera aimed at the empty desk for over 30 seconds. If three consecutive false face detections occur, the countdown can still cancel; check the optional preview and detection confidence using the self-test. Report the log's `present -> leaving`, `leaving -> present`, and `DEPARTED` lines to the maintainer (no images are needed). |

## Settings and diagnostics

All settings are in `presence_automation/config.py`. Key defaults: `DETECTOR_BACKEND = "vision"`, `FACE_CONFIDENCE_THRESHOLD = 0.7`, `ARRIVAL_CONSECUTIVE_SAMPLES = 3`, `RETURN_CONFIRM_SAMPLES = 3`, `ABSENCE_SECONDS = 30.0`, `COOLDOWN_SECONDS = 60.0`, and `SAMPLE_INTERVAL_SECONDS = 1.0`. Changes take effect when the **worker is restarted** (Disable, then Enable). Do not run a separate headless worker alongside the DP menu's worker.

For a non-camera diagnostics check, run:

```bash
launchctl print "gui/$(id -u)/com.user.deskpresence.menubar"
./.venv/bin/python -m unittest discover -s . -p 'test_*.py'
```

The project currently has **217 automated tests**. The logs at `~/.local/state/deskpresence/` contain timestamps, app identities, status and transitions—not image files. A heartbeat also reports `detector_backend` (`vision` or `haar`) for live verification. When enabled, use **DP → Disable Automation** to stop the worker; a test command is not an on/off switch.

To stop launching the menu at login, uncheck **DP → Launch at Login**. You can still use the current menu until you Quit. To remove the installed menu agent entirely, **Disable Automation** and **Quit** first, then remove its LaunchAgent; do not delete the project folder while an installed agent still points to it.
