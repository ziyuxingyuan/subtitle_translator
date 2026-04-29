from __future__ import annotations

import os

from PyQt6.QtCore import QDir


def to_native_path(path: str) -> str:
    text = (path or "").strip()
    if not text:
        return ""
    try:
        return QDir.toNativeSeparators(text)
    except Exception:
        return os.path.normpath(text)
