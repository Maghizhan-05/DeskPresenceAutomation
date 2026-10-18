"""Spotify playback control via the `spotify:` URI scheme.

Same already-running guard as app_manager: if Spotify was already open before we
arrived, we treat it as the user's own session -- we still open the configured
item on arrival (that is the point of the automation) but we never quit Spotify
on departure unless we were the ones who launched it.
"""

import subprocess
import time

from . import app_manager, config


class _Failed(object):
    """Stand-in result for a timed-out osascript call."""

    returncode = -1
    stdout = ""
    stderr = "timeout"


def _osascript(script, timeout=15):
    """Run a one-liner. Never raises: a blocked Apple Events prompt or an
    unresponsive Spotify must not kill the automation loop."""
    try:
        return subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return _Failed()


def is_running():
    return app_manager.is_running(config.SPOTIFY_BUNDLE_ID)


def play_configured_item(uri=None, settle_seconds=2.0):
    """Open the configured album/playlist URI. Returns True on success.

    If Spotify was not already running, its launch is recorded in the session
    file so `close_tracked()` is allowed to quit it later.
    """
    if uri is None:
        uri = config.SPOTIFY_URI
    was_running = is_running()

    if not was_running:
        session = app_manager._load_session()
        if config.SPOTIFY_BUNDLE_ID not in {
            entry["bundle_id"] for entry in session["launched"]
        }:
            session["launched"].append(
                {"bundle_id": config.SPOTIFY_BUNDLE_ID, "launched_at": time.time()}
            )
            app_manager._save_session(session)

    result = _osascript('open location "%s"' % uri)
    if result.returncode != 0:
        return False

    if not was_running:
        # Give a cold-started Spotify a moment before issuing transport control.
        time.sleep(settle_seconds)

    # `open location` already starts playback. The explicit play is best-effort
    # insurance for the paused-on-open case; a timeout here (e.g. the Apple
    # Events permission prompt on first run) must not report failure.
    _osascript('tell application id "%s" to play' % config.SPOTIFY_BUNDLE_ID)
    return True


def pause():
    """Pause playback if Spotify is running. Never force-quits the user's app."""
    if not is_running():
        return False
    result = _osascript(
        'tell application id "%s" to pause' % config.SPOTIFY_BUNDLE_ID
    )
    return result.returncode == 0
