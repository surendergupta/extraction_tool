"""OpenCV-based image cleanup applied before Tesseract OCR.

Grayscale -> Otsu binarization -> deskew. Each step is cheap on CPU and
together they're the single biggest accuracy lever for Tesseract on
scanned/photographed English documents (per Tesseract's own docs).
"""

import cv2
import numpy as np


def preprocess_image(img_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    _, binarized = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return _deskew(binarized)


def _deskew(img: np.ndarray, max_correction_deg: float = 15.0) -> np.ndarray:
    """Rotate `img` to correct small skew, estimated from the minimum-area
    bounding rectangle of the dark (text) pixels.
    """
    coords = np.column_stack(np.where(img < 255))
    if coords.shape[0] < 50:  # not enough ink to estimate an angle reliably
        return img

    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    if abs(angle) < 0.1 or abs(angle) > max_correction_deg:
        # No meaningful skew, or the estimate is unreliable - leave as-is
        # rather than risk rotating a page that wasn't actually skewed.
        return img

    h, w = img.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(
        img, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )
