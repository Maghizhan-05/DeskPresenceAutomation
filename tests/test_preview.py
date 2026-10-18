"""Preview buffer + detector opt-in tests. No camera, no window server."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from presence_automation import config, preview_buffer as pb  # noqa: E402
from presence_automation.preview_window import (  # noqa: E402
    NO_WORKER_TEXT,
    WAITING_TEXT,
    PreviewController,
    countdown_text,
)


def _frame(fill=0):
    return bytes(bytearray([fill]) * pb.frame_bytes())


class BufferLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.name = "deskpresence_test_%d" % os.getpid()
        pb.unlink_stale(self.name)
        self.writer = None

    def tearDown(self):
        if self.writer is not None and not self.writer.closed:
            self.writer.close()
        pb.unlink_stale(self.name)

    def test_segment_size_is_header_plus_one_frame(self):
        self.assertEqual(
            pb.segment_size(), pb.HEADER_SIZE + pb.frame_bytes()
        )
        self.assertEqual(
            pb.frame_bytes(),
            config.PREVIEW_WIDTH * config.PREVIEW_HEIGHT * config.PREVIEW_CHANNELS,
        )

    def test_write_then_read_roundtrip(self):
        self.writer = pb.PreviewWriter(self.name)
        self.writer.write(_frame(7), True, (1, 2, 3, 4))
        reader = pb.PreviewReader(self.name)
        try:
            seq, present, box, data = reader.read()
            self.assertEqual(seq, 1)
            self.assertTrue(present)
            self.assertEqual(box, (1, 2, 3, 4))
            self.assertEqual(len(data), pb.frame_bytes())
            self.assertEqual(data[0], 7)
        finally:
            reader.close()

    def test_buffer_never_grows_across_many_writes(self):
        self.writer = pb.PreviewWriter(self.name)
        size = self.writer.size
        for i in range(200):
            self.writer.write(_frame(i % 256), i % 2 == 0)
            self.assertEqual(self.writer.size, size, "buffer grew at write %d" % i)
        self.assertGreaterEqual(size, pb.segment_size())

    def test_sequence_increments_so_readers_see_fresh_frames(self):
        self.writer = pb.PreviewWriter(self.name)
        self.assertEqual(self.writer.write(_frame(1), False), 1)
        self.assertEqual(self.writer.write(_frame(2), False), 2)
        reader = pb.PreviewReader(self.name)
        try:
            self.assertEqual(reader.read()[0], 2)
        finally:
            reader.close()

    def test_frame_is_overwritten_in_place_not_appended(self):
        self.writer = pb.PreviewWriter(self.name)
        self.writer.write(_frame(1), False)
        self.writer.write(_frame(9), False)
        reader = pb.PreviewReader(self.name)
        try:
            data = reader.read()[3]
            self.assertEqual(set(data[:100]), {9})
        finally:
            reader.close()

    def test_wrong_sized_frame_is_rejected(self):
        self.writer = pb.PreviewWriter(self.name)
        with self.assertRaises(ValueError):
            self.writer.write(b"too short", True)

    def test_read_before_any_write_returns_none(self):
        self.writer = pb.PreviewWriter(self.name)
        reader = pb.PreviewReader(self.name)
        try:
            self.assertIsNone(reader.read())
        finally:
            reader.close()

    def test_close_unlinks_segment(self):
        self.writer = pb.PreviewWriter(self.name)
        self.writer.write(_frame(3), True)
        self.writer.close()
        with self.assertRaises(pb.PreviewUnavailable):
            pb.PreviewReader(self.name)

    def test_reader_without_worker_raises(self):
        with self.assertRaises(pb.PreviewUnavailable):
            pb.PreviewReader(self.name)

    def test_writer_unlinks_stale_segment_from_crashed_worker(self):
        orphan = pb.PreviewWriter(self.name)
        orphan.write(_frame(5), True)
        # Simulate a crash: the process dies without close(), segment survives.
        orphan.closed = True
        fresh = pb.PreviewWriter(self.name)
        self.writer = fresh
        reader = pb.PreviewReader(self.name)
        try:
            # Fresh segment: sequence restarts, no stale frame is served.
            self.assertIsNone(reader.read())
        finally:
            reader.close()

    def test_unlink_stale_reports_false_when_nothing_exists(self):
        self.assertFalse(pb.unlink_stale(self.name))

    def test_write_after_close_raises(self):
        self.writer = pb.PreviewWriter(self.name)
        self.writer.close()
        with self.assertRaises(pb.PreviewUnavailable):
            self.writer.write(_frame(1), True)


class FakeWriter(object):
    def __init__(self):
        self.writes = []

    def write(self, data, present, box=None):
        self.writes.append((len(data), present, box))
        return len(self.writes)


class FakeCapture(object):
    def __init__(self, frame):
        self._frame = frame

    def read(self):
        return True, self._frame


class DetectorPreviewTests(unittest.TestCase):
    """The preview path must never change the detection result."""

    def setUp(self):
        try:
            import numpy  # noqa: F401
            from presence_automation.detector import Detector
        except Exception as exc:  # pragma: no cover
            self.skipTest("detector deps unavailable: %s" % exc)
        import numpy as np

        self.np = np
        self.Detector = Detector
        self.det = Detector.__new__(Detector)   # no camera, no cascade load
        self.det._capture = FakeCapture(np.zeros((480, 640, 3), dtype=np.uint8))

        class _Cascade(object):
            def detectMultiScale(self, *_a, **_k):
                return []

        self.det._cascade = _Cascade()

    def test_preview_off_by_default_touches_no_writer(self):
        writer = FakeWriter()
        result = self.det.sample()           # default: publish_preview=False
        self.assertFalse(result)
        self.assertEqual(writer.writes, [])

    def test_preview_off_explicitly_touches_no_writer(self):
        writer = FakeWriter()
        self.det.sample(publish_preview=False, preview_writer=writer)
        self.assertEqual(writer.writes, [])

    def test_preview_on_writes_exactly_one_fixed_size_frame(self):
        writer = FakeWriter()
        self.det.sample(publish_preview=True, preview_writer=writer)
        self.assertEqual(len(writer.writes), 1)
        self.assertEqual(writer.writes[0][0], pb.frame_bytes())

    def test_detection_result_is_identical_with_and_without_preview(self):
        writer = FakeWriter()
        without = self.det.sample()
        with_preview = self.det.sample(publish_preview=True, preview_writer=writer)
        self.assertEqual(without, with_preview)

    def test_preview_write_failure_does_not_break_detection(self):
        class Exploding(object):
            def write(self, *_a, **_k):
                raise RuntimeError("boom")

        result = self.det.sample(publish_preview=True, preview_writer=Exploding())
        self.assertFalse(result)             # still returned a clean verdict

    def test_publish_true_without_writer_is_safe(self):
        self.assertFalse(self.det.sample(publish_preview=True, preview_writer=None))

    def test_signature_defaults_keep_preview_off(self):
        import inspect

        sig = inspect.signature(self.Detector.sample)
        self.assertIs(sig.parameters["publish_preview"].default, False)
        self.assertIsNone(sig.parameters["preview_writer"].default)


class CountdownTextTests(unittest.TestCase):
    def test_leaving_shows_remaining_seconds(self):
        text = countdown_text(
            {"state": "leaving", "present": False, "absence_seconds_remaining": 12.4}
        )
        self.assertIn("12s", text)
        self.assertIn("closing apps", text)

    def test_leaving_without_field_still_renders(self):
        self.assertEqual(countdown_text({"state": "leaving"}), "Away")

    def test_present_state(self):
        self.assertIn(
            "At desk", countdown_text({"state": "present", "present": True})
        )

    def test_cooldown_shows_remaining(self):
        text = countdown_text(
            {"state": "cooldown", "cooldown_seconds_remaining": 41.9}
        )
        self.assertIn("42s", text)

    def test_negative_remaining_is_clamped(self):
        text = countdown_text(
            {"state": "leaving", "absence_seconds_remaining": -5.0}
        )
        self.assertIn("0s", text)

    def test_non_dict_is_no_worker(self):
        self.assertEqual(countdown_text(None), NO_WORKER_TEXT)
        self.assertEqual(countdown_text("garbage"), NO_WORKER_TEXT)


class FakeSupervisor(object):
    def __init__(self, running=True, beat=None):
        self.running = running
        self.beat = beat or {"state": "present", "present": True}
        self.preview_requests = []

    def set_preview_requested(self, enabled):
        self.preview_requests.append(bool(enabled))
        return bool(enabled)

    def read_heartbeat(self):
        return self.beat

    def is_worker_running(self):
        return self.running


class FakeReader(object):
    def __init__(self, payload=None):
        self.payload = payload
        self.closed = False

    def read(self):
        return self.payload

    def close(self):
        self.closed = True


class PreviewControllerTests(unittest.TestCase):
    def test_open_requests_preview_from_worker(self):
        sup = FakeSupervisor()
        PreviewController(supervisor=sup, reader_factory=FakeReader).open()
        self.assertEqual(sup.preview_requests, [True])

    def test_close_cancels_request_and_never_stops_worker(self):
        sup = FakeSupervisor()
        controller = PreviewController(supervisor=sup, reader_factory=FakeReader)
        controller.open()
        controller.close()
        self.assertEqual(sup.preview_requests, [True, False])
        self.assertTrue(sup.running, "closing preview must not stop the worker")

    def test_tick_returns_frame_and_text(self):
        payload = (7, True, (1, 2, 3, 4), b"xyz")
        sup = FakeSupervisor(
            beat={"state": "leaving", "absence_seconds_remaining": 9.0}
        )
        controller = PreviewController(
            supervisor=sup, reader_factory=lambda: FakeReader(payload)
        )
        data, box, text = controller.tick()
        self.assertEqual(data, b"xyz")
        self.assertEqual(box, (1, 2, 3, 4))
        self.assertIn("9s", text)
        self.assertEqual(controller.last_seq, 7)

    def test_tick_without_worker_reports_off(self):
        sup = FakeSupervisor(running=False)
        controller = PreviewController(
            supervisor=sup, reader_factory=lambda: FakeReader(None)
        )
        data, _box, text = controller.tick()
        self.assertIsNone(data)
        self.assertEqual(text, NO_WORKER_TEXT)

    def test_tick_before_first_frame_waits(self):
        sup = FakeSupervisor()
        controller = PreviewController(
            supervisor=sup, reader_factory=lambda: FakeReader(None)
        )
        data, _box, text = controller.tick()
        self.assertIsNone(data)
        self.assertEqual(text, WAITING_TEXT)

    def test_missing_segment_does_not_raise(self):
        def boom():
            raise pb.PreviewUnavailable("no segment")

        controller = PreviewController(
            supervisor=FakeSupervisor(), reader_factory=boom
        )
        data, _box, text = controller.tick()
        self.assertIsNone(data)
        self.assertEqual(text, WAITING_TEXT)


if __name__ == "__main__":
    unittest.main()
