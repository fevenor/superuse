from __future__ import annotations

from superuse.core.errors import ControlError, ErrorCode


def detect_active_rect(frame) -> list[int]:
    import cv2
    import numpy as np

    if frame.fmt == "bgr":
        image = np.frombuffer(frame.data, dtype=np.uint8).reshape(frame.height, frame.width, 3)
    else:
        image = cv2.imdecode(np.frombuffer(frame.data, dtype=np.uint8), cv2.IMREAD_COLOR)
    small = cv2.resize(image, (320, 180), interpolation=cv2.INTER_AREA)
    lit = small.max(axis=2) > 24
    rows = np.flatnonzero(lit.mean(axis=1) > 0.08)
    columns = np.flatnonzero(lit.mean(axis=0) > 0.08)
    if len(rows) < 54 or len(columns) < 96:
        raise ControlError(
            ErrorCode.INVALID_REQUEST, "Picture is too dark or sparse; use manual calibration"
        )
    left, right = int(columns[0]), int(columns[-1]) + 1
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    if (right - left) * (bottom - top) < 320 * 180 * 0.4:
        raise ControlError(
            ErrorCode.INVALID_REQUEST, "No reliable picture boundary; use manual calibration"
        )
    x = round(left * frame.width / 320)
    y = round(top * frame.height / 180)
    return [x, y, round(right * frame.width / 320) - x, round(bottom * frame.height / 180) - y]
