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
