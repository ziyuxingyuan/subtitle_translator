from __future__ import annotations

from typing import Dict, Iterable, Tuple

from PyQt6.QtGui import QColor
from qfluentwidgets.common.style_sheet import setThemeColor

from app.data.config_store import ConfigStore


class ThemeManager:
    PRESETS: Dict[str, str] = {
        "原始绿": "#009FAA",
        "斗鱼橙": "#FF691E",
        "B站粉": "#FF8CB0",
    }
    KEY = "theme_preset"

    def __init__(self, store: ConfigStore | None = None) -> None:
        self._store = store or ConfigStore()
        self._settings = self._store.load_user_settings() or {}

    def preset_items(self) -> Iterable[Tuple[str, QColor]]:
        return [(name, QColor(color)) for name, color in self.PRESETS.items()]

    def current_name(self) -> str:
        name = self._settings.get(self.KEY, "原始绿")
        if name not in self.PRESETS:
            return "原始绿"
        return name

    def apply_preset(self, name: str) -> QColor:
        if name not in self.PRESETS:
            name = "原始绿"
        color = QColor(self.PRESETS[name])
        setThemeColor(color, save=False)
        self._settings[self.KEY] = name
        self._store.save_user_settings(self._settings)
        return color

    def apply_saved(self) -> QColor:
        return self.apply_preset(self.current_name())
