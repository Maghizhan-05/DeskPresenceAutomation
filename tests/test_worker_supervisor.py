"""Tests for worker_supervisor: PID identity, stop escalation, heartbeat, login item.

No real worker, no real launchctl: `_popen`, `_signal`, `_process_identity` and
`_run` are stubbed and both state files are redirected to a temp dir.
"""

import os
import signal
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from presence_automation import config, worker_supervisor as ws  # noqa: E402


class FakeProcessWorld(object):
    """A pretend process table keyed by PID, with identity and signal rules."""

    def __init__(self):
        self.procs = {}          # pid -> {"start_time", "comm", "ignores_term"}
        self.next_pid = 500
        self.spawn_ok = True
        self.spawn_dies_instantly = False
        self.signals = []        # [(pid, sig)]

    # -- seams ------------------------------------------------------------
    def popen(self, args):
        if not self.spawn_ok:
            return None
        self.next_pid += 1
        pid = self.next_pid
        if not self.spawn_dies_instantly:
            self.procs[pid] = {
                "start_time": "Sun Oct 18 12:00:0%d 2026" % (pid % 10),
                "comm": "/usr/bin/python3",
                "ignores_term": False,
            }
        return FakePopen(self, pid)

    def process_identity(self, pid):
        proc = self.procs.get(int(pid))
        if proc is None:
            return None
        return proc["start_time"], proc["comm"]

    def send_signal(self, pid, sig):
        self.signals.append((int(pid), sig))
        proc = self.procs.get(int(pid))
        if proc is None:
            return False
        if sig == signal.SIGTERM and not proc["ignores_term"]:
            del self.procs[int(pid)]
        elif sig == signal.SIGKILL:
            del self.procs[int(pid)]  # SIGKILL is never ignorable
        return True

    # -- helpers ----------------------------------------------------------
    def recycle_pid(self, pid):
        """Same PID number, different process (new start time + comm)."""
        self.procs[int(pid)] = {
            "start_time": "Mon Oct 19 09:30:00 2026",
            "comm": "/usr/sbin/unrelated",
            "ignores_term": False,
        }

    def alive(self, pid):
        return int(pid) in self.procs


class FakePopen(object):
    def __init__(self, world, pid):
        self.world = world
        self.pid = pid
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return None if self.world.alive(self.pid) else 0

    def terminate(self):
        self.terminate_calls += 1
        self.world.send_signal(self.pid, signal.SIGTERM)

    def kill(self):
        self.kill_calls += 1
        self.world.send_signal(self.pid, signal.SIGKILL)


class SupervisorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_paths = (config.MENUBAR_STATE_FILE, config.WORKER_HEARTBEAT_FILE)
        config.MENUBAR_STATE_FILE = os.path.join(self.tmp, "menubar.json")
        config.WORKER_HEARTBEAT_FILE = os.path.join(self.tmp, "heartbeat.json")

        self.world = FakeProcessWorld()
        self._orig = (ws._popen, ws._signal, ws._process_identity, ws._run)
        ws._popen = self.world.popen
        ws._signal = self.world.send_signal
        ws._process_identity = self.world.process_identity
        ws._clear_child()

    def tearDown(self):
        ws._popen, ws._signal, ws._process_identity, ws._run = self._orig
        ws._clear_child()
        config.MENUBAR_STATE_FILE, config.WORKER_HEARTBEAT_FILE = self._orig_paths

    def simulate_menubar_restart(self):
        """Drop the child handle: the worker is now a non-child process."""
        ws._clear_child()

    def corrupt_state_losing_start_time(self):
        """Persisted record loses start_time while our child is still alive."""
        raw = ws._read_json(ws._state_path(), {})
        worker = dict(raw.get("worker") or {})
        worker.pop("start_time", None)
        ws._atomic_write(ws._state_path(), {"enabled": True, "worker": worker})


class StartStopTests(SupervisorTestCase):
    def test_start_records_full_identity(self):
        self.assertTrue(ws.start_worker())
        worker = ws.load_state()["worker"]
        self.assertIsNotNone(worker)
        self.assertEqual(
            set(worker), {"pid", "start_time", "comm", "started_at"}
        )
        self.assertTrue(ws.is_worker_running())
        self.assertTrue(ws.load_state()["enabled"])

    def test_start_when_already_running_is_a_noop(self):
        ws.start_worker()
        first = ws.load_state()["worker"]
        self.assertTrue(ws.start_worker())
        self.assertEqual(ws.load_state()["worker"], first)
        self.assertEqual(len(self.world.procs), 1)

    def test_failed_spawn_claims_nothing(self):
        self.world.spawn_ok = False
        self.assertFalse(ws.start_worker())
        self.assertIsNone(ws.load_state()["worker"])
        self.assertFalse(ws.load_state()["enabled"])

    def test_worker_dying_instantly_claims_nothing(self):
        self.world.spawn_dies_instantly = True
        self.assertFalse(ws.start_worker())
        self.assertIsNone(ws.load_state()["worker"])

    def test_stop_when_not_running_is_a_noop(self):
        self.assertTrue(ws.stop_worker(timeout=0.3))
        self.assertEqual(self.world.signals, [])
        self.assertFalse(ws.load_state()["enabled"])

    def test_graceful_sigterm_exit_avoids_sigkill(self):
        ws.start_worker()
        pid = ws.worker_pid()
        self.assertTrue(ws.stop_worker(timeout=1.0))
        self.assertEqual(self.world.signals, [(pid, signal.SIGTERM)])
        self.assertNotIn(signal.SIGKILL, [s for _p, s in self.world.signals])
        self.assertFalse(ws.is_worker_running())
        self.assertFalse(ws.load_state()["enabled"])

    def test_sigkill_escalation_when_sigterm_ignored(self):
        ws.start_worker()
        pid = ws.worker_pid()
        self.world.procs[pid]["ignores_term"] = True
        self.assertTrue(ws.stop_worker(timeout=0.5))
        sigs = [s for p, s in self.world.signals if p == pid]
        self.assertEqual(sigs, [signal.SIGTERM, signal.SIGKILL])
        self.assertFalse(self.world.alive(pid))
        self.assertFalse(ws.is_worker_running())

    def test_stop_clears_the_heartbeat(self):
        ws.start_worker()
        ws._atomic_write(ws._heartbeat_path(), {"state": "present", "ts": time.time()})
        ws.stop_worker(timeout=1.0)
        self.assertIsNone(ws.read_heartbeat())


class PidIdentityTests(SupervisorTestCase):
    def test_liveness_survives_a_menubar_restart(self):
        ws.start_worker()
        pid = ws.worker_pid()
        self.simulate_menubar_restart()
        self.assertTrue(ws.is_worker_running())   # non-child path
        self.assertEqual(ws.worker_pid(), pid)

    def test_enabled_state_persists_across_a_restart(self):
        ws.start_worker()
        self.simulate_menubar_restart()
        self.assertTrue(ws.load_state()["enabled"])

    def test_recycled_pid_is_not_running_and_is_never_signalled(self):
        ws.start_worker()
        pid = ws.worker_pid()
        self.simulate_menubar_restart()
        del self.world.procs[pid]        # our worker exited
        self.world.recycle_pid(pid)      # an unrelated process took the PID
        self.assertFalse(ws.is_worker_running())
        self.assertIsNone(ws.worker_pid())
        self.assertTrue(ws.stop_worker(timeout=0.3))
        self.assertEqual(self.world.signals, [])     # nothing signalled
        self.assertTrue(self.world.alive(pid))       # bystander untouched

    def test_dead_worker_reports_stopped(self):
        ws.start_worker()
        pid = ws.worker_pid()
        self.simulate_menubar_restart()
        del self.world.procs[pid]
        self.assertFalse(ws.is_worker_running())
        self.assertEqual(ws.worker_status(), "Stopped")

    def test_record_without_start_time_is_rejected_and_never_signalled(self):
        """Reviewer's blocker: a bare {"pid": N} must not be treated as ours."""
        ws.start_worker()
        worker = ws.load_state()["worker"]
        self.assertIsNotNone(worker)
        assert worker is not None
        pid = worker["pid"]
        ws._atomic_write(ws._state_path(), {"enabled": True, "worker": {"pid": pid}})
        self.simulate_menubar_restart()
        self.assertFalse(ws.is_worker_running())
        self.assertIsNone(ws.worker_pid())
        self.assertEqual(ws.worker_status(), "Stopped")
        self.assertTrue(ws.stop_worker(timeout=0.3))
        self.assertEqual(self.world.signals, [])      # nothing signalled
        self.assertTrue(self.world.alive(pid))        # bystander untouched

    def test_unknown_pid_without_start_time_is_never_signalled(self):
        """Exactly the reproduction reported: {"worker": {"pid": 4242}}."""
        self.world.procs[4242] = {
            "start_time": "Mon Oct 19 09:30:00 2026",
            "comm": "/usr/sbin/unrelated",
            "ignores_term": False,
        }
        ws._atomic_write(ws._state_path(), {"enabled": True, "worker": {"pid": 4242}})
        self.assertFalse(ws.is_worker_running())
        self.assertIsNone(ws.worker_pid())
        self.assertTrue(ws.stop_worker(timeout=0.3))
        self.assertEqual(self.world.signals, [])
        self.assertTrue(self.world.alive(4242))

    def test_empty_or_whitespace_start_time_is_rejected(self):
        for bad in ("", "   ", None, 12345, [], {}):
            ws._atomic_write(
                ws._state_path(),
                {"enabled": True, "worker": {"pid": 4242, "start_time": bad}},
            )
            self.assertFalse(
                ws.is_worker_running(), "start_time=%r must fail closed" % (bad,)
            )
            self.assertIsNone(ws.worker_pid())

    def test_invalid_pid_values_are_rejected(self):
        for bad in (None, 0, -5, "1234", True, 12.5, [], {}):
            ws._atomic_write(
                ws._state_path(),
                {
                    "enabled": True,
                    "worker": {"pid": bad, "start_time": "Sun Oct 18 12:00:00 2026"},
                },
            )
            self.assertFalse(
                ws.is_worker_running(), "pid=%r must fail closed" % (bad,)
            )
            self.assertIsNone(ws.worker_pid())

    def test_non_dict_worker_record_does_not_raise(self):
        for bad in ("a string", 42, ["list"], True):
            ws._atomic_write(ws._state_path(), {"enabled": True, "worker": bad})
            self.assertFalse(ws.is_worker_running())
            self.assertIsNone(ws.worker_pid())
            self.assertEqual(ws.worker_status(), "Stopped")
            self.assertTrue(ws.stop_worker(timeout=0.3))

    def test_corrupt_state_file_does_not_raise(self):
        with open(ws._state_path(), "w") as handle:
            handle.write("{definitely not json")
        self.assertFalse(ws.is_worker_running())
        self.assertIsNone(ws.worker_pid())
        self.assertEqual(ws.worker_status(), "Stopped")

    def test_discard_invalid_state_clears_malformed_records(self):
        ws._atomic_write(ws._state_path(), {"enabled": True, "worker": {"pid": 4242}})
        self.assertTrue(ws.discard_invalid_state())
        self.assertIsNone(ws.load_state()["worker"])
        self.assertFalse(ws.load_state()["enabled"])

    def test_discard_invalid_state_keeps_valid_records(self):
        ws.start_worker()
        before = ws.load_state()["worker"]
        self.assertFalse(ws.discard_invalid_state())
        self.assertEqual(ws.load_state()["worker"], before)

    def test_start_time_mismatch_is_rejected_even_when_pid_is_alive(self):
        ws.start_worker()
        worker = dict(ws.load_state()["worker"] or {})
        worker["start_time"] = "Mon Oct 19 09:30:00 2026"  # wrong
        ws.save_state(True, worker)
        self.simulate_menubar_restart()
        self.assertFalse(ws.is_worker_running())
        self.assertTrue(ws.stop_worker(timeout=0.3))
        self.assertEqual(self.world.signals, [])

    def test_comm_rewritten_after_exec_does_not_invalidate_identity(self):
        """macOS rewrites `comm` for a venv python shim just after exec.

        Observed on this machine: the venv path at spawn, then the real
        framework binary. start_time is the authoritative anchor, so the worker
        must still be recognised as ours.
        """
        ws.start_worker()
        pid = ws.worker_pid()
        self.assertIsNotNone(pid)
        self.world.procs[pid]["comm"] = "/System/.../Python"  # same proc, new comm
        self.simulate_menubar_restart()
        self.assertTrue(ws.is_worker_running())
        self.assertEqual(ws.worker_pid(), pid)

    def test_recycled_pid_with_same_comm_is_still_rejected(self):
        """Identity must not hinge on comm: a reused PID can share it."""
        ws.start_worker()
        pid = ws.worker_pid()
        comm = self.world.procs[pid]["comm"]
        self.simulate_menubar_restart()
        del self.world.procs[pid]
        self.world.procs[pid] = {
            "start_time": "Mon Oct 19 09:30:00 2026",  # later start = recycled
            "comm": comm,
            "ignores_term": False,
        }
        self.assertFalse(ws.is_worker_running())
        self.assertTrue(ws.stop_worker(timeout=0.3))
        self.assertEqual(self.world.signals, [])


class StateCorruptionAfterStartTests(SupervisorTestCase):
    """Reviewer's blocker: corrupted state must not orphan our live child."""

    def test_disable_still_kills_our_child_when_start_time_is_lost(self):
        ws.start_worker()
        pid = ws.worker_pid()
        self.assertIsNotNone(pid)
        child = ws._CHILD["popen"]

        self.corrupt_state_losing_start_time()
        # Persisted identity is now unverifiable...
        self.assertIsNone(ws._valid_worker_record(
            ws._read_json(ws._state_path(), {}).get("worker")
        ))
        # ...but our own child is still tracked and still reported.
        self.assertTrue(ws.is_worker_running())
        self.assertEqual(ws.worker_pid(), pid)

        self.assertTrue(ws.stop_worker(timeout=1.0))
        self.assertGreaterEqual(child.terminate_calls, 1)   # actually signalled
        self.assertFalse(self.world.alive(pid))             # actually exited
        self.assertIsNone(ws._CHILD["popen"])
        self.assertFalse(ws.is_worker_running())

    def test_disable_reports_failure_while_our_child_survives(self):
        ws.start_worker()
        pid = ws.worker_pid()
        self.world.procs[pid]["ignores_term"] = True
        self.corrupt_state_losing_start_time()

        # Make SIGKILL ineffective too, so the child cannot be stopped at all.
        original = self.world.send_signal

        def deaf(pid, sig):
            self.world.signals.append((int(pid), sig))
            return True

        self.world.send_signal = deaf
        ws._signal = deaf
        try:
            self.assertFalse(ws.stop_worker(timeout=0.4))   # no false success
        finally:
            self.world.send_signal = original
            ws._signal = original
        self.assertIsNotNone(ws._CHILD["popen"])            # handle retained
        self.assertTrue(self.world.alive(pid))
        self.assertTrue(ws.is_worker_running())             # still tracked

        # And once it can be killed, Disable succeeds for real.
        self.assertTrue(ws.stop_worker(timeout=1.0))
        self.assertFalse(self.world.alive(pid))

    def test_non_dict_state_does_not_orphan_our_child(self):
        ws.start_worker()
        pid = ws.worker_pid()
        ws._atomic_write(ws._state_path(), {"enabled": True, "worker": "garbage"})
        self.assertTrue(ws.is_worker_running())
        self.assertEqual(ws.worker_pid(), pid)
        self.assertTrue(ws.stop_worker(timeout=1.0))
        self.assertFalse(self.world.alive(pid))

    def test_deleted_state_file_does_not_orphan_our_child(self):
        ws.start_worker()
        pid = ws.worker_pid()
        os.remove(ws._state_path())
        self.assertTrue(ws.is_worker_running())
        self.assertEqual(ws.worker_pid(), pid)
        self.assertTrue(ws.stop_worker(timeout=1.0))
        self.assertFalse(self.world.alive(pid))

    def test_child_sigkill_escalation_via_popen(self):
        ws.start_worker()
        pid = ws.worker_pid()
        child = ws._CHILD["popen"]
        self.world.procs[pid]["ignores_term"] = True
        self.corrupt_state_losing_start_time()
        self.assertTrue(ws.stop_worker(timeout=0.4))
        self.assertGreaterEqual(child.terminate_calls, 1)
        self.assertGreaterEqual(child.kill_calls, 1)
        self.assertFalse(self.world.alive(pid))

    def test_corrupted_state_with_live_child_still_shows_running(self):
        ws.start_worker()
        pid = ws.worker_pid()
        start_time = ws._CHILD["start_time"]
        self.corrupt_state_losing_start_time()
        ws._atomic_write(
            ws._heartbeat_path(),
            {
                "present": True,
                "pid": pid,
                "start_time": start_time,
                "ts": time.time(),
            },
        )
        self.assertEqual(ws.worker_status(), "Running (Present)")


class StatusTests(SupervisorTestCase):
    def _age_out_startup_grace(self):
        """Pretend the worker started long ago, so startup grace no longer applies."""
        state = ws.load_state()
        worker = dict(state["worker"] or {})
        worker["started_at"] = time.time() - (
            config.WORKER_STARTUP_GRACE_SECONDS + 5.0
        )
        ws.save_state(state["enabled"], worker)

    def _beat(self, present, age=0.0, pid=None, state="present", start_time=-1):
        """Write a heartbeat. Defaults carry FULL identity (pid + start_time)."""
        if start_time == -1:
            start_time = ws._tracked_start_time()
        ws._atomic_write(
            ws._heartbeat_path(),
            {
                "state": state,
                "present": present,
                "pid": pid if pid is not None else ws.worker_pid(),
                "start_time": start_time,
                "ts": time.time() - age,
            },
        )

    def test_stopped_when_no_worker(self):
        self.assertEqual(ws.worker_status(), "Stopped")

    def test_running_present(self):
        ws.start_worker()
        self._beat(True)
        self.assertEqual(ws.worker_status(), "Running (Present)")

    def test_running_away(self):
        ws.start_worker()
        self._beat(False, state="idle")
        self.assertEqual(ws.worker_status(), "Running (Away)")

    def test_stale_heartbeat_is_unresponsive_not_stale_presence(self):
        ws.start_worker()
        self._age_out_startup_grace()
        self._beat(True, age=config.HEARTBEAT_STALE_SECONDS + 2.0)
        self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_missing_heartbeat_is_unresponsive(self):
        ws.start_worker()
        self._age_out_startup_grace()
        ws.clear_heartbeat()
        self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_missing_heartbeat_right_after_start_is_starting_not_unresponsive(self):
        ws.start_worker()
        ws.clear_heartbeat()
        self.assertEqual(ws.worker_status(), "Starting...")

    def test_startup_grace_expires_into_unresponsive(self):
        ws.start_worker()
        ws.clear_heartbeat()
        self.assertEqual(ws.worker_status(), "Starting...")
        self._age_out_startup_grace()
        self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_first_heartbeat_during_grace_wins_over_starting(self):
        ws.start_worker()
        self._beat(True)
        self.assertEqual(ws.worker_status(), "Running (Present)")

    def test_worker_that_hangs_during_startup_grace_is_unresponsive(self):
        """A stale beat from OUR pid means stuck, not still starting."""
        ws.start_worker()
        self._beat(True, age=config.HEARTBEAT_STALE_SECONDS + 2.0)
        self.assertTrue(ws._within_startup_grace())  # grace is still open
        self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_heartbeat_from_another_pid_is_unresponsive(self):
        ws.start_worker()
        self._age_out_startup_grace()
        self._beat(True, pid=999999)
        self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_corrupt_heartbeat_file_is_unresponsive(self):
        ws.start_worker()
        self._age_out_startup_grace()
        with open(ws._heartbeat_path(), "w") as handle:
            handle.write("{not json")
        self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_heartbeat_without_pid_is_not_rendered_as_live(self):
        """Reviewer's repro: {"present": true, "ts": now} has no identity."""
        ws.start_worker()
        self._age_out_startup_grace()
        ws._atomic_write(
            ws._heartbeat_path(), {"present": True, "ts": time.time()}
        )
        self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_heartbeat_with_pid_and_ts_but_no_start_time_is_not_live(self):
        """Full identity is mandatory: pid + ts alone must not show Running."""
        ws.start_worker()
        self._age_out_startup_grace()
        ws._atomic_write(
            ws._heartbeat_path(),
            {"present": True, "pid": ws.worker_pid(), "ts": time.time()},
        )
        self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_missing_or_empty_heartbeat_start_time_matrix(self):
        ws.start_worker()
        self._age_out_startup_grace()
        pid = ws.worker_pid()
        for bad in (None, "", "   ", 12345, 0, True, [], {}):
            ws._atomic_write(
                ws._heartbeat_path(),
                {
                    "present": True,
                    "pid": pid,
                    "start_time": bad,
                    "ts": time.time(),
                },
            )
            self.assertEqual(
                ws.worker_status(),
                "Unresponsive",
                "start_time=%r must never render Running" % (bad,),
            )

    def test_full_identity_heartbeat_is_the_only_live_path(self):
        ws.start_worker()
        self._age_out_startup_grace()
        self._beat(True)   # helper now includes pid AND start_time
        self.assertEqual(ws.worker_status(), "Running (Present)")

    def test_nonnumeric_ts_does_not_raise(self):
        ws.start_worker()
        self._age_out_startup_grace()
        for bad in ("soon", None, [], {}, True):
            ws._atomic_write(
                ws._heartbeat_path(),
                {"present": True, "pid": ws.worker_pid(), "ts": bad},
            )
            self.assertEqual(
                ws.worker_status(), "Unresponsive", "ts=%r must fail closed" % (bad,)
            )

    def test_invalid_heartbeat_pid_types_do_not_raise(self):
        ws.start_worker()
        self._age_out_startup_grace()
        for bad in ("1234", 0, -1, None, True, 12.5, []):
            ws._atomic_write(
                ws._heartbeat_path(),
                {"present": True, "pid": bad, "ts": time.time()},
            )
            self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_non_dict_heartbeat_does_not_raise(self):
        ws.start_worker()
        self._age_out_startup_grace()
        for bad in ("[]", "42", '"a string"', "[1, 2, 3]"):
            with open(ws._heartbeat_path(), "w") as handle:
                handle.write(bad)
            self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_heartbeat_start_time_mismatch_is_unresponsive(self):
        ws.start_worker()
        self._age_out_startup_grace()
        ws._atomic_write(
            ws._heartbeat_path(),
            {
                "present": True,
                "pid": ws.worker_pid(),
                "start_time": "Mon Oct 19 09:30:00 2026",  # not our worker
                "ts": time.time(),
            },
        )
        self.assertEqual(ws.worker_status(), "Unresponsive")

    def test_matching_heartbeat_start_time_is_live(self):
        ws.start_worker()
        worker = ws.load_state()["worker"]
        assert worker is not None
        self._age_out_startup_grace()
        ws._atomic_write(
            ws._heartbeat_path(),
            {
                "present": False,
                "pid": worker["pid"],
                "start_time": worker["start_time"],
                "ts": time.time(),
            },
        )
        self.assertEqual(ws.worker_status(), "Running (Away)")

    def test_status_never_raises_across_a_matrix_of_garbage(self):
        ws.start_worker()
        garbage = [
            "null", "[]", "{}", '{"ts": "x"}', '{"pid": null}',
            '{"ts": 1, "pid": "a"}', '{"present": true}', "not json at all",
        ]
        for payload in garbage:
            with open(ws._heartbeat_path(), "w") as handle:
                handle.write(payload)
            status = ws.worker_status()  # must not raise
            self.assertIn(status, ("Stopped", "Starting...", "Unresponsive"))

    def test_heartbeat_write_is_atomic_and_round_trips(self):
        ws.write_heartbeat("present", True)
        beat = ws.read_heartbeat()
        self.assertIsNotNone(beat)
        assert beat is not None  # narrow for type checkers
        self.assertEqual(beat["state"], "present")
        self.assertTrue(beat["present"])
        self.assertEqual(beat["pid"], os.getpid())
        self.assertFalse(os.path.exists(ws._heartbeat_path() + ".tmp"))


class LoginItemTests(SupervisorTestCase):
    """`launchctl enable|disable` only -- never bootout, which would kill the UI."""

    def setUp(self):
        super(LoginItemTests, self).setUp()
        self.label = config.MENUBAR_LAUNCHD_LABEL
        self.disabled = {}
        self.commands = []
        ws._run = self._fake_run
        self._orig_installed = ws.login_item_installed
        ws.login_item_installed = lambda: True

    def tearDown(self):
        ws.login_item_installed = self._orig_installed
        super(LoginItemTests, self).tearDown()

    def _fake_run(self, args, timeout=10):
        self.commands.append(list(args))

        class R(object):
            returncode = 0
            stdout = ""
            stderr = ""

        result = R()
        if args[1] == "print-disabled":
            lines = ["disabled services = {"]
            for label, state in self.disabled.items():
                lines.append('\t\t"%s" => %s' % (label, state))
            lines.append("}")
            result.stdout = "\n".join(lines)
        elif args[1] in ("enable", "disable"):
            self.disabled[self.label] = (
                "disabled" if args[1] == "disable" else "enabled"
            )
        return result

    def test_enable_uses_launchctl_enable_and_verifies_by_requery(self):
        self.assertTrue(ws.enable_login_item())
        verbs = [c[1] for c in self.commands]
        self.assertIn("enable", verbs)
        self.assertIn("print-disabled", verbs)   # verified, not assumed
        self.assertTrue(ws.is_login_item_enabled())

    def test_disable_uses_launchctl_disable_and_verifies(self):
        self.assertTrue(ws.disable_login_item())
        self.assertIn("disable", [c[1] for c in self.commands])
        self.assertFalse(ws.is_login_item_enabled())

    def test_toggle_round_trip(self):
        ws.disable_login_item()
        self.assertFalse(ws.is_login_item_enabled())
        ws.enable_login_item()
        self.assertTrue(ws.is_login_item_enabled())

    def test_never_calls_bootout_or_bootstrap(self):
        ws.enable_login_item()
        ws.disable_login_item()
        verbs = [c[1] for c in self.commands]
        self.assertNotIn("bootout", verbs)
        self.assertNotIn("bootstrap", verbs)

    def test_absent_from_disabled_map_counts_as_enabled_when_installed(self):
        self.disabled = {"com.other.thing": "disabled"}
        self.assertTrue(ws.is_login_item_enabled())

    def test_absent_and_not_installed_is_not_enabled(self):
        ws.login_item_installed = lambda: False
        self.disabled = {}
        self.assertFalse(ws.is_login_item_enabled())

    def test_targets_the_menubar_label_in_the_gui_domain(self):
        ws.enable_login_item()
        target = [c for c in self.commands if c[1] == "enable"][0][2]
        self.assertEqual(target, "gui/%d/%s" % (os.getuid(), self.label))


if __name__ == "__main__":
    unittest.main(verbosity=2)
