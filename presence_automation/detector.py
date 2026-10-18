"""Webcam presence detection.

Privacy contract: a frame is grabbed, passed to the detector in memory, and the
reference is dropped immediately. Nothing is written to disk, encoded, or sent
anywhere. `sample()` returns presence-for-this-frame only -- no frame history is
retained, debouncing is the state machine's job.
"""

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


class Detector:
    def __init__(self, camera_index=config.CAMERA_INDEX):
        self.camera_index = camera_index
        self._capture = None
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
    def sample(self):
        """Grab one frame and return True iff a face is visible in it."""
        if self._capture is None:
            self.open()
        capture = self._capture
        if capture is None:
            raise DetectorUnavailable("camera not open")
        ok, frame = capture.read()
        if not ok or frame is None:
            return False
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)  # helps in low light
            faces = self._cascade.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(60, 60),
            )
            return len(faces) > 0
        finally:
            # Drop every pixel reference before returning. No persistence.
            del frame
            gray = None
