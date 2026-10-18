"""Unit tests for PresenceStateMachine. No hardware, no clock, no subprocess."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from presence_automation.state_machine import (  # noqa: E402
    Event,
    PresenceStateMachine,
    State,
)


class ArrivalDebounceTests(unittest.TestCase):
    def setUp(self):
        self.sm = PresenceStateMachine(
            arrival_samples=3, absence_seconds=30.0, cooldown_seconds=60.0
        )

    def test_starts_idle(self):
        self.assertIs(self.sm.state, State.IDLE)

    def test_single_positive_sample_does_not_fire_arrival(self):
        self.assertIsNone(self.sm.update(True, 0.0))
        self.assertIs(self.sm.state, State.ARRIVING)

    def test_two_positive_samples_still_do_not_fire(self):
        self.sm.update(True, 0.0)
        self.assertIsNone(self.sm.update(True, 1.0))
        self.assertIs(self.sm.state, State.ARRIVING)

    def test_third_consecutive_positive_fires_arrived_once(self):
        self.sm.update(True, 0.0)
        self.sm.update(True, 1.0)
        self.assertIs(self.sm.update(True, 2.0), Event.ARRIVED)
        self.assertIs(self.sm.state, State.PRESENT)
        # Steady presence must not re-fire.
        self.assertIsNone(self.sm.update(True, 3.0))
        self.assertIsNone(self.sm.update(True, 4.0))

    def test_broken_streak_resets_to_idle(self):
        self.sm.update(True, 0.0)
        self.sm.update(True, 1.0)
        self.assertIsNone(self.sm.update(False, 2.0))
        self.assertIs(self.sm.state, State.IDLE)
        self.assertEqual(self.sm.positive_streak, 0)

    def test_glance_pattern_never_arrives(self):
        # on/off/on/off/on/off: 10 samples, never 3 in a row.
        for i in range(10):
            self.assertIsNone(self.sm.update(i % 2 == 0, float(i)))
        self.assertIsNot(self.sm.state, State.PRESENT)

    def test_streak_restarts_from_scratch_after_break(self):
        self.sm.update(True, 0.0)
        self.sm.update(True, 1.0)
        self.sm.update(False, 2.0)
        self.sm.update(True, 3.0)
        self.sm.update(True, 4.0)
        self.assertIs(self.sm.update(True, 5.0), Event.ARRIVED)

    def test_arrival_samples_one_fires_immediately(self):
        sm = PresenceStateMachine(arrival_samples=1)
        self.assertIs(sm.update(True, 0.0), Event.ARRIVED)

    def test_invalid_arrival_samples_rejected(self):
        with self.assertRaises(ValueError):
            PresenceStateMachine(arrival_samples=0)


class AbsenceTests(unittest.TestCase):
    def setUp(self):
        self.sm = PresenceStateMachine(
            arrival_samples=3, absence_seconds=30.0, cooldown_seconds=60.0
        )
        self.sm.update(True, 0.0)
        self.sm.update(True, 1.0)
        assert self.sm.update(True, 2.0) is Event.ARRIVED

    def test_first_negative_enters_leaving_without_event(self):
        self.assertIsNone(self.sm.update(False, 10.0))
        self.assertIs(self.sm.state, State.LEAVING)

    def test_absence_shorter_than_window_does_not_depart(self):
        self.sm.update(False, 10.0)
        for t in range(11, 40):  # 29s of absence
            self.assertIsNone(self.sm.update(False, float(t)))
        self.assertIs(self.sm.state, State.LEAVING)

    def test_full_absence_window_fires_departed(self):
        self.sm.update(False, 10.0)
        for t in range(11, 40):
            self.sm.update(False, float(t))
        self.assertIs(self.sm.update(False, 40.0), Event.DEPARTED)
        self.assertIs(self.sm.state, State.COOLDOWN)

    def test_return_before_window_closes_cancels_departure(self):
        self.sm.update(False, 10.0)
        self.sm.update(False, 20.0)
        # A CONFIRMED return (3 consecutive positives) cancels departure.
        self.assertIsNone(self.sm.update(True, 23.0))
        self.assertIsNone(self.sm.update(True, 24.0))
        self.assertIsNone(self.sm.update(True, 25.0))
        self.assertIs(self.sm.state, State.PRESENT)
        # Absence timer restarts from zero, so 29s more is still not enough.
        self.sm.update(False, 30.0)
        self.assertIsNone(self.sm.update(False, 59.0))
        self.assertIs(self.sm.state, State.LEAVING)

    def test_departed_fires_only_once(self):
        self.sm.update(False, 10.0)
        self.assertIs(self.sm.update(False, 45.0), Event.DEPARTED)
        self.assertIsNone(self.sm.update(False, 46.0))
        self.assertIsNone(self.sm.update(False, 47.0))


class ReturnDebounceTests(unittest.TestCase):
    """Regression: a stray positive frame must not cancel an absence countdown.

    Real failure from deskpresence.log: 1191 present->leaving vs 1182
    leaving->present flips with only 8 DEPARTED in 2802 lines, because a single
    false-positive sample reset the whole 30s timer.
    """

    def setUp(self):
        self.sm = PresenceStateMachine(
            arrival_samples=3,
            absence_seconds=30.0,
            cooldown_seconds=60.0,
            return_samples=3,
        )
        self.sm.update(True, 0.0)
        self.sm.update(True, 1.0)
        assert self.sm.update(True, 2.0) is Event.ARRIVED
        self.sm.update(False, 10.0)   # absence starts at t=10
        assert self.sm.state is State.LEAVING

    def test_single_stray_positive_does_not_cancel_absence(self):
        self.assertIsNone(self.sm.update(True, 15.0))
        self.assertIs(self.sm.state, State.LEAVING)
        self.assertEqual(self.sm.return_streak, 1)

    def test_single_stray_positive_does_not_rewind_elapsed_absence(self):
        self.sm.update(False, 20.0)
        self.assertEqual(self.sm.seconds_absent(20.0), 10.0)
        self.sm.update(True, 21.0)            # stray frame
        self.sm.update(False, 22.0)
        # Still measured from the ORIGINAL t=10, not restarted at t=21.
        self.assertEqual(self.sm.seconds_absent(22.0), 12.0)

    def test_two_stray_positives_still_do_not_cancel(self):
        self.sm.update(True, 15.0)
        self.assertIsNone(self.sm.update(True, 16.0))
        self.assertIs(self.sm.state, State.LEAVING)

    def test_three_consecutive_positives_confirm_return(self):
        self.sm.update(True, 15.0)
        self.sm.update(True, 16.0)
        self.assertIsNone(self.sm.update(True, 17.0))
        self.assertIs(self.sm.state, State.PRESENT)
        self.assertEqual(self.sm.seconds_absent(17.0), 0.0)
        self.assertEqual(self.sm.return_streak, 0)

    def test_negative_sample_resets_the_return_streak(self):
        self.sm.update(True, 15.0)
        self.sm.update(True, 16.0)
        self.sm.update(False, 17.0)           # streak broken
        self.assertEqual(self.sm.return_streak, 0)
        self.sm.update(True, 18.0)            # starts over, not 3rd positive
        self.assertIs(self.sm.state, State.LEAVING)

    def test_departure_fires_despite_single_sample_flicker(self):
        """The exact log pattern: alternating flips must still reach DEPARTED."""
        event = None
        t = 11.0
        while t <= 45.0:
            event = self.sm.update(False, t)
            if event:
                break
            t += 1.0
            event = self.sm.update(True, t)   # stray false positive
            if event:
                break
            t += 1.0
        self.assertIs(event, Event.DEPARTED)
        self.assertIs(self.sm.state, State.COOLDOWN)

    def test_absence_is_monotonic_across_flicker(self):
        last = 0.0
        for t in range(11, 40):
            present = (t % 3 == 0)           # a stray positive every 3rd sample
            self.sm.update(present, float(t))
            if self.sm.state is not State.LEAVING:
                self.fail("flicker cancelled the countdown at t=%s" % t)
            current = self.sm.seconds_absent(float(t))
            self.assertGreaterEqual(current, last)
            last = current
        self.assertIs(self.sm.update(False, 41.0), Event.DEPARTED)

    def test_replay_of_recorded_log_pattern_reaches_departed(self):
        """Replay real timings from deskpresence.log 16:19:38 - 16:20:52."""
        sm = PresenceStateMachine(
            arrival_samples=3, absence_seconds=30.0,
            cooldown_seconds=60.0, return_samples=3,
        )
        sm.update(True, 0.0)
        sm.update(True, 1.0)
        sm.update(True, 2.0)
        # (offset_seconds, present) taken from the logged flip timestamps.
        flips = [
            (0.0, False), (3.5, True), (4.7, False), (7.1, True),
            (8.3, False), (11.8, True), (13.0, False), (16.4, True),
            (17.6, False), (29.3, True), (31.6, False), (46.9, True),
            (48.0, False), (56.1, True), (57.3, False), (59.5, True),
            (60.7, False), (61.8, True), (63.0, False), (64.1, True),
            (65.2, False), (67.6, True), (69.9, False), (74.5, True),
        ]
        event = None
        for offset, present in flips:
            event = sm.update(present, 10.0 + offset)
            if event is Event.DEPARTED:
                break
        self.assertIs(
            event, Event.DEPARTED,
            "replayed real flicker pattern still never departs",
        )

    def test_absence_remaining_counts_down_for_the_preview(self):
        self.assertEqual(self.sm.absence_remaining(10.0), 30.0)
        self.sm.update(False, 20.0)
        self.assertEqual(self.sm.absence_remaining(20.0), 20.0)
        self.sm.update(True, 21.0)            # stray positive
        self.assertEqual(self.sm.absence_remaining(25.0), 15.0)
        self.assertEqual(self.sm.absence_remaining(99.0), 0.0)

    def test_absence_remaining_is_zero_when_present(self):
        sm = PresenceStateMachine(arrival_samples=1)
        self.assertIs(sm.update(True, 0.0), Event.ARRIVED)
        self.assertEqual(sm.absence_remaining(5.0), 0.0)

    def test_invalid_return_samples_rejected(self):
        with self.assertRaises(ValueError):
            PresenceStateMachine(return_samples=0)

    def test_return_samples_one_restores_instant_cancel(self):
        sm = PresenceStateMachine(
            arrival_samples=1, absence_seconds=30.0, return_samples=1
        )
        sm.update(True, 0.0)
        sm.update(False, 1.0)
        self.assertIs(sm.state, State.LEAVING)
        sm.update(True, 2.0)
        self.assertIs(sm.state, State.PRESENT)

    def test_default_return_samples_comes_from_config(self):
        import presence_automation.config as cfg
        sm = PresenceStateMachine()
        self.assertEqual(sm.return_samples, cfg.RETURN_CONFIRM_SAMPLES)


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.sm = PresenceStateMachine(
            arrival_samples=3, absence_seconds=30.0, cooldown_seconds=60.0
        )
        self.sm.update(True, 0.0)
        self.sm.update(True, 1.0)
        self.sm.update(True, 2.0)
        self.sm.update(False, 10.0)
        assert self.sm.update(False, 41.0) is Event.DEPARTED  # cooldown -> 101.0

    def test_cooldown_blocks_relaunch_thrash(self):
        for t in range(42, 101):
            self.assertIsNone(self.sm.update(True, float(t)))
        self.assertIs(self.sm.state, State.COOLDOWN)

    def test_repeated_brief_departures_and_returns_do_not_relaunch(self):
        t = 42.0
        for _ in range(10):  # 10 quick in/out cycles inside the cooldown
            self.assertIsNone(self.sm.update(True, t))
            self.assertIsNone(self.sm.update(False, t + 1.0))
            t += 2.0
        self.assertIs(self.sm.state, State.COOLDOWN)

    def test_cooldown_expiry_returns_to_idle_and_honours_sample(self):
        self.sm.update(True, 90.0)  # still blocked
        self.assertIsNone(self.sm.update(True, 101.0))  # expired -> ARRIVING
        self.assertIs(self.sm.state, State.ARRIVING)
        self.assertEqual(self.sm.positive_streak, 1)

    def test_rearrival_after_cooldown_needs_full_streak_again(self):
        self.assertIsNone(self.sm.update(True, 101.0))
        self.assertIsNone(self.sm.update(True, 102.0))
        self.assertIs(self.sm.update(True, 103.0), Event.ARRIVED)

    def test_cooldown_expiry_with_negative_sample_goes_idle(self):
        self.assertIsNone(self.sm.update(False, 101.0))
        self.assertIs(self.sm.state, State.IDLE)

    def test_cooldown_remaining_decreases(self):
        self.assertAlmostEqual(self.sm.cooldown_remaining(41.0), 60.0)
        self.assertAlmostEqual(self.sm.cooldown_remaining(71.0), 30.0)
        self.assertAlmostEqual(self.sm.cooldown_remaining(200.0), 0.0)


class FullCycleTests(unittest.TestCase):
    def test_arrive_depart_arrive_emits_exactly_two_then_one_events(self):
        sm = PresenceStateMachine(
            arrival_samples=3, absence_seconds=30.0, cooldown_seconds=60.0
        )
        events = []
        script = (
            [(True, t) for t in (0.0, 1.0, 2.0, 3.0)]          # arrive
            + [(False, t) for t in (4.0, 20.0, 34.1)]          # depart at 34.1
            + [(True, t) for t in (40.0, 60.0, 90.0)]          # blocked
            + [(True, t) for t in (95.0, 96.0, 97.0)]          # re-arrive
        )
        for present, t in script:
            ev = sm.update(present, t)
            if ev is not None:
                events.append((ev, t))
        self.assertEqual(
            events,
            [(Event.ARRIVED, 2.0), (Event.DEPARTED, 34.1), (Event.ARRIVED, 97.0)],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
