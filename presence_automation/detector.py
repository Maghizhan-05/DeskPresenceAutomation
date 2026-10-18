"""Webcam presence detection.

Privacy contract: a frame is grabbed, passed to the detector in memory, and the
reference is dropped immediately. Nothing is written to disk, encoded, or sent
anywhere. `sample()` returns presence-for-this-frame only -- no frame history is
retained, debouncing is the state machine's job.
"""

import math
import os

import cv2

from . import config


class DetectorUnavailable(RuntimeError):
    """Camera could not be opened (in use, missing, or permission denied)."""


def _haar_cascade_path():
    path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
    if not os.path.exists(path):
        raise DetectorUnavailable("Haar cascade not found at %s" % path)
    return path


def vision_faces(observations, threshold, width, height):
    """Accepted faces as top-left, unmirrored OpenCV pixel boxes + confidence.

    Vision normalizes against the upright image with a LOWER-left origin; the
    capture and preview use the same unmirrored pixels with a TOP-left origin.
    """
    accepted = []
    for observation in observations or ():
        try:
            score = float(observation.confidence())
            if not math.isfinite(score) or score < threshold:
                continue
            (x, y), (w, h) = observation.boundingBox()
            x, y, w, h = map(float, (x, y, w, h))
            if not all(map(math.isfinite, (x, y, w, h))):
                continue
            if w <= 0 or h <= 0 or x >= 1 or y >= 1 or x + w <= 0 or y + h <= 0:
                continue
            left = max(0, min(width, round(x * width)))
            right = max(0, min(width, round((x + w) * width)))
            top = max(0, min(height, round((1 - y - h) * height)))
            bottom = max(0, min(height, round((1 - y) * height)))
            if right > left and bottom > top:
                accepted.append((left, top, right - left, bottom - top, score))
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
    return accepted


class Detector:
    def __init__(self, camera_index=None):
        # Resolved at construction time, not at def time, so a config change
        # (or a test override) is honoured instead of frozen at import.
        if camera_index is None:
            camera_index = config.CAMERA_INDEX
        self.camera_index = camera_index
        self.backend = config.DETECTOR_BACKEND
        if self.backend not in ("vision", "haar"):
            raise DetectorUnavailable("unknown detector backend: %s" % self.backend)
        self._capture = None
        self.last_confidences = ()  # scores only, overwritten each tick (no frame history)
        self._cascade = None
        if self.backend == "haar":
            self._cascade = cv2.CascadeClassifier(_haar_cascade_path())
            if self._cascade.empty():
                raise DetectorUnavailable("Haar cascade failed to load")

    # -- lifecycle ------------------------------------------------------------
    def open(self):
        if self._capture is not None and self._capture.isOpened():
            return
        capture = cv2.VideoCapture(self.camera_index)
        if not capture.isOpened():
            capture.release()
            raise DetectorUnavailable(
                "cannot open camera index %s (check Privacy & Security > Camera)"
                % self.camera_index
            )
        self._capture = capture

    def close(self):
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_exc):
        self.close()

    # -- the single sample ----------------------------------------------------
    def sample(self, publish_preview=False, preview_writer=None):
        """Grab one frame and return True iff a face is visible in it.

        `publish_preview` is opt-in and OFF by default. When enabled, the
        already-in-memory frame is downscaled and copied into the caller's
        fixed-size shared-memory slot AFTER inference, so the detection result
        can never depend on whether preview is on. Nothing is written to disk.
        """
        if self._capture is None:
            self.open()
        capture = self._capture
        if capture is None:
            raise DetectorUnavailable("camera not open")
        ok, frame = capture.read()
        self.last_confidences = ()
        if not ok or frame is None:
            return False
        gray = None
        try:
            if self.backend == "vision":
                observations = self._vision_observations(frame)
                scores = []
                for observation in observations:
                    try:
                        score = float(observation.confidence())
                        if math.isfinite(score):
                            scores.append(score)
                    except (AttributeError, ValueError, TypeError, OverflowError):
                        continue
                self.last_confidences = tuple(scores)
                height, width = frame.shape[:2]
                accepted = vision_faces(
                    observations, config.FACE_CONFIDENCE_THRESHOLD, width, height
                )
                faces = [face[:4] for face in accepted]
            else:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.equalizeHist(gray)
                faces = self._cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60),
                )
            present = len(faces) > 0
            # Inference is complete and `present` is already decided; nothing
            # below can change it.
            if publish_preview and preview_writer is not None:
                self._publish(frame, faces, present, preview_writer)
            return present
        finally:
            # Drop every pixel reference before returning. No persistence.
            del frame
            gray = None

    def _vision_observations(self, frame):
        """Detect in the exact upright, unmirrored pixel array sent to preview."""
        try:
            import Vision
            import Quartz
            from Foundation import NSData
        except ImportError as exc:
            raise DetectorUnavailable("Apple Vision unavailable: %s" % exc)
        rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
        height, width = rgba.shape[:2]
        pixels = NSData.dataWithBytes_length_(rgba.tobytes(), rgba.nbytes)
        provider = Quartz.CGDataProviderCreateWithCFData(pixels)
        image = Quartz.CGImageCreate(
            width, height, 8, 32, width * 4,
            Quartz.CGColorSpaceCreateDeviceRGB(),
            Quartz.kCGImageAlphaPremultipliedLast | Quartz.kCGBitmapByteOrder32Big,
            provider, None, False, Quartz.kCGRenderingIntentDefault,
        )
        if image is None:
            raise DetectorUnavailable("Vision could not construct in-memory image")
        try:
            request = Vision.VNDetectFaceRectanglesRequest.alloc().init()
            handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_orientation_options_(
                image, Quartz.kCGImagePropertyOrientationUp, None,
            )
            success, error = handler.performRequests_error_([request], None)
            if not success:
                raise DetectorUnavailable("Vision face detection failed: %s" % error)
            return request.results() or []
        except DetectorUnavailable:
            raise
        except Exception as exc:
            raise DetectorUnavailable("Vision face detection failed: %s" % exc) from exc

    @staticmethod
    def _publish(frame, faces, present, writer):
        """Copy a downscaled frame into the fixed shared slot. Best effort."""
        try:
            height, width = frame.shape[:2]
            small = cv2.resize(
                frame,
                (config.PREVIEW_WIDTH, config.PREVIEW_HEIGHT),
                interpolation=cv2.INTER_AREA,
            )
            box = (0, 0, 0, 0)
            if len(faces) > 0:
                fx, fy, fw, fh = faces[0]
                sx = float(config.PREVIEW_WIDTH) / float(width)
                sy = float(config.PREVIEW_HEIGHT) / float(height)
                box = (int(fx * sx), int(fy * sy), int(fw * sx), int(fh * sy))
            writer.write(small.tobytes(), present, box)
        except Exception:
            # A preview failure must never break detection or the loop.
            pass
