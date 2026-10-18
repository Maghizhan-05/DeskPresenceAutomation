"""Ownership regression tests for app_manager.

No real apps are launched: `_running_app_table`, `_spawn` and `_terminate` are
stubbed, and the session file is redirected to a temp dir. These tests pin the
acceptance criterion "only automation-launched instances close".
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from presence_automation import app_manager, config  # noqa: E402


class FakeWorld(object):
    """A pretend macOS process table with controllable PID assignment."""

    def __init__(self, running=()):
        self.table = list(running)            # [(bundle_id, pid)]
        self.next_pid = 1000
        self.spawn_ok = True
        self.spawn_appears = True
        self.terminate_ok = True
        self.spawned = []
        self.terminated = []

    # -- seams ------------------------------------------------------------
    def running_app_table(self):
        return list(self.table)

    def spawn(self, bundle_id):
        self.spawned.append(bundle_id)
        if not self.spawn_ok:
            return False
        if self.spawn_appears:
            self.next_pid += 1
            self.table.append((bundle_id, self.next_pid))
        return True

    def terminate(self, bundle_id, pid):
        self.terminated.append((bundle_id, pid))
        if not self.terminate_ok:
            return False
        self.table = [row for row in self.table if row != (bundle_id, pid)]
        return True

    # -- helpers ----------------------------------------------------------
    def kill(self, bundle_id):
        """Simulate the process exiting on its own (no quit request)."""
        self.table = [row for row in self.table if row[0] != bundle_id]

    def user_opens(self, bundle_id):
        """Simulate the USER opening the app -> brand new, different PID."""
        self.next_pid += 1
        self.table.append((bundle_id, self.next_pid))
        return self.next_pid

    def pids(self, bundle_id):
        return sorted(pid for bid, pid in self.table if bid == bundle_id)


class AppManagerTestCase(unittest.TestCase):
    APP = "com.example.tracked"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_session = config.SESSION_FILE
        config.SESSION_FILE = os.path.join(self.tmp, "session.json")

        self.world = FakeWorld()
        self._orig = (
            app_manager._running_app_table,
            app_manager._spawn,
            app_manager._terminate,
            app_manager.LAUNCH_GRACE_SECONDS,
        )
        app_manager._running_app_table = self.world.running_app_table
        app_manager._spawn = self.world.spawn
        app_manager._terminate = self.world.terminate
        app_manager.LAUNCH_GRACE_SECONDS = 0.5  # keep failure paths fast

    def tearDown(self):
        (
            app_manager._running_app_table,
            app_manager._spawn,
            app_manager._terminate,
            app_manager.LAUNCH_GRACE_SECONDS,
        ) = self._orig
        config.SESSION_FILE = self._orig_session


class ProcessReplacementTests(AppManagerTestCase):
    """The blocker: a user-opened replacement must never be quit."""

    def test_launch_records_the_exact_pid(self):
        self.assertEqual(app_manager.launch_tracked([self.APP]), [self.APP])
        pid = self.world.pids(self.APP)[0]
        self.assertEqual(app_manager.tracked_instances(), [(self.APP, pid)])

    def test_replacement_after_our_instance_exits_is_not_quit(self):
        app_manager.launch_tracked([self.APP])
        our_pid = self.world.pids(self.APP)[0]

        self.world.kill(self.APP)                 # our instance dies
        user_pid = self.world.user_opens(self.APP)  # user opens it themselves
        self.assertNotEqual(our_pid, user_pid)

        self.assertEqual(app_manager.close_tracked(), [])
        self.assertEqual(self.world.terminated, [])          # nothing quit
        self.assertIn(user_pid, self.world.pids(self.APP))   # user's app alive
        self.assertEqual(app_manager.tracked_instances(), [])  # stale record gone

    def test_our_own_instance_is_quit_by_pid(self):
        app_manager.launch_tracked([self.APP])
        our_pid = self.world.pids(self.APP)[0]
        self.assertEqual(app_manager.close_tracked(), [self.APP])
        self.assertEqual(self.world.terminated, [(self.APP, our_pid)])
        self.assertEqual(self.world.pids(self.APP), [])
        self.assertEqual(app_manager.tracked_instances(), [])

    def test_already_running_app_is_never_launched_or_recorded(self):
        user_pid = self.world.user_opens(self.APP)
        self.assertEqual(app_manager.launch_tracked([self.APP]), [])
        self.assertEqual(self.world.spawned, [])
        self.assertEqual(app_manager.tracked_instances(), [])
        self.assertEqual(app_manager.close_tracked(), [])
        self.assertIn(user_pid, self.world.pids(self.APP))

    def test_second_user_instance_alongside_ours_is_left_alone(self):
        app_manager.launch_tracked([self.APP])
        our_pid = self.world.pids(self.APP)[0]
        user_pid = self.world.user_opens(self.APP)
        self.assertEqual(app_manager.close_tracked(), [self.APP])
        self.assertEqual(self.world.terminated, [(self.APP, our_pid)])
        self.assertEqual(self.world.pids(self.APP), [user_pid])

    def test_pid_reused_by_another_bundle_is_not_quit(self):
        app_manager.launch_tracked([self.APP])
        our_pid = self.world.pids(self.APP)[0]
        # Same PID number, different app now owns it.
        self.world.table = [("com.other.app", our_pid)]
        self.assertEqual(app_manager.close_tracked(), [])
        self.assertEqual(self.world.terminated, [])

    def test_legacy_record_without_pid_is_discarded_not_quit(self):
        app_manager._save_session(
            {"launched": [{"bundle_id": self.APP, "launched_at": 0.0}]}
        )
        self.world.user_opens(self.APP)
        self.assertEqual(app_manager.close_tracked(), [])
        self.assertEqual(self.world.terminated, [])
        self.assertEqual(app_manager.tracked_instances(), [])


class LaunchFailureOwnershipTests(AppManagerTestCase):
    def test_failed_spawn_claims_nothing(self):
        self.world.spawn_ok = False
        self.assertEqual(app_manager.launch_tracked([self.APP]), [])
        self.assertEqual(app_manager.tracked_instances(), [])

    def test_spawn_that_never_appears_claims_nothing(self):
        self.world.spawn_appears = False
        self.assertEqual(app_manager.launch_tracked([self.APP]), [])
        self.assertEqual(app_manager.tracked_instances(), [])
        # A later user-opened instance must therefore survive a departure.
        user_pid = self.world.user_opens(self.APP)
        self.assertEqual(app_manager.close_tracked(), [])
        self.assertIn(user_pid, self.world.pids(self.APP))

    def test_failed_terminate_keeps_ownership_for_retry(self):
        app_manager.launch_tracked([self.APP])
        our_pid = self.world.pids(self.APP)[0]
        self.world.terminate_ok = False
        self.assertEqual(app_manager.close_tracked(), [])
        self.assertEqual(app_manager.tracked_instances(), [(self.APP, our_pid)])
        self.world.terminate_ok = True
        self.assertEqual(app_manager.close_tracked(), [self.APP])


class MultiAppTests(AppManagerTestCase):
    A = "com.example.a"
    B = "com.example.b"

    def test_mixed_ownership_only_ours_closes(self):
        user_b = self.world.user_opens(self.B)  # user already has B open
        launched = app_manager.launch_tracked([self.A, self.B])
        self.assertEqual(launched, [self.A])
        our_a = self.world.pids(self.A)[0]

        self.assertEqual(app_manager.close_tracked(), [self.A])
        self.assertEqual(self.world.terminated, [(self.A, our_a)])
        self.assertEqual(self.world.pids(self.B), [user_b])

    def test_relaunch_after_close_tracks_the_new_pid(self):
        app_manager.launch_tracked([self.A])
        first = self.world.pids(self.A)[0]
        app_manager.close_tracked()
        app_manager.launch_tracked([self.A])
        second = self.world.pids(self.A)[0]
        self.assertNotEqual(first, second)
        self.assertEqual(app_manager.tracked_instances(), [(self.A, second)])


class CameraConfigTests(unittest.TestCase):
    """CAMERA_INDEX must be read when Detector is built, not at import."""

    def setUp(self):
        from presence_automation import detector as detector_module

        self.module = detector_module
        self._orig_cascade = detector_module.cv2.CascadeClassifier
        self._orig_path = detector_module._haar_cascade_path
        detector_module._haar_cascade_path = lambda: "stub.xml"

        class StubCascade(object):
            def __init__(self, _path):
                pass

            def empty(self):
                return False

        detector_module.cv2.CascadeClassifier = StubCascade
        self._orig_index = config.CAMERA_INDEX

    def tearDown(self):
        self.module.cv2.CascadeClassifier = self._orig_cascade
        self.module._haar_cascade_path = self._orig_path
        config.CAMERA_INDEX = self._orig_index

    def test_signature_default_is_not_frozen(self):
        import inspect

        signature = inspect.signature(self.module.Detector)
        self.assertIsNone(signature.parameters["camera_index"].default)

    def test_config_change_is_honoured_at_construction(self):
        config.CAMERA_INDEX = 7
        self.assertEqual(self.module.Detector().camera_index, 7)
        config.CAMERA_INDEX = 2
        self.assertEqual(self.module.Detector().camera_index, 2)

    def test_explicit_argument_still_wins(self):
        config.CAMERA_INDEX = 7
        self.assertEqual(self.module.Detector(camera_index=3).camera_index, 3)
        self.assertEqual(self.module.Detector(camera_index=0).camera_index, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
