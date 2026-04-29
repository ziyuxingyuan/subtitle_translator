from __future__ import annotations

from typing import Any, Dict

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    LineEdit,
    SingleDirectionScrollArea,
    SwitchButton,
    TitleLabel,
)

from app.data.config_store import ConfigStore
from app.services.logging_setup import get_logger, set_debug_mode


class SettingsPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("app_settings_page")
        self._store = ConfigStore()
        self._logger = get_logger("app")
        self._settings: Dict[str, Any] = self._store.load_user_settings() or {}
        self._loading = True

        self._build_ui()
        self._apply_settings()
        self._loading = False

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

        header = TitleLabel("应用设置", content)
        content_layout.addWidget(header)
        header_desc = BodyLabel("配置代理与调试相关的基础选项。", content)
        content_layout.addWidget(header_desc)
        content_layout.addSpacing(4)

        self.proxy_enabled = SwitchButton(content)
        self.proxy_enabled.setOnText("启用")
        self.proxy_enabled.setOffText("停用")
        self.proxy_enabled.checkedChanged.connect(self._save_settings)

        self.proxy_address = LineEdit(content)
        self.proxy_address.setPlaceholderText("例如 http://127.0.0.1:7890")
        self.proxy_address.setMinimumWidth(260)
        self.proxy_address.editingFinished.connect(self._save_settings)

        self.debug_mode = SwitchButton(content)
        self.debug_mode.setOnText("启用")
        self.debug_mode.setOffText("停用")
        self.debug_mode.checkedChanged.connect(self._save_settings)

        content_layout.addWidget(
            self._build_card(
                "代理",
                "启用代理以转发接口请求。",
                self.proxy_enabled,
            )
        )
        content_layout.addWidget(
            self._build_card(
                "代理地址",
                "代理服务器地址（HTTP/SOCKS）。",
                self.proxy_address,
            )
        )
        content_layout.addWidget(
            self._build_card(
                "调试模式",
                "记录更详细的诊断与调试日志。",
                self.debug_mode,
            )
        )

        content_layout.addStretch(1)

    @staticmethod
    def _build_card(title: str, desc: str, control: QWidget) -> CardWidget:
        card = CardWidget()
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        text_container = QWidget(card)
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.addWidget(BodyLabel(title, text_container))
        tip = CaptionLabel(desc, text_container)
        tip.setStyleSheet("color: #6B6B6B;")
        text_layout.addWidget(tip)
        text_container.setFixedWidth(240)

        layout.addWidget(text_container)
        layout.addStretch(1)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return card

    def _apply_settings(self) -> None:
        self.proxy_enabled.blockSignals(True)
        self.proxy_address.blockSignals(True)
        self.debug_mode.blockSignals(True)
        try:
            self.proxy_enabled.setChecked(bool(self._settings.get("proxy_enabled", False)))
            self.proxy_address.setText(self._settings.get("proxy_address", ""))
            self.debug_mode.setChecked(int(self._settings.get("debug_mode", 0)) > 0)
        finally:
            self.proxy_enabled.blockSignals(False)
            self.proxy_address.blockSignals(False)
            self.debug_mode.blockSignals(False)
        set_debug_mode(self.debug_mode.isChecked())

    def _save_settings(self) -> None:
        if self._loading:
            return
        self._settings["proxy_enabled"] = self.proxy_enabled.isChecked()
        self._settings["proxy_address"] = self.proxy_address.text().strip()
        self._settings["debug_mode"] = 1 if self.debug_mode.isChecked() else 0
        self._settings.pop("endpoint", None)

        self._store.save_user_settings(self._settings)
        self._logger.info("Settings updated")
        set_debug_mode(self.debug_mode.isChecked())
