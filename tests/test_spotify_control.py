"""Spotify ownership tests: a failed launch must never leave a claim behind.

No real Spotify is touched: `_osascript` and the app_manager process seams are
stubbed and the session file is redirected to a temp dir.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from presence_automation import app_manager, config, spotify_control  # noqa: E402
from tests.test_app_manager import FakeWorld  # noqa: E402


class _Result(object):
    def __init__(self, returncode):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class SpotifyOwnershipTests(unittest.TestCase):
    SPOTIFY = config.SPOTIFY_BUNDLE_ID

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_session = config.SESSION_FILE
        config.SESSION_FILE = os.path.join(self.tmp, "session.json")

        self.world = FakeWorld()
        self._orig_table = app_manager._running_app_table
        self._orig_terminate = app_manager._terminate
        self._orig_grace = app_manager.LAUNCH_GRACE_SECONDS
        app_manager._running_app_table = self.world.running_app_table
        app_manager._terminate = self.world.terminate
        app_manager.LAUNCH_GRACE_SECONDS = 0.5

        self.scripts = []
        self.open_ok = True
        self.open_starts_spotify = True
        self._orig_osascript = spotify_control._osascript
        spotify_control._osascript = self._fake_osascript

    def tearDown(self):
        app_manager._running_app_table = self._orig_table
        app_manager._terminate = self._orig_terminate
        app_manager.LAUNCH_GRACE_SECONDS = self._orig_grace
        spotify_control._osascript = self._orig_osascript
        config.SESSION_FILE = self._orig_session

    def _fake_osascript(self, script, timeout=15):
        self.scripts.append(script)
        if script.startswith("open location"):
            if not self.open_ok:
                return _Result(-1)  # e.g. osascript timeout / Spotify missing
            if self.open_starts_spotify and not self.world.pids(self.SPOTIFY):
                self.world.user_opens(self.SPOTIFY)  # cold start -> fresh PID
            return _Result(0)
        return _Result(0)

    # -- failure paths ----------------------------------------------------
    def test_failed_open_location_claims_nothing(self):
        self.open_ok = False
        self.assertFalse(spotify_control.play_configured_item(settle_seconds=0))
        self.assertEqual(app_manager.tracked_bundle_ids(), [])

    def test_failed_launch_then_user_opens_spotify_is_not_quit(self):
        self.open_ok = False
        self.assertFalse(spotify_control.play_configured_item(settle_seconds=0))
        user_pid = self.world.user_opens(self.SPOTIFY)
        self.assertEqual(app_manager.close_tracked(), [])
        self.assertEqual(self.world.terminated, [])
        self.assertIn(user_pid, self.world.pids(self.SPOTIFY))

    def test_open_succeeds_but_spotify_never_appears_claims_nothing(self):
        self.open_starts_spotify = False
        self.assertFalse(spotify_control.play_configured_item(settle_seconds=0))
        self.assertEqual(app_manager.tracked_bundle_ids(), [])

    def test_prior_stale_claim_is_rolled_back_on_failed_launch(self):
        app_manager.record_ownership(self.SPOTIFY, 4242)  # leftover record
        self.open_ok = False
        self.assertFalse(spotify_control.play_configured_item(settle_seconds=0))
        self.assertEqual(app_manager.tracked_bundle_ids(), [])

    # -- success paths ----------------------------------------------------
    def test_cold_start_claims_the_observed_pid(self):
        self.assertTrue(spotify_control.play_configured_item(settle_seconds=0))
        pid = self.world.pids(self.SPOTIFY)[0]
        self.assertEqual(app_manager.tracked_instances(), [(self.SPOTIFY, pid)])

    def test_already_running_spotify_is_never_claimed(self):
        user_pid = self.world.user_opens(self.SPOTIFY)
        self.assertTrue(spotify_control.play_configured_item(settle_seconds=0))
        self.assertEqual(app_manager.tracked_bundle_ids(), [])
        self.assertEqual(app_manager.close_tracked(), [])
        self.assertIn(user_pid, self.world.pids(self.SPOTIFY))

    def test_configured_album_uri_is_the_one_opened(self):
        spotify_control.play_configured_item(settle_seconds=0)
        self.assertIn('open location "%s"' % config.SPOTIFY_URI, self.scripts)
        self.assertTrue(config.SPOTIFY_URI.startswith("spotify:album:"))

    def test_pause_is_a_noop_when_spotify_is_not_running(self):
        self.assertFalse(spotify_control.pause())
        self.assertEqual(self.scripts, [])

    def test_pause_does_not_quit_the_users_spotify(self):
        user_pid = self.world.user_opens(self.SPOTIFY)
        self.assertTrue(spotify_control.pause())
        self.assertIn(user_pid, self.world.pids(self.SPOTIFY))
        self.assertEqual(self.world.terminated, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
