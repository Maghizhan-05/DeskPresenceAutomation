"""Tunable constants. Pure data: this module must never perform I/O."""

# --- Detection cadence -------------------------------------------------------
CAMERA_INDEX = 0
SAMPLE_INTERVAL_SECONDS = 1.0
# Apple's bundled on-device face detector; use "haar" only as a manual fallback.
DETECTOR_BACKEND = "vision"
FACE_CONFIDENCE_THRESHOLD = 0.7

# --- State machine thresholds ------------------------------------------------
# Arrival requires this many *consecutive* positive samples (~3s at 1 fps),
# so a single glance / passer-by cannot trigger a launch.
ARRIVAL_CONSECUTIVE_SAMPLES = 3
# Mirror of ARRIVAL_CONSECUTIVE_SAMPLES on the RETURN path: while an absence
# countdown is running, this many CONSECUTIVE positive samples are required to
# cancel it. Without this, one false-positive frame (clutter, a photo, a
# reflection) resets the whole 30s timer and departure never fires.
RETURN_CONFIRM_SAMPLES = 3

# Continuous absence required before we consider the desk vacated.
ABSENCE_SECONDS = 30.0

# After a departure, block any re-launch for this long (anti-thrash).
COOLDOWN_SECONDS = 60.0

# --- Automation targets ------------------------------------------------------
# Bundle IDs verified on this machine via CFBundleIdentifier.
TRACKED_BUNDLE_IDS = (
    "com.openai.codex",            # ChatGPT.app
    "com.anthropic.claudefordesktop",  # Claude.app
)

SPOTIFY_BUNDLE_ID = "com.spotify.client"
# NOTE: the user-supplied link resolves to an ALBUM, not a playlist:
# "Jailer 2 (Original Motion Picture Soundtrack)".
SPOTIFY_URI = "spotify:album:1uwOU5ZjczJJozQkhzrGJr"

# --- Paths -------------------------------------------------------------------
# Only state transitions (timestamps + bundle IDs) are ever written. No frames.
STATE_DIR = "~/.local/state/deskpresence"
SESSION_FILE = STATE_DIR + "/session.json"
LOG_FILE = STATE_DIR + "/deskpresence.log"

# --- Menu bar / worker supervision -------------------------------------------
# Persisted enable preference + worker process identity (pid, start_time, comm).
MENUBAR_STATE_FILE = STATE_DIR + "/menubar.json"
# Worker liveness + presence heartbeat, rewritten atomically once per tick.
WORKER_HEARTBEAT_FILE = STATE_DIR + "/heartbeat.json"
# A heartbeat older than this is "Unresponsive" (5x the sample interval).
HEARTBEAT_STALE_SECONDS = 5.0
# A worker started less than this ago may not have written a heartbeat yet;
# it shows "Starting..." rather than a false "Unresponsive".
WORKER_STARTUP_GRACE_SECONDS = 15.0
# Grace period for a SIGTERM'd worker before SIGKILL.
WORKER_STOP_GRACE_SECONDS = 5.0
# LaunchAgent label for the menu-bar UI (distinct from the headless agent).
MENUBAR_LAUNCHD_LABEL = "com.user.deskpresence.menubar"

# --- camera preview (opt-in, in-memory only) ---------------------------------
# OFF by default. When enabled, the worker copies the already-in-memory frame
# (AFTER inference, so detection never depends on preview being on) into a
# fixed-size shared-memory segment that is overwritten in place every tick.
# Never appended, never written to disk, never encoded, never transmitted.
PREVIEW_ENABLED_DEFAULT = False
PREVIEW_SHM_NAME = "deskpresence_preview"
PREVIEW_WIDTH = 320
PREVIEW_HEIGHT = 240
PREVIEW_CHANNELS = 3
# Toggle file: the menu asks for preview, the worker picks it up next tick.
PREVIEW_REQUEST_FILE = STATE_DIR + "/preview.json"
# A preview request is a LEASE: the menu re-stamps it while the window is open.
# If the menu crashes, the lease expires this many seconds later and the worker
# stops publishing frames. Must comfortably exceed the menu refresh interval.
PREVIEW_LEASE_SECONDS = 10.0
