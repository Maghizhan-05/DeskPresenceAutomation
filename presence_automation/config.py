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
