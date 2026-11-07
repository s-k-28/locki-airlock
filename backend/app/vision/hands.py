"""MediaPipe Tasks wrappers (hand landmarks, face detection).

MediaPipe 1.x removed the legacy ``mp.solutions`` API, so this uses the Tasks API
with model files downloaded by ``scripts/download_models.py``. VIDEO running mode
lets MediaPipe track hands between frames instead of re-detecting every frame.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from .focus_observation import FaceObs, yaw_ratio_from_keypoints
from .reach import HandObs

os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

HAND_MODEL = "hand_landmarker.task"
FACE_MODEL = "blaze_face_short_range.tflite"

# MediaPipe hand topology, for drawing.
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
]


class ModelMissing(RuntimeError):
    pass


def _require(models_dir: Path, name: str) -> Path:
    path = models_dir / name
    if not path.exists():
        raise ModelMissing(f"{name} not found in {models_dir}; run `uv run python scripts/download_models.py` while online")
    return path


class HandTracker:
    def __init__(
        self,
        models_dir: Path,
        num_hands: int = 2,
        min_detection: float = 0.5,
        min_presence: float = 0.5,
        min_tracking: float = 0.5,
        delegate: str = "cpu",  # "gpu" is only for profiling experiments (scripts/profile_vision.py)
    ):
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        self._mp = mp
        self._gpu = delegate == "gpu"
        d = mp_python.BaseOptions.Delegate.GPU if self._gpu else mp_python.BaseOptions.Delegate.CPU
        options = mp_vision.HandLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(_require(models_dir, HAND_MODEL)), delegate=d),
            running_mode=mp_vision.RunningMode.VIDEO,
            num_hands=num_hands,
            min_hand_detection_confidence=min_detection,
            min_hand_presence_confidence=min_presence,
            min_tracking_confidence=min_tracking,
        )
        self._landmarker = mp_vision.HandLandmarker.create_from_options(options)

    def detect(self, rgb: np.ndarray, timestamp_ms: int) -> list[HandObs]:
        if self._gpu:  # the Metal path only takes 4-channel images
            rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
            image = self._mp.Image(image_format=self._mp.ImageFormat.SRGBA, data=rgba)
        else:
            image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(image, timestamp_ms)
        hands: list[HandObs] = []
        for i, lms in enumerate(result.hand_landmarks):
            cat = result.handedness[i][0] if i < len(result.handedness) and result.handedness[i] else None
            hands.append(
                HandObs(
                    landmarks=[(lm.x, lm.y) for lm in lms],
                    score=float(cat.score) if cat is not None else 0.0,
                    handedness=cat.category_name if cat is not None else "",
                )
            )
        return hands

    def close(self) -> None:
        self._landmarker.close()


class FaceObserver:
    def __init__(self, models_dir: Path, min_detection: float = 0.5):
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        self._mp = mp
        options = mp_vision.FaceDetectorOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(_require(models_dir, FACE_MODEL))),
            running_mode=mp_vision.RunningMode.VIDEO,
            min_detection_confidence=min_detection,
        )
        self._detector = mp_vision.FaceDetector.create_from_options(options)

    def observe(self, rgb: np.ndarray, timestamp_ms: int) -> FaceObs:
        h, w = rgb.shape[:2]
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._detector.detect_for_video(image, timestamp_ms)
        if not result.detections:
            return FaceObs(present=False)
        det = max(result.detections, key=lambda d: d.bounding_box.width * d.bounding_box.height)
        bb = det.bounding_box
        score = float(det.categories[0].score) if det.categories else 0.0
        kps = [(k.x, k.y) for k in (det.keypoints or [])]
        return FaceObs(
            present=True,
            score=score,
            yaw_ratio=yaw_ratio_from_keypoints(kps),
            box=(bb.origin_x / w, bb.origin_y / h, bb.width / w, bb.height / h),
        )

    def close(self) -> None:
        self._detector.close()
