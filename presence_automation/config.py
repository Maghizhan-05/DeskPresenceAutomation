"""Tunable constants. Pure data: this module must never perform I/O."""

# --- Detection cadence -------------------------------------------------------
CAMERA_INDEX = 0
SAMPLE_INTERVAL_SECONDS = 1.0

# --- State machine thresholds ------------------------------------------------
# Arrival requires this many *consecutive* positive samples (~3s at 1 fps),
# so a single glance / passer-by cannot trigger a launch.
ARRIVAL_CONSECUTIVE_SAMPLES = 3

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

