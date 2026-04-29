from __future__ import annotations

from typing import Dict

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy, QFrame, QButtonGroup
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    RadioButton,
    SingleDirectionScrollArea,
    TitleLabel,
)

from app.services.theme_manager import ThemeManager


class ThemeOptionCard(CardWidget):
    def __init__(self, name: str, color: QColor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._color = color

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        swatch = QFrame(self)
        swatch.setFixedSize(18, 18)
        swatch.setStyleSheet(
            f"background-color: {self._color.name()}; border-radius: 9px;"
        )

        text_container = QWidget(self)
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.addWidget(BodyLabel(name, text_container))
        desc = CaptionLabel(f"主色 {self._color.name().upper()} · 背景沿用当前", text_container)
        desc.setStyleSheet("color: #6B6B6B;")
        text_layout.addWidget(desc)

        self.radio = RadioButton("启用", self)

        layout.addWidget(swatch)
        layout.addWidget(text_container)
        layout.addStretch(1)
        layout.addWidget(self.radio, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.radio.setChecked(True)
        super().mousePressEvent(event)

    @property
    def name(self) -> str:
        return self._name


class ThemePage(QWidget):
    def __init__(self, manager: ThemeManager | None = None) -> None:
        super().__init__()
        self.setObjectName("theme_page")
        self._manager = manager or ThemeManager()
        self._cards: Dict[str, ThemeOptionCard] = {}
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        self._build_ui()
        self._apply_current()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll_area = SingleDirectionScrollArea(self, Qt.Orientation.Vertical)
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.enableTransparentBackground()
        scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.setSpacing(8)

        scroll_area.setWidget(content)
        root.addWidget(scroll_area)

        header = TitleLabel("主题配色", content)
        content_layout.addWidget(header)
        header_desc = BodyLabel("切换按钮、滑动条、指示线与装饰颜色的主色调。", content)
        content_layout.addWidget(header_desc)
        content_layout.addSpacing(4)

        for name, color in self._manager.preset_items():
            card = ThemeOptionCard(name, color, content)
            card.radio.toggled.connect(lambda checked, n=name: self._on_selected(n, checked))
            self._button_group.addButton(card.radio)
            self._cards[name] = card
            content_layout.addWidget(card)

        content_layout.addStretch(1)

    def _apply_current(self) -> None:
        current = self._manager.current_name()
        for name, card in self._cards.items():
            card.radio.blockSignals(True)
            card.radio.setChecked(name == current)
            card.radio.blockSignals(False)

    def _on_selected(self, name: str, checked: bool) -> None:
        if not checked:
            return
        self._manager.apply_preset(name)
