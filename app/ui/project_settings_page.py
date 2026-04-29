from __future__ import annotations

from typing import Any, Dict, List, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    SingleDirectionScrollArea,
    SpinBox,
    SwitchButton,
    TitleLabel,
)

from app.data.config_store import ConfigStore


class ProjectSettingsPage(QWidget):
    segmentation_feature_changed = pyqtSignal(bool)
    LANGUAGE_OPTIONS: List[Tuple[str, str]] = [
        ("auto", "自动检测"),
        ("zh-CN", "中文（简体）"),
        ("zh-TW", "中文（繁体）"),
        ("en", "英语"),
        ("ja", "日语"),
        ("ko", "韩语"),
        ("fr", "法语"),
        ("de", "德语"),
        ("es", "西班牙语"),
        ("ru", "俄语"),
        ("ar", "阿拉伯语"),
        ("pt", "葡萄牙语"),
        ("it", "意大利语"),
        ("th", "泰语"),
        ("vi", "越南语"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("project_settings_page")
        self._store = ConfigStore()
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

        header = TitleLabel("项目设置", content)
        content_layout.addWidget(header)
        header_desc = BodyLabel("在这里配置翻译任务的基础参数。", content)
        content_layout.addWidget(header_desc)
        content_layout.addSpacing(4)

        self.source_combo = ComboBox(content)
        self._fill_language_options(self.source_combo)
        self.source_combo.setMinimumWidth(220)
        self.source_combo.currentIndexChanged.connect(self._save_settings)

        self.target_combo = ComboBox(content)
        self._fill_language_options(self.target_combo)
        self.target_combo.setMinimumWidth(220)
        self.target_combo.currentIndexChanged.connect(self._save_settings)

        self.retry_spin = SpinBox(content)
        self.retry_spin.setRange(0, 10)
        self.retry_spin.setSingleStep(1)
        self.retry_spin.setMinimumWidth(180)
        self.retry_spin.valueChanged.connect(self._save_settings)

        self.batch_retry_spin = SpinBox(content)
        self.batch_retry_spin.setRange(0, 10)
        self.batch_retry_spin.setSingleStep(1)
        self.batch_retry_spin.setMinimumWidth(180)
        self.batch_retry_spin.valueChanged.connect(self._save_settings)

        self.timeout_spin = SpinBox(content)
        self.timeout_spin.setRange(10, 600)
        self.timeout_spin.setSingleStep(10)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setMinimumWidth(180)
        self.timeout_spin.valueChanged.connect(self._save_settings)

        self.batch_timeout_spin = SpinBox(content)
        self.batch_timeout_spin.setRange(0, 3600)
        self.batch_timeout_spin.setSingleStep(30)
        self.batch_timeout_spin.setSuffix(" 秒")
        self.batch_timeout_spin.setMinimumWidth(180)
        self.batch_timeout_spin.valueChanged.connect(self._save_settings)

        self.segmentation_enabled = SwitchButton(content)
        self.segmentation_enabled.setOnText("启用")
        self.segmentation_enabled.setOffText("停用")
        self.segmentation_enabled.checkedChanged.connect(self._save_settings)

        content_layout.addWidget(
            self._build_card(
                "源语言",
                "原始字幕文本的语言，用于生成更准确的翻译提示。",
                self.source_combo,
            )
        )
        content_layout.addWidget(
            self._build_card(
                "目标语言",
                "翻译结果输出的语言。",
                self.target_combo,
            )
        )
        content_layout.addWidget(
            self._build_card(
                "重试次数",
                "接口请求失败后自动重试的次数。",
                self.retry_spin,
            )
        )
        content_layout.addWidget(
            self._build_card(
                "批次重试",
                "批次翻译失败后自动重试的次数（与接口重试独立）。",
                self.batch_retry_spin,
            )
        )
        content_layout.addWidget(
            self._build_card(
                "超时时间",
                "单次请求硬超时上限，达到后会强制终止当前请求并按重试策略处理。",
                self.timeout_spin,
            )
        )
        content_layout.addWidget(
            self._build_card(
                "批次硬超时",
                "单个批次总时长上限（含接口重试与批次重试），0 表示关闭。",
                self.batch_timeout_spin,
            )
        )
        content_layout.addWidget(
            self._build_card(
                "语义分段",
                "启用任务与自动化中的语义分段功能（关闭将隐藏相关配置）。",
                self.segmentation_enabled,
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

    def _fill_language_options(self, combo: ComboBox) -> None:
        combo.clear()
        for code, name in self.LANGUAGE_OPTIONS:
            combo.addItem(name, code)

    def _apply_settings(self) -> None:
        source_value = self._settings.get("source_language", "ja")
        target_value = self._settings.get("target_language", "zh-CN")
        self.source_combo.blockSignals(True)
        self.target_combo.blockSignals(True)
        self.retry_spin.blockSignals(True)
        self.batch_retry_spin.blockSignals(True)
        self.timeout_spin.blockSignals(True)
        self.batch_timeout_spin.blockSignals(True)
        self.segmentation_enabled.blockSignals(True)
        try:
            self._select_combo_value(self.source_combo, source_value)
            self._select_combo_value(self.target_combo, target_value)
            self.retry_spin.setValue(int(self._settings.get("max_retries", 2)))
            self.batch_retry_spin.setValue(int(self._settings.get("batch_retries", 2)))
            self.timeout_spin.setValue(int(self._settings.get("timeout", 240)))
            self.batch_timeout_spin.setValue(int(self._settings.get("batch_timeout", 0)))
            self.segmentation_enabled.setChecked(bool(self._settings.get("segmentation_enabled", False)))
        finally:
            self.source_combo.blockSignals(False)
            self.target_combo.blockSignals(False)
            self.retry_spin.blockSignals(False)
            self.batch_retry_spin.blockSignals(False)
            self.timeout_spin.blockSignals(False)
            self.batch_timeout_spin.blockSignals(False)
            self.segmentation_enabled.blockSignals(False)

    def _select_combo_value(self, combo: ComboBox, value: str) -> None:
        if value:
            for index, (code, name) in enumerate(self.LANGUAGE_OPTIONS):
                if value == code or value == name:
                    combo.setCurrentIndex(index)
                    return
        combo.setCurrentIndex(0)

    def _get_language_code(self, combo: ComboBox) -> str:
        index = combo.currentIndex()
        if 0 <= index < len(self.LANGUAGE_OPTIONS):
            return self.LANGUAGE_OPTIONS[index][0]
        return combo.currentData() or combo.currentText()

    def _save_settings(self) -> None:
        if self._loading:
            return
        self._settings = self._store.load_user_settings() or {}
        prev_enabled = bool(self._settings.get("segmentation_enabled", False))
        self._settings["source_language"] = self._get_language_code(self.source_combo)
        self._settings["target_language"] = self._get_language_code(self.target_combo)
        self._settings["max_retries"] = int(self.retry_spin.value())
        self._settings["batch_retries"] = int(self.batch_retry_spin.value())
        self._settings["timeout"] = int(self.timeout_spin.value())
        self._settings["batch_timeout"] = int(self.batch_timeout_spin.value())
        self._settings["segmentation_enabled"] = bool(self.segmentation_enabled.isChecked())
        self._store.save_user_settings(self._settings)
        if prev_enabled != self.segmentation_enabled.isChecked():
            self.segmentation_feature_changed.emit(self.segmentation_enabled.isChecked())
