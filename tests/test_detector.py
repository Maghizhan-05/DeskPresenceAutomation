"""Vision selection, confidence gate and normalized face-box translation."""
import unittest
from unittest.mock import patch

import numpy as np

from presence_automation import config, detector


class Observation:
    def __init__(self, confidence, box):
        self._confidence = confidence
        self._box = box

    def confidence(self):
        return self._confidence

    def boundingBox(self):
        return self._box


class VisionResultTests(unittest.TestCase):
    def test_only_confident_face_is_present(self):
        high = Observation(.91, ((.2, .3), (.25, .4)))
        low = Observation(.4, ((.7, .7), (.1, .1)))
        self.assertEqual(detector.vision_faces([low, high], .7, 640, 480),
                         [(128, 144, 160, 192, .91)])

    def test_missing_malformed_and_nonfinite_confidence_are_not_faces(self):
        for value in (None, (), [object()], [Observation(float('nan'), ((0, 0), (.3, .3)))],
                      [Observation(float('inf'), ((0, 0), (.3, .3)))],
                      [Observation(.9, ((0, 0), (0, .3)))],
                      [Observation(.9, ((2, 2), (.2, .2)))],
                      [Observation(.9, 'invalid')]):
            self.assertEqual(detector.vision_faces(value, .7, 640, 480), [], repr(value))

    def test_orientation_and_mirroring_are_explicit(self):
        # Vision sees the same unmirrored, upright pixel array as OpenCV.
        # Vision lower-left (x=.125,y=.25,w=.25,h=.5) maps to OpenCV top-left.
        o = Observation(.9, ((.125, .25), (.25, .5)))
        self.assertEqual(detector.vision_faces([o], .7, 640, 480),
                         [(80, 120, 160, 240, .9)])
        # A horizontal mirror would incorrectly move the box to x=400.
        self.assertNotEqual(detector.vision_faces([o], .7, 640, 480)[0][0], 400)

    def test_backend_is_selected_at_construction_and_bad_backend_fails_closed(self):
        with patch.object(config, 'DETECTOR_BACKEND', 'vision'):
            d = detector.Detector()
            self.assertEqual(d.backend, 'vision')
            self.assertIsNone(d._cascade)
        with patch.object(config, 'DETECTOR_BACKEND', 'haar'):
            d = detector.Detector()
            self.assertEqual(d.backend, 'haar')
        with patch.object(config, 'DETECTOR_BACKEND', 'other'):
            with self.assertRaises(detector.DetectorUnavailable):
                detector.Detector()

    def test_vision_bridge_accepts_in_memory_raw_pixels(self):
        frame = np.zeros((128, 128, 3), dtype=np.uint8)
        self.assertEqual(detector.Detector()._vision_observations(frame), [])

    def test_native_vision_exception_is_reported_as_detector_unavailable(self):
        import Vision
        class BrokenHandler:
            @staticmethod
            def alloc():
                raise ValueError('native framework error')
        frame = np.zeros((128, 128, 3), dtype=np.uint8)
        with patch.object(Vision, 'VNImageRequestHandler', BrokenHandler):
            with self.assertRaises(detector.DetectorUnavailable):
                detector.Detector()._vision_observations(frame)

    def test_sample_exposes_only_current_numeric_confidences_for_self_test(self):
        class Capture:
            def read(self):
                return True, np.zeros((100, 100, 3), dtype=np.uint8)
        d = detector.Detector()
        d._capture = Capture()
        with patch.object(d, '_vision_observations', side_effect=[
            [Observation(.91, ((.1, .1), (.2, .2)))], [],
        ]):
            self.assertTrue(d.sample())
            self.assertEqual(d.last_confidences, (.91,))
            self.assertFalse(d.sample())
            self.assertEqual(d.last_confidences, ())

    def test_malformed_vision_result_does_not_crash_or_report_presence(self):
        class Capture:
            def read(self):
                return True, np.zeros((100, 100, 3), dtype=np.uint8)
        d = detector.Detector()
        d._capture = Capture()
        with patch.object(d, '_vision_observations', return_value=[object(), Observation(float('nan'), ((0, 0), (.5, .5)))]):
            self.assertFalse(d.sample())
            self.assertEqual(d.last_confidences, ())

    def test_self_test_reports_confidence_for_each_sample(self):
        from presence_automation import main
        class FakeDetector:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def sample(self):
                self.last_confidences = (.82,)
                return True
        with patch.object(detector, 'Detector', FakeDetector), \
             patch.object(main.app_manager, 'running_bundle_ids', return_value=set()), \
             patch.object(main.app_manager, 'tracked_bundle_ids', return_value=[]), \
             self.assertLogs('deskpresence', level='INFO') as messages:
            self.assertEqual(main._self_test(), 0)
        self.assertEqual(sum('confidence=' in msg for msg in messages.output), 3)

    def test_worker_heartbeat_reports_selected_backend(self):
        from presence_automation import main
        class FakeDetector:
            backend = 'vision'
            def sample(self, **_kwargs):
                return False
        beats = []
        with patch.object(main, '_RUNNING', True), \
             patch.object(main.worker_supervisor, 'preview_requested', return_value=False), \
             patch.object(main.worker_supervisor, 'write_heartbeat', side_effect=lambda *a, **k: beats.append(k)), \
             patch.object(main.worker_supervisor, 'clear_heartbeat'), \
             patch.object(main.time, 'sleep'):
            self.assertEqual(main.run(interval=0, max_ticks=1, detector=FakeDetector()), 0)
        self.assertEqual(beats[0]['extra']['detector_backend'], 'vision')

    def test_real_sample_path_gates_confidence_and_preserves_preview_box(self):
        class Capture:
            def read(self):
                return True, np.zeros((480, 640, 3), dtype=np.uint8)
        class Writer:
            def __init__(self):
                self.calls = []
            def write(self, data, present, box):
                self.calls.append((present, box, len(data)))
        d = detector.Detector()
        d._capture = Capture()
        writer = Writer()
        with patch.object(d, '_vision_observations', return_value=[Observation(.9, ((.125, .25), (.25, .5)))]):
            self.assertTrue(d.sample(publish_preview=True, preview_writer=writer))
        self.assertEqual(writer.calls[0][:2], (True, (40, 60, 80, 120)))
        with patch.object(d, '_vision_observations', return_value=[Observation(.4, ((.125, .25), (.25, .5)))]):
            self.assertFalse(d.sample(publish_preview=True, preview_writer=writer))
        self.assertEqual(writer.calls[1][:2], (False, (0, 0, 0, 0)))
