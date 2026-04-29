from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
from typing import Iterable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QListWidget,
    QFileDialog,
    QSizePolicy,
)
from qfluentwidgets import (
    CardWidget,
    TitleLabel,
    BodyLabel,
    CaptionLabel,
    StrongBodyLabel,
    PushButton,
    SwitchButton,
    ComboBox,
    LineEdit,
    SpinBox,
    DoubleSpinBox,
    CheckBox,
    Slider,
    ProgressRing,
    FluentIcon,
    IconWidget,
    SingleDirectionScrollArea,
)
from qfluentwidgets.common.config import qconfig
from qfluentwidgets.common.style_sheet import themeColor, setCustomStyleSheet

from app.data.config_store import ConfigStore
from app.services.cleanup_subtitle_worker import CleanupSubtitleWorker
from app.services.logging_setup import get_logger
from app.services.merge_subtitle_worker import MergeSubtitleWorker
from app.services.prompt_resolver import resolve_system_prompt
from app.services.segmentation_worker import SegmentationWorker
from app.services.theme_palette import build_theme_palette, color_to_hex
from app.services.translation_worker import TranslationWorker
from app.ui.message_dialog import show_warning, show_info, show_error
from app.ui.path_utils import to_native_path
from app.ui.waveform_widget import WaveformWidget
from modules.api_manager import APIManager
from modules.segmentation_engine import SegmentationConfig
from modules.translation_state_manager import TranslationStateManager


@dataclass
class AutomationJob:
    index: int
    original_path: Path
    work_dir: Path
    source_path: Path
    cleaned_json: Path | None = None
    segmented_json: Path | None = None
    segmented_srt: Path | None = None
    optimized_srt: Path | None = None
    translated_srt: Path | None = None
    post_cleaned_srt: Path | None = None


class ClickableTitleLabel(TitleLabel):
    clicked = pyqtSignal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class AutomationDropArea(QFrame):
    filesDropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None, action_widget: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("automation_drop_area")
        self.setAcceptDrops(True)
        self.setProperty("dragging", False)
        self.setMinimumHeight(140)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(6)

        title = BodyLabel("拖拽 1-2 个字幕文件到此处", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip = BodyLabel("支持 .json / .srt", self)
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(tip)
        self._action_container = QWidget(self)
        self._action_layout = QHBoxLayout(self._action_container)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(0)
        self._action_layout.addStretch(1)
        self._action_layout.addStretch(1)
        self._action_container.setVisible(False)
        layout.addWidget(self._action_container)
        layout.addStretch(1)

        self._apply_style()
        qconfig.themeColorChanged.connect(self._apply_style)
        self._action_widget: QWidget | None = None
        if action_widget is not None:
            self.set_action_widget(action_widget)

    def set_action_widget(self, widget: QWidget | None) -> None:
        if self._action_widget is widget:
            return
        if self._action_widget is not None:
            self._action_layout.removeWidget(self._action_widget)
            self._action_widget.setParent(None)
        self._action_widget = widget
        if widget is None:
            self._action_container.setVisible(False)
            return
        widget.setParent(self)
        self._action_layout.insertWidget(1, widget)
        self._action_container.setVisible(True)

    def _apply_style(self, *_: object) -> None:
        primary = themeColor()
        palette = build_theme_palette(primary)
        border_color = color_to_hex(palette.border_strong, with_alpha=True)
        drop_bg = color_to_hex(palette.drop_area_bg, with_alpha=True)
        active = QColor(primary)
        active.setAlphaF(0.08)
        self.setStyleSheet(
            f"""
            QFrame#automation_drop_area {{
                border: 2px dashed {border_color};
                border-radius: 12px;
                background-color: {drop_bg};
            }}
            QFrame#automation_drop_area[dragging="true"] {{
                border: 2px solid {primary.name()};
                background-color: {active.name(QColor.NameFormat.HexArgb)};
            }}
            """
        )

    def dragEnterEvent(self, event) -> None:
        if self._find_valid_paths(event.mimeData().urls()):
            event.acceptProposedAction()
            self._set_dragging(True)
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_dragging(False)
        event.accept()

    def dropEvent(self, event) -> None:
        self._set_dragging(False)
        paths = self._find_valid_paths(event.mimeData().urls())
        if paths:
            self.filesDropped.emit(paths)

    def _set_dragging(self, dragging: bool) -> None:
        if self.property("dragging") == dragging:
            return
        self.setProperty("dragging", dragging)
        self.style().unpolish(self)
        self.style().polish(self)

    @staticmethod
    def _find_valid_paths(urls) -> list[str]:
        valid: list[str] = []
        for url in urls:
            if not url.isLocalFile():
                continue
            path = to_native_path(url.toLocalFile())
            if not path:
                continue
            suffix = Path(path).suffix.lower()
            if suffix in (".srt", ".json") and Path(path).exists():
                valid.append(path)
        return valid


class ToolboxAutomationPage(QWidget):
    provider_changed = pyqtSignal(str)
    log_message = pyqtSignal(str)
    MERGE_STRATEGY_LABELS = {
        "pass1_primary": "Pass1 主导（无重叠）",
        "pass1_overlap": "Pass1 主导（30% 重叠容忍）",
    }

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("toolbox_automation_page")
        self._config_store = ConfigStore()
        self._api_manager = APIManager()
        self._settings = self._load_settings()
        self._logger = get_logger("app")

        self._file_paths: list[Path] = []
        self._jobs: list[AutomationJob] = []
        self._steps: list[tuple[str, callable]] = []
        self._total_steps = 0
        self._current_step = 0
        self._last_step_value = 0
        self._last_translation_count = 0
        self._translation_active = False
        self._temp_root: Path | None = None
        self._active_worker: object | None = None
        self._running = False
        self._output_dir = ""
        self._translation_expanded = False
        self._segment_expanded = False

        self._initializing = True
        self._build_ui()
        self._load_provider_models()
        self._apply_provider_settings()
        self._apply_segmentation_settings()
        self.apply_segmentation_feature_state()
        self._apply_merge_settings()
        self._initializing = False

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
        content_layout.setSpacing(12)

        scroll_area.setWidget(content)
        root.addWidget(scroll_area, 1)

        head_container = QWidget(content)
        head_layout = QHBoxLayout(head_container)
        head_layout.setContentsMargins(0, 0, 0, 0)
        head_layout.setSpacing(8)

        self.waveform = WaveformWidget(head_container)
        self.waveform.set_matrix_size(100, 20)

        waveform_container = QWidget(head_container)
        waveform_layout = QVBoxLayout(waveform_container)
        waveform_layout.setContentsMargins(0, 0, 0, 0)
        waveform_layout.addStretch(1)
        waveform_layout.addWidget(self.waveform)
        waveform_layout.addStretch(1)

        self.ring = ProgressRing(head_container)
        self.ring.setRange(0, 10000)
        self.ring.setValue(0)
        self.ring.setTextVisible(True)
        self.ring.setStrokeWidth(12)
        self.ring.setFixedSize(140, 140)
        self.ring.setFormat("就绪")

        ring_container = QWidget(head_container)
        ring_layout = QVBoxLayout(ring_container)
        ring_layout.setContentsMargins(0, 0, 0, 0)
        ring_layout.addStretch(1)
        ring_layout.addWidget(self.ring)
        ring_layout.addStretch(1)

        head_layout.addWidget(ring_container)
        head_layout.addSpacing(8)
        head_layout.addWidget(waveform_container, 1)
        content_layout.addWidget(head_container)

        input_card = CardWidget(content)
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(16, 16, 16, 16)
        input_layout.setSpacing(8)
        input_layout.addWidget(TitleLabel("脚本自动化", input_card))
        input_layout.addWidget(BodyLabel("拖入 1-2 个原始字幕，自动跑完整流程。", input_card))

        self.add_file_btn = PushButton("选择文件", input_card)
        self.add_file_btn.setFixedHeight(32)
        self.add_file_btn.clicked.connect(self._pick_files)

        self.drop_area = AutomationDropArea(input_card, action_widget=self.add_file_btn)
        self.drop_area.filesDropped.connect(self._on_files_dropped)
        input_layout.addWidget(self.drop_area)

        self.file_list = QListWidget(input_card)
        self.file_list.setMinimumHeight(0)
        self.file_list.setMaximumHeight(90)
        self.file_list.setVisible(False)
        input_layout.addWidget(self.file_list)
        self._apply_palette()
        qconfig.themeColorChanged.connect(self._apply_palette)

        hint = CaptionLabel("语义分段/对齐修段/优化字幕需要 JSON（含 words/segments）。", input_card)
        hint.setStyleSheet("color: #6B6B6B;")
        hint.setWordWrap(True)
        input_layout.addWidget(hint)
        self.segmentation_hint = hint

        output_section = QWidget(input_card)
        output_layout = QVBoxLayout(output_section)
        output_layout.setContentsMargins(0, 6, 0, 0)
        output_layout.setSpacing(6)
        output_layout.addWidget(StrongBodyLabel("输出设置", output_section))

        output_row = QHBoxLayout()
        self.output_dir_line = LineEdit(output_section)
        self.output_dir_line.setPlaceholderText("默认使用输入文件目录")
        self.output_dir_line.editingFinished.connect(self._on_output_dir_edited)
        output_row.addWidget(self.output_dir_line, 1)
        self.output_dir_btn = PushButton("选择目录", output_section)
        self.output_dir_btn.clicked.connect(self._pick_output_dir)
        output_row.addWidget(self.output_dir_btn)
        output_layout.addLayout(output_row)

        self.keep_intermediate_switch = SwitchButton(output_section)
        self.keep_intermediate_switch.setOnText("保留")
        self.keep_intermediate_switch.setOffText("清理")
        self.keep_intermediate_switch.setChecked(bool(self._settings.get("automation_keep_intermediate", False)))
        self.keep_intermediate_switch.checkedChanged.connect(self._on_keep_intermediate_changed)
        self._add_option_row(
            output_layout,
            "过程保留",
            "保留清理/分段/对齐等中间文件。",
            self.keep_intermediate_switch,
            add_divider=True,
        )
        self.resume_pipeline_switch = SwitchButton(output_section)
        self.resume_pipeline_switch.setOnText("启用")
        self.resume_pipeline_switch.setOffText("停用")
        self.resume_pipeline_switch.setChecked(bool(self._settings.get("automation_resume_pipeline", False)))
        self.resume_pipeline_switch.checkedChanged.connect(self._on_resume_pipeline_changed)
        self._add_option_row(
            output_layout,
            "失败后续跑",
            "复用已完成步骤的中间文件。",
            self.resume_pipeline_switch,
            add_divider=False,
        )
        input_layout.addWidget(output_section)

        content_layout.addWidget(input_card)

        main_row = QWidget(content)
        main_layout = QHBoxLayout(main_row)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)

        left_col = QWidget(main_row)
        left_col.setFixedWidth(360)
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)

        workflow_card = CardWidget(left_col)
        workflow_layout = QVBoxLayout(workflow_card)
        workflow_layout.setContentsMargins(16, 16, 16, 16)
        workflow_layout.setSpacing(7)
        workflow_layout.addWidget(TitleLabel("流程开关", workflow_card))

        self.clean_switch = SwitchButton(workflow_card)
        self.clean_switch.setOnText("启用")
        self.clean_switch.setOffText("停用")
        self.clean_switch.setChecked(bool(self._settings.get("automation_cleanup", True)))
        self.clean_switch.checkedChanged.connect(self._on_clean_changed)
        workflow_row_margin = 8
        self._add_option_row(
            workflow_layout,
            "文本净化",
            "去除噪音与无效文本。",
            self.clean_switch,
            row_v_margin=workflow_row_margin,
        )

        self.segment_switch = SwitchButton(workflow_card)
        self.segment_switch.setOnText("启用")
        self.segment_switch.setOffText("停用")
        self.segment_switch.setChecked(True)
        self.segment_switch_row, self.segment_switch_divider = self._add_option_row(
            workflow_layout,
            "语义分段",
            "先在日文阶段生成更自然的分段和标点。",
            self.segment_switch,
            row_v_margin=workflow_row_margin,
        )

        self.optimize_switch = SwitchButton(workflow_card)
        self.optimize_switch.setOnText("启用")
        self.optimize_switch.setOffText("停用")
        self.optimize_switch.setChecked(bool(self._settings.get("automation_optimize", False)))
        self.optimize_switch.checkedChanged.connect(self._on_optimize_changed)
        self._enforce_cleanup_optimize_mutex(prefer="optimize")
        self._add_option_row(
            workflow_layout,
            "优化字幕",
            "在当前日文中间产物上做去呻吟/词级拆短。",
            self.optimize_switch,
            row_v_margin=workflow_row_margin,
        )

        self.translate_switch = SwitchButton(workflow_card)
        self.translate_switch.setOnText("启用")
        self.translate_switch.setOffText("停用")
        self.translate_switch.setChecked(True)
        self.translate_switch.checkedChanged.connect(lambda _: self._update_merge_switch_state())
        self._add_option_row(
            workflow_layout,
            "自动翻译",
            "生成目标语言字幕。",
            self.translate_switch,
            row_v_margin=workflow_row_margin,
        )

        self.post_clean_switch = SwitchButton(workflow_card)
        self.post_clean_switch.setOnText("启用")
        self.post_clean_switch.setOffText("停用")
        self.post_clean_switch.setChecked(bool(self._settings.get("automation_post_cleanup", False)))
        self.post_clean_switch.checkedChanged.connect(self._on_post_cleanup_changed)
        self._add_option_row(
            workflow_layout,
            "译后清理",
            "翻译后删除单行语气词/呻吟词。",
            self.post_clean_switch,
            row_v_margin=workflow_row_margin,
        )

        self.merge_switch = SwitchButton(workflow_card)
        self.merge_switch.setOnText("启用")
        self.merge_switch.setOffText("停用")
        self.merge_switch.setChecked(True)
        self.merge_switch.checkedChanged.connect(lambda _: self._update_merge_switch_state())
        self._merge_switch_row, self._merge_switch_divider = self._add_option_row(
            workflow_layout,
            "合并字幕",
            "仅双文件时执行合并。",
            self.merge_switch,
            add_divider=True,
            row_v_margin=workflow_row_margin,
        )

        self.merge_strategy_combo = ComboBox(workflow_card)
        self.merge_strategy_combo.addItems(list(self.MERGE_STRATEGY_LABELS.values()))
        self.merge_strategy_combo.setMinimumWidth(240)
        self.merge_strategy_combo.currentTextChanged.connect(self._on_merge_strategy_changed)
        self._merge_strategy_row, self._merge_strategy_divider = self._add_option_row(
            workflow_layout,
            "合并策略",
            "选择合并规则。",
            self.merge_strategy_combo,
            add_divider=False,
            row_v_margin=workflow_row_margin,
        )

        left_layout.addWidget(workflow_card)

        left_layout.addStretch(1)

        right_col = QWidget(main_row)
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)

        translation_card = CardWidget(right_col)
        translation_layout = QVBoxLayout(translation_card)
        translation_layout.setContentsMargins(16, 16, 16, 16)
        translation_layout.setSpacing(8)
        self.translation_title = ClickableTitleLabel("开始翻译", translation_card)
        self.translation_title.clicked.connect(self._toggle_translation_options)
        translation_layout.addWidget(self.translation_title)

        translation_body = QWidget(translation_card)
        translation_body_layout = QVBoxLayout(translation_body)
        translation_body_layout.setContentsMargins(0, 0, 0, 0)
        translation_body_layout.setSpacing(0)

        self._translation_rows: list[QWidget] = []
        self._translation_dividers: list[QFrame | None] = []

        def add_translation_row(
            icon: FluentIcon,
            title_text: str,
            desc_text: str,
            controls: QWidget,
            add_divider: bool = True,
        ) -> None:
            row_container = QWidget(translation_body)
            row_layout = QHBoxLayout(row_container)
            row_layout.setContentsMargins(0, 10, 0, 10)
            row_layout.setSpacing(14)

            icon_widget = IconWidget(icon, row_container)
            icon_widget.setFixedSize(26, 26)
            row_layout.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)

            text_container = QWidget(row_container)
            text_layout = QVBoxLayout(text_container)
            text_layout.setContentsMargins(0, 0, 0, 0)
            text_layout.setSpacing(2)
            title_label = StrongBodyLabel(title_text, text_container)
            title_label.setStyleSheet("font-size: 15px;")
            text_layout.addWidget(title_label)
            if desc_text:
                desc_label = CaptionLabel(desc_text, text_container)
                desc_label.setStyleSheet("color: #6B6B6B; font-size: 12px;")
                text_layout.addWidget(desc_label)
            text_container.setFixedWidth(300)
            row_layout.addWidget(text_container, 0, Qt.AlignmentFlag.AlignVCenter)

            row_layout.addStretch(1)

            control_container = QWidget(row_container)
            control_layout = QHBoxLayout(control_container)
            control_layout.setContentsMargins(0, 0, 0, 0)
            control_layout.setSpacing(8)
            control_layout.addWidget(controls)
            row_layout.addWidget(control_container, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            translation_body_layout.addWidget(row_container)

            self._translation_rows.append(row_container)
            if add_divider:
                divider = QFrame(translation_body)
                divider.setFixedHeight(1)
                divider.setStyleSheet("background-color: #E5E5E5;")
                translation_body_layout.addWidget(divider)
                self._translation_dividers.append(divider)
            else:
                self._translation_dividers.append(None)

        self.provider_combo = ComboBox()
        self.provider_combo.setMinimumWidth(240)
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        provider_controls = QWidget(translation_body)
        provider_layout = QHBoxLayout(provider_controls)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(8)
        provider_layout.addWidget(self.provider_combo, 1)
        add_translation_row(
            FluentIcon.CONNECT,
            "翻译接口",
            "选择翻译服务提供商",
            provider_controls,
        )

        self.model_combo = ComboBox()
        self.model_combo.setMinimumWidth(240)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        add_translation_row(
            FluentIcon.ROBOT,
            "翻译模型",
            "选择要使用的模型",
            self.model_combo,
        )

        def build_slider_control() -> tuple[QWidget, Slider, BodyLabel]:
            container = QWidget(translation_body)
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)
            value_row = QHBoxLayout()
            value_row.setContentsMargins(0, 0, 0, 0)
            value_row.addStretch(1)
            value_label = BodyLabel("0", container)
            value_label.setStyleSheet("color: #6B6B6B; font-size: 13px;")
            value_row.addWidget(value_label)
            layout.addLayout(value_row)
            slider = Slider(Qt.Orientation.Horizontal, container)
            slider.setMinimumWidth(260)
            layout.addWidget(slider)
            return container, slider, value_label

        batch_container, self.batch_slider, self.batch_value_label = build_slider_control()
        self.batch_slider.setRange(10, 300)
        self.batch_slider.setSingleStep(10)
        self.batch_slider.setPageStep(10)
        self.batch_slider.valueChanged.connect(self._on_batch_slider_changed)
        add_translation_row(
            FluentIcon.FONT_SIZE,
            "批处理大小",
            "每次翻译的字幕段数量",
            batch_container,
        )

        concurrency_container, self.concurrency_slider, self.concurrency_value_label = build_slider_control()
        self.concurrency_slider.setRange(1, 10)
        self.concurrency_slider.setSingleStep(1)
        self.concurrency_slider.setPageStep(1)
        self.concurrency_slider.valueChanged.connect(self._on_concurrency_slider_changed)
        add_translation_row(
            FluentIcon.SPEED_HIGH,
            "并发数",
            "同时进行的翻译任务数",
            concurrency_container,
            add_divider=True,
        )

        self.resume_translate_switch = SwitchButton(translation_body)
        self.resume_translate_switch.setOnText("启用")
        self.resume_translate_switch.setOffText("停用")
        self.resume_translate_switch.setChecked(
            bool(self._settings.get("automation_translation_resume", True))
        )
        self.resume_translate_switch.checkedChanged.connect(self._on_resume_translation_changed)
        add_translation_row(
            FluentIcon.SYNC,
            "断点续译",
            "检测到历史进度时继续翻译。",
            self.resume_translate_switch,
            add_divider=False,
        )

        translation_layout.addWidget(translation_body)
        right_layout.addWidget(translation_card)

        segment_card = CardWidget(right_col)
        self.segment_card = segment_card
        segment_layout = QVBoxLayout(segment_card)
        segment_layout.setContentsMargins(16, 16, 16, 16)
        segment_layout.setSpacing(8)
        self.segment_title = ClickableTitleLabel("语义分段", segment_card)
        self.segment_title.clicked.connect(self._toggle_segment_options)
        segment_layout.addWidget(self.segment_title)

        segment_body = QWidget(segment_card)
        segment_body_layout = QVBoxLayout(segment_body)
        segment_body_layout.setContentsMargins(0, 0, 0, 0)
        segment_body_layout.setSpacing(0)

        self._segment_rows: list[QWidget] = []
        self._segment_dividers: list[QFrame | None] = []

        def add_segment_row(
            icon: FluentIcon,
            title_text: str,
            desc_text: str,
            controls: QWidget,
            add_divider: bool = True,
        ) -> None:
            row_container = QWidget(segment_body)
            row_layout = QHBoxLayout(row_container)
            row_layout.setContentsMargins(0, 10, 0, 10)
            row_layout.setSpacing(14)

            icon_widget = IconWidget(icon, row_container)
            icon_widget.setFixedSize(26, 26)
            row_layout.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)

            text_container = QWidget(row_container)
            text_layout = QVBoxLayout(text_container)
            text_layout.setContentsMargins(0, 0, 0, 0)
            text_layout.setSpacing(2)
            title_label = StrongBodyLabel(title_text, text_container)
            title_label.setStyleSheet("font-size: 15px;")
            text_layout.addWidget(title_label)
            if desc_text:
                desc_label = CaptionLabel(desc_text, text_container)
                desc_label.setStyleSheet("color: #6B6B6B; font-size: 12px;")
                text_layout.addWidget(desc_label)
            text_container.setFixedWidth(280)
            row_layout.addWidget(text_container, 0, Qt.AlignmentFlag.AlignVCenter)

            row_layout.addStretch(1)

            control_container = QWidget(row_container)
            control_layout = QHBoxLayout(control_container)
            control_layout.setContentsMargins(0, 0, 0, 0)
            control_layout.setSpacing(8)
            control_layout.addWidget(controls)
            row_layout.addWidget(control_container, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            segment_body_layout.addWidget(row_container)

            self._segment_rows.append(row_container)
            if add_divider:
                divider = QFrame(segment_body)
                divider.setFixedHeight(1)
                divider.setStyleSheet("background-color: #E5E5E5;")
                segment_body_layout.addWidget(divider)
                self._segment_dividers.append(divider)
            else:
                self._segment_dividers.append(None)

        self.seg_provider_combo = ComboBox()
        self.seg_provider_combo.setMinimumWidth(240)
        self.seg_provider_combo.currentTextChanged.connect(self._on_seg_provider_changed)
        provider_controls = QWidget(segment_body)
        provider_layout = QHBoxLayout(provider_controls)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(8)
        provider_layout.addWidget(self.seg_provider_combo, 1)
        self.seg_refresh_btn = PushButton("刷新")
        self.seg_refresh_btn.clicked.connect(self._reload_segmentation_providers)
        provider_layout.addWidget(self.seg_refresh_btn)
        add_segment_row(
            FluentIcon.CONNECT,
            "分段接口",
            "选择用于语义分段的接口",
            provider_controls,
        )

        self.seg_model_combo = ComboBox()
        self.seg_model_combo.setMinimumWidth(240)
        self.seg_model_combo.currentTextChanged.connect(self._on_seg_model_changed)
        add_segment_row(
            FluentIcon.ROBOT,
            "分段模型",
            "选择用于分段的模型",
            self.seg_model_combo,
        )

        self.seg_temperature = DoubleSpinBox()
        self.seg_temperature.setRange(0.0, 1.0)
        self.seg_temperature.setSingleStep(0.1)
        self.seg_temperature.setDecimals(2)
        self.seg_temperature.setMinimumWidth(140)
        add_segment_row(
            FluentIcon.BRIGHTNESS,
            "分段温度",
            "调整标点修正的随机性",
            self.seg_temperature,
        )

        self.seg_timeout = SpinBox()
        self.seg_timeout.setRange(30, 600)
        self.seg_timeout.setSingleStep(30)
        self.seg_timeout.setMinimumWidth(140)
        add_segment_row(
            FluentIcon.STOP_WATCH,
            "超时（秒）",
            "单次 LLM 请求超时阈值",
            self.seg_timeout,
        )

        self.seg_max_retries = SpinBox()
        self.seg_max_retries.setRange(0, 5)
        self.seg_max_retries.setMinimumWidth(140)
        add_segment_row(
            FluentIcon.SYNC,
            "最大重试",
            "校验失败或请求失败后的重试次数",
            self.seg_max_retries,
        )

        self.seg_batch_concurrency = SpinBox()
        self.seg_batch_concurrency.setRange(1, 5)
        self.seg_batch_concurrency.setMinimumWidth(140)
        add_segment_row(
            FluentIcon.TILES,
            "同时发送批次数",
            "同时并发发送的语义批次数",
            self.seg_batch_concurrency,
        )

        self.seg_failure_fallback = CheckBox()
        self.seg_failure_fallback.setText("")
        add_segment_row(
            FluentIcon.CANCEL,
            "失败回退",
            "关闭时重试到报错；开启时失败后改用纯规则。",
            self.seg_failure_fallback,
            add_divider=False,
        )

        self.segment_note = CaptionLabel("仅 JSON 输入支持智能分割，翻译会自动应用分段结果。", segment_body)
        self.segment_note.setWordWrap(True)
        self.segment_note.setStyleSheet("color: #6B6B6B; font-size: 12px;")
        self.segment_note_container = QWidget(segment_body)
        note_layout = QVBoxLayout(self.segment_note_container)
        note_layout.setContentsMargins(0, 6, 0, 0)
        note_layout.setSpacing(0)
        note_layout.addWidget(self.segment_note)
        segment_body_layout.addWidget(self.segment_note_container)

        segment_layout.addWidget(segment_body)
        right_layout.addWidget(segment_card)
        right_layout.addStretch(1)

        self._set_translation_expanded(False)
        self._set_segment_expanded(False)

        main_layout.addWidget(left_col)
        main_layout.addWidget(right_col, 1)
        content_layout.addWidget(main_row)
        content_layout.addStretch(1)

        bottom_container = QWidget(self)
        bottom_container_layout = QHBoxLayout(bottom_container)
        bottom_container_layout.setContentsMargins(24, 0, 24, 0)
        bottom_container_layout.setSpacing(0)

        bottom_bar = CardWidget(bottom_container)
        bottom_bar.setObjectName("automation_bottom_bar")
        bottom_bar.setFixedHeight(60)
        bottom_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(24, 6, 24, 6)
        bottom_layout.setSpacing(12)
        self.clear_files_btn = PushButton("清空列表")
        self.clear_files_btn.setFixedHeight(32)
        self.clear_files_btn.clicked.connect(self._clear_files)
        bottom_layout.addWidget(self.clear_files_btn)
        bottom_layout.addStretch(1)
        self.start_btn = PushButton("开始执行")
        self.start_btn.setFixedHeight(32)
        self.start_btn.setMinimumWidth(140)
        self.start_btn.clicked.connect(self._start_automation)
        bottom_layout.addWidget(self.start_btn)
        bottom_container_layout.addWidget(bottom_bar)
        root.addWidget(bottom_container)
        self._update_merge_switch_state()
        self._apply_action_button_styles()

    def _apply_palette(self, *_: object) -> None:
        palette = build_theme_palette(themeColor())
        list_bg = color_to_hex(palette.surface_2)
        border = color_to_hex(palette.border_strong, with_alpha=True)
        selected = QColor(themeColor())
        selected.setAlphaF(0.12)
        selected_bg = color_to_hex(selected, with_alpha=True)
        self.file_list.setStyleSheet(
            "QListWidget {"
            f"background-color: {list_bg};"
            f"border: 1px solid {border};"
            "border-radius: 8px;"
            "font-size: 12px;"
            "padding: 4px;"
            "}"
            "QListWidget::item {"
            "padding: 6px 8px;"
            "}"
            "QListWidget::item:selected {"
            f"background-color: {selected_bg};"
            "}"
        )
        self._apply_action_button_styles()

    @staticmethod
    def _scale_color(color: QColor, factor: float) -> QColor:
        return QColor(
            max(0, min(255, int(color.red() * factor))),
            max(0, min(255, int(color.green() * factor))),
            max(0, min(255, int(color.blue() * factor))),
        )

    def _apply_action_button_styles(self) -> None:
        primary = QColor(themeColor())
        primary_hover = self._scale_color(primary, 0.9)

        palette = build_theme_palette(primary)
        secondary_bg = color_to_hex(palette.surface_2)
        secondary_hover = color_to_hex(palette.surface_3)
        secondary_border = color_to_hex(palette.border_strong, with_alpha=True)
        neutral_disabled_bg = "#ECEFF3"
        neutral_disabled_border = "#D4DAE2"
        neutral_disabled_text = "#99A1AB"

        primary_qss = (
            "PushButton, QPushButton {"
            f"background-color: {primary.name()};"
            f"border: 1px solid {primary.name()};"
            "color: #FFFFFF;"
            "border-radius: 6px;"
            "padding: 6px 14px;"
            "}"
            "PushButton:hover, QPushButton:hover {"
            f"background-color: {primary_hover.name()};"
            f"border-color: {primary_hover.name()};"
            "}"
            "PushButton:disabled, QPushButton:disabled {"
            f"background-color: {neutral_disabled_bg};"
            f"border-color: {neutral_disabled_border};"
            f"color: {neutral_disabled_text};"
            "}"
        )
        secondary_qss = (
            "PushButton, QPushButton {"
            f"background-color: {secondary_bg};"
            f"border: 1px solid {secondary_border};"
            "color: #1F2937;"
            "border-radius: 6px;"
            "padding: 6px 14px;"
            "}"
            "PushButton:hover, QPushButton:hover {"
            f"background-color: {secondary_hover};"
            "}"
            "PushButton:disabled, QPushButton:disabled {"
            f"background-color: {neutral_disabled_bg};"
            f"border-color: {neutral_disabled_border};"
            f"color: {neutral_disabled_text};"
            "}"
        )

        button_styles = {
            "start_btn": primary_qss,
            "add_file_btn": secondary_qss,
            "clear_files_btn": secondary_qss,
            "output_dir_btn": secondary_qss,
            "seg_refresh_btn": secondary_qss,
        }
        for attr, qss in button_styles.items():
            button = getattr(self, attr, None)
            if button is not None:
                setCustomStyleSheet(button, qss, qss)

    @staticmethod
    def _build_row(title: str, desc: str, control: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(12)

        text_container = QWidget()
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.addWidget(StrongBodyLabel(title, text_container))
        if desc:
            desc_label = CaptionLabel(desc, text_container)
            desc_label.setStyleSheet("color: #6B6B6B;")
            desc_label.setWordWrap(True)
            text_layout.addWidget(desc_label)
        text_container.setFixedWidth(140)

        row.addWidget(text_container)
        row.addStretch(1)
        row.addWidget(control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return row

    def _add_option_row(
        self,
        parent_layout: QVBoxLayout,
        title: str,
        desc: str,
        control: QWidget,
        *,
        add_divider: bool = True,
        row_v_margin: int = 10,
    ) -> tuple[QWidget, QFrame | None]:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, row_v_margin, 0, row_v_margin)
        layout.setSpacing(12)

        text_container = QWidget(row)
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        title_label = StrongBodyLabel(title, text_container)
        text_layout.addWidget(title_label)
        if desc:
            desc_label = CaptionLabel(desc, text_container)
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet("color: #6B6B6B;")
            text_layout.addWidget(desc_label)

        layout.addWidget(text_container, 1)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        parent_layout.addWidget(row)

        divider = None
        if add_divider:
            divider = QFrame()
            divider.setFixedHeight(1)
            divider.setStyleSheet("background-color: #E5E5E5;")
            parent_layout.addWidget(divider)
        return row, divider

    def _toggle_translation_options(self) -> None:
        self._set_translation_expanded(not self._translation_expanded)

    def _set_translation_expanded(self, expanded: bool) -> None:
        self._translation_expanded = expanded
        if not hasattr(self, "_translation_rows"):
            return
        visible_count = len(self._translation_rows) if expanded else min(2, len(self._translation_rows))
        for idx, row in enumerate(self._translation_rows):
            row.setVisible(idx < visible_count)
            divider = self._translation_dividers[idx] if idx < len(self._translation_dividers) else None
            if divider is not None:
                divider.setVisible(idx < visible_count - 1)

    def _toggle_segment_options(self) -> None:
        self._set_segment_expanded(not self._segment_expanded)

    def _set_segment_expanded(self, expanded: bool) -> None:
        self._segment_expanded = expanded
        if not hasattr(self, "_segment_rows"):
            return
        visible_count = len(self._segment_rows) if expanded else min(2, len(self._segment_rows))
        for idx, row in enumerate(self._segment_rows):
            row.setVisible(idx < visible_count)
            divider = self._segment_dividers[idx] if idx < len(self._segment_dividers) else None
            if divider is not None:
                divider.setVisible(idx < visible_count - 1)
        if hasattr(self, "segment_note_container"):
            self.segment_note_container.setVisible(expanded)

    def _is_segmentation_feature_enabled(self) -> bool:
        return bool(self._settings.get("segmentation_enabled", False))

    def apply_segmentation_feature_state(self, enabled: bool | None = None) -> None:
        if enabled is None:
            self._settings = self._load_settings()
            enabled = bool(self._settings.get("segmentation_enabled", False))
        else:
            self._settings["segmentation_enabled"] = bool(enabled)

        if hasattr(self, "segment_switch_row"):
            self.segment_switch_row.setVisible(enabled)
        if getattr(self, "segment_switch_divider", None) is not None:
            self.segment_switch_divider.setVisible(enabled)
        if hasattr(self, "segment_card"):
            self.segment_card.setVisible(enabled)
        if hasattr(self, "segmentation_hint"):
            self.segmentation_hint.setVisible(enabled)

        if not enabled:
            self.segment_switch.setChecked(False)
            self._set_segment_expanded(False)

        if hasattr(self, "segment_switch"):
            self.segment_switch.setEnabled(enabled and not self._running)
        if hasattr(self, "seg_provider_combo"):
            self.seg_provider_combo.setEnabled(enabled and not self._running)
        if hasattr(self, "seg_model_combo"):
            self.seg_model_combo.setEnabled(enabled and not self._running)
        if hasattr(self, "seg_temperature"):
            self.seg_temperature.setEnabled(enabled and not self._running)
        if hasattr(self, "seg_timeout"):
            self.seg_timeout.setEnabled(enabled and not self._running)
        if hasattr(self, "seg_max_retries"):
            self.seg_max_retries.setEnabled(enabled and not self._running)
        if hasattr(self, "seg_refresh_btn"):
            self.seg_refresh_btn.setEnabled(enabled and not self._running)

    def _on_keep_intermediate_changed(self, checked: bool) -> None:
        self._settings["automation_keep_intermediate"] = bool(checked)
        self._config_store.save_user_settings(self._settings)

    def _save_workflow_switch_settings(self) -> None:
        self._settings["automation_cleanup"] = bool(
            getattr(self, "clean_switch", None) and self.clean_switch.isChecked()
        )
        self._settings["automation_optimize"] = bool(
            getattr(self, "optimize_switch", None) and self.optimize_switch.isChecked()
        )
        self._settings["automation_post_cleanup"] = bool(
            getattr(self, "post_clean_switch", None) and self.post_clean_switch.isChecked()
        )
        if not getattr(self, "_initializing", False):
            self._config_store.save_user_settings(self._settings)

    def _enforce_cleanup_optimize_mutex(self, *, prefer: str = "optimize") -> None:
        if not hasattr(self, "clean_switch") or not hasattr(self, "optimize_switch"):
            return
        clean_on = self.clean_switch.isChecked()
        optimize_on = self.optimize_switch.isChecked()
        if clean_on and optimize_on:
            if prefer == "cleanup":
                self.optimize_switch.blockSignals(True)
                self.optimize_switch.setChecked(False)
                self.optimize_switch.blockSignals(False)
            else:
                self.clean_switch.blockSignals(True)
                self.clean_switch.setChecked(False)
                self.clean_switch.blockSignals(False)
        self._save_workflow_switch_settings()

    def _on_clean_changed(self, checked: bool) -> None:
        self._enforce_cleanup_optimize_mutex(prefer="cleanup" if checked else "optimize")

    def _on_optimize_changed(self, checked: bool) -> None:
        self._enforce_cleanup_optimize_mutex(prefer="optimize" if checked else "cleanup")

    def _on_post_cleanup_changed(self, checked: bool) -> None:
        if checked and not self.translate_switch.isChecked():
            self.post_clean_switch.blockSignals(True)
            self.post_clean_switch.setChecked(False)
            self.post_clean_switch.blockSignals(False)
        self._save_workflow_switch_settings()

    def _load_settings(self) -> dict:
        defaults = {
            "current_provider": "openai",
            "model": "",
            "api_keys": {},
            "source_language": "ja",
            "target_language": "zh-CN",
            "batch_size": 10,
            "concurrency": 3,
            "timeout": 60,
            "max_retries": 2,
            "batch_retries": 2,
            "proxy_enabled": False,
            "proxy_address": "",
            "debug_mode": 0,
            "custom_prompt": "",
            "last_output_dir": "",
            "segmentation_enabled": False,
            "segmentation_provider": "",
            "segmentation_model": "",
            "segmentation_max_chars": 4000,
            "segmentation_enable_summary": True,
            "segmentation_temperature": 0.2,
            "segmentation_timeout": 180,
            "segmentation_max_retries": 1,
            "segmentation_batch_concurrency": 2,
            "segmentation_fallback_on_failure": False,
            "system_prompt": "",
            "automation_keep_intermediate": False,
            "automation_cleanup": True,
            "automation_optimize": False,
            "automation_post_cleanup": False,
            "merge_strategy": "pass1_primary",
            "automation_translation_resume": True,
            "automation_resume_pipeline": False,
        }
        data = self._config_store.load_all()
        stored = data.get("user_settings") or {}
        merged = defaults.copy()
        merged.update(stored)
        system_prompts = data.get("system_prompts") or {}
        resolved_prompt_id, content = resolve_system_prompt(merged, system_prompts)
        if resolved_prompt_id and not merged.get("system_prompt_id"):
            merged["system_prompt_id"] = resolved_prompt_id
        if content:
            merged["system_prompt"] = content
        if merged.get("system_prompt_id") and not stored.get("system_prompt_id"):
            stored["system_prompt_id"] = merged["system_prompt_id"]
            self._config_store.save_user_settings(stored)
        return merged

    def reload_provider_configs(self) -> None:
        self._initializing = True
        self._settings = self._load_settings()
        self._api_manager.reload_providers()
        self._load_provider_models()
        self._apply_provider_settings()
        self._apply_segmentation_settings()
        self._apply_merge_settings()
        self._initializing = False

    def _load_provider_models(self) -> None:
        providers = self._api_manager.get_providers()
        self.provider_combo.blockSignals(True)
        self.seg_provider_combo.blockSignals(True)
        try:
            self.provider_combo.clear()
            self.seg_provider_combo.clear()
            self.provider_combo.addItems(providers)
            self.seg_provider_combo.addItems(providers)
        finally:
            self.provider_combo.blockSignals(False)
            self.seg_provider_combo.blockSignals(False)
        self._refresh_translation_models(self.provider_combo.currentText())
        self._refresh_segmentation_models(self.seg_provider_combo.currentText())

    def _apply_provider_settings(self) -> None:
        provider = self._settings.get("current_provider", "")
        if provider and self.provider_combo.findText(provider) >= 0:
            self.provider_combo.setCurrentText(provider)
        self._refresh_translation_models(self.provider_combo.currentText())
        model = self._settings.get("model", "")
        if model and self.model_combo.findText(model) >= 0:
            self.model_combo.setCurrentText(model)
        self._apply_translation_param_sliders()

    def _apply_segmentation_settings(self) -> None:
        provider = self._settings.get("segmentation_provider") or self._settings.get("current_provider", "")
        if provider and self.seg_provider_combo.findText(provider) >= 0:
            self.seg_provider_combo.setCurrentText(provider)
        self._refresh_segmentation_models(self.seg_provider_combo.currentText())
        model = self._settings.get("segmentation_model") or self._settings.get("model", "")
        if model and self.seg_model_combo.findText(model) >= 0:
            self.seg_model_combo.setCurrentText(model)
        self.seg_temperature.setValue(float(self._settings.get("segmentation_temperature", 0.2)))
        self.seg_timeout.setValue(int(self._settings.get("segmentation_timeout", self._settings.get("timeout", 180))))
        self.seg_max_retries.setValue(int(self._settings.get("segmentation_max_retries", self._settings.get("max_retries", 1))))
        self.seg_batch_concurrency.setValue(int(self._settings.get("segmentation_batch_concurrency", 2)))
        self.seg_failure_fallback.setChecked(bool(self._settings.get("segmentation_fallback_on_failure", False)))

    def _apply_merge_settings(self) -> None:
        if not hasattr(self, "merge_strategy_combo"):
            return
        key = self._settings.get("merge_strategy", "pass1_primary")
        label = self.MERGE_STRATEGY_LABELS.get(key, self.MERGE_STRATEGY_LABELS["pass1_primary"])
        index = self.merge_strategy_combo.findText(label)
        if index >= 0:
            self.merge_strategy_combo.setCurrentIndex(index)

    def _refresh_translation_models(self, provider: str) -> None:
        models = self._api_manager.get_available_models(provider)
        self.model_combo.blockSignals(True)
        try:
            self.model_combo.clear()
            self.model_combo.addItems(models)
        finally:
            self.model_combo.blockSignals(False)
        if models:
            default_model = self._api_manager.get_default_model(provider)
            if default_model and default_model in models:
                self.model_combo.setCurrentText(default_model)
            elif not self.model_combo.currentText():
                self.model_combo.setCurrentIndex(0)

    def _refresh_segmentation_models(self, provider: str) -> None:
        models = self._api_manager.get_available_models(provider)
        self.seg_model_combo.blockSignals(True)
        try:
            self.seg_model_combo.clear()
            self.seg_model_combo.addItems(models)
        finally:
            self.seg_model_combo.blockSignals(False)
        if models:
            default_model = self._api_manager.get_default_model(provider)
            if default_model and default_model in models:
                self.seg_model_combo.setCurrentText(default_model)
            elif not self.seg_model_combo.currentText():
                self.seg_model_combo.setCurrentIndex(0)

    def _apply_translation_param_sliders(self) -> None:
        if not hasattr(self, "batch_slider"):
            return
        batch_value = int(self._settings.get("batch_size", 120))
        batch_value = max(10, min(300, batch_value))
        batch_value = int(round(batch_value / 10) * 10)
        self.batch_slider.blockSignals(True)
        self.batch_slider.setValue(batch_value)
        self.batch_slider.blockSignals(False)
        self.batch_value_label.setText(str(batch_value))

        concurrency_value = int(self._settings.get("concurrency", 2))
        concurrency_value = max(1, min(10, concurrency_value))
        self.concurrency_slider.blockSignals(True)
        self.concurrency_slider.setValue(concurrency_value)
        self.concurrency_slider.blockSignals(False)
        self.concurrency_value_label.setText(str(concurrency_value))

    def _on_batch_slider_changed(self, value: int) -> None:
        step = 10
        snapped = int(round(value / step) * step)
        if snapped != value:
            self.batch_slider.blockSignals(True)
            self.batch_slider.setValue(snapped)
            self.batch_slider.blockSignals(False)
        self.batch_value_label.setText(str(snapped))
        self._settings["batch_size"] = snapped
        self._config_store.save_user_settings(self._settings)

    def _on_concurrency_slider_changed(self, value: int) -> None:
        self.concurrency_value_label.setText(str(value))
        self._settings["concurrency"] = value
        self._config_store.save_user_settings(self._settings)

    def _reload_segmentation_providers(self) -> None:
        self.reload_provider_configs()

    def _on_provider_changed(self, provider: str) -> None:
        if getattr(self, "_initializing", False):
            return
        self._refresh_translation_models(provider)
        provider = provider.strip()
        if not provider:
            return
        self._settings["current_provider"] = provider
        model = self.model_combo.currentText().strip()
        if model:
            self._settings["model"] = model
        self._config_store.save_user_settings(self._settings)
        self.provider_changed.emit(provider)

    def _on_model_changed(self, model: str) -> None:
        if getattr(self, "_initializing", False):
            return
        if not model:
            return
        self._settings["model"] = model
        self._config_store.save_user_settings(self._settings)

    def _on_seg_provider_changed(self, provider: str) -> None:
        if getattr(self, "_initializing", False):
            return
        self._refresh_segmentation_models(provider)
        provider = provider.strip()
        if not provider:
            return
        self._settings["segmentation_provider"] = provider
        self._config_store.save_user_settings(self._settings)

    def _on_seg_model_changed(self, model: str) -> None:
        if getattr(self, "_initializing", False):
            return
        if not model:
            return
        self._settings["segmentation_model"] = model
        self._config_store.save_user_settings(self._settings)

    def _on_resume_translation_changed(self, checked: bool) -> None:
        self._settings["automation_translation_resume"] = bool(checked)
        self._config_store.save_user_settings(self._settings)

    def _on_resume_pipeline_changed(self, checked: bool) -> None:
        self._settings["automation_resume_pipeline"] = bool(checked)
        self._config_store.save_user_settings(self._settings)

    def _on_merge_strategy_changed(self, label: str) -> None:
        key = self._current_merge_strategy_key(label)
        self._settings["merge_strategy"] = key
        self._config_store.save_user_settings(self._settings)

    def _current_merge_strategy_key(self, label: str | None = None) -> str:
        current_label = label or getattr(self, "merge_strategy_combo", None)
        if hasattr(current_label, "currentText"):
            current_label = current_label.currentText()
        if isinstance(current_label, str):
            for key, value in self.MERGE_STRATEGY_LABELS.items():
                if value == current_label:
                    return key
        return "pass1_primary"

    def _get_api_key_for_provider(self, provider: str) -> str:
        api_keys = self._settings.get("api_keys", {})
        return api_keys.get(provider, "") or ""

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择字幕文件",
            "",
            "字幕文件 (*.srt *.json)",
        )
        if not paths:
            return
        self._add_files([to_native_path(path) for path in paths if path])

    def _on_files_dropped(self, paths: Iterable[str]) -> None:
        self._add_files(paths)

    def _add_files(self, paths: Iterable[str]) -> None:
        for path in paths:
            file_path = Path(path)
            if file_path not in self._file_paths:
                self._file_paths.append(file_path)
        if len(self._file_paths) > 2:
            show_warning(self, "文件过多", "最多只能选择 2 个字幕文件。")
            self._file_paths = self._file_paths[:2]
        self._refresh_file_list()

    def _refresh_file_list(self) -> None:
        self.file_list.clear()
        if not self._file_paths:
            self.file_list.setVisible(False)
        else:
            for path in self._file_paths:
                self.file_list.addItem(path.name)
            self.file_list.setVisible(True)
        self._update_merge_switch_state()

    def _clear_files(self) -> None:
        self._file_paths = []
        self._refresh_file_list()
        self._reset_status()

    def _update_merge_switch_state(self) -> None:
        has_two = len(self._file_paths) == 2
        translate_on = self.translate_switch.isChecked()
        if hasattr(self, "post_clean_switch"):
            self.post_clean_switch.setEnabled(translate_on)
            if not translate_on:
                self.post_clean_switch.setChecked(False)
        merge_enabled = has_two and translate_on
        self.merge_switch.setEnabled(merge_enabled)
        if not has_two:
            self.merge_switch.setChecked(False)
        if hasattr(self, "merge_strategy_combo"):
            show_strategy = merge_enabled and self.merge_switch.isChecked()
            self.merge_strategy_combo.setEnabled(show_strategy)
            if hasattr(self, "_merge_strategy_row"):
                self._merge_strategy_row.setVisible(show_strategy)
            if getattr(self, "_merge_switch_divider", None) is not None:
                self._merge_switch_divider.setVisible(show_strategy)

    def _pick_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self._output_dir or "")
        if not path:
            return
        normalized = to_native_path(path)
        self.output_dir_line.setText(normalized)
        self._output_dir = normalized

    def _on_output_dir_edited(self) -> None:
        self._output_dir = to_native_path(self.output_dir_line.text())

    def _start_automation(self) -> None:
        if self._running:
            show_info(self, "正在执行", "当前已有自动化流程在运行。")
            return
        if not self._file_paths:
            show_warning(self, "缺少文件", "请先选择字幕文件。")
            return

        segmentation_enabled = self._is_segmentation_feature_enabled()
        if not segmentation_enabled:
            self.segment_switch.setChecked(False)
        segment_on = segmentation_enabled and self.segment_switch.isChecked()
        self._enforce_cleanup_optimize_mutex(prefer="optimize")
        optimize_on = bool(getattr(self, "optimize_switch", None) and self.optimize_switch.isChecked())


        if segment_on:
            for path in self._file_paths:
                if path.suffix.lower() != ".json":
                    show_warning(self, "文件格式不匹配", "语义分段/对齐修段仅支持 JSON 输入。")
                    return
        if optimize_on:
            for path in self._file_paths:
                if path.suffix.lower() != ".json":
                    show_warning(self, "文件格式不匹配", "优化字幕仅支持 JSON 输入。")
                    return
        if self.translate_switch.isChecked() and not segment_on and not optimize_on:
            for path in self._file_paths:
                if path.suffix.lower() == ".json":
                    show_warning(self, "翻译输入不完整", "JSON 输入需要语义分段或优化字幕生成 SRT。")
                    return

        if self.merge_switch.isChecked() and len(self._file_paths) != 2:
            show_warning(self, "无法合并", "合并字幕需要选择 2 个文件。")
            return

        if self.translate_switch.isChecked():
            provider = self.provider_combo.currentText().strip()
            model = self.model_combo.currentText().strip()
            api_key = self._get_api_key_for_provider(provider)
            if not provider or not model:
                show_warning(self, "翻译配置缺失", "请先设置翻译接口和模型。")
                return
            if not api_key:
                show_warning(self, "缺少翻译 API Key", "请先在接口管理中配置 API Key。")
                return

        if segment_on:
            if self._build_segmentation_config() is None:
                return

        self._prepare_pipeline()

    def _prepare_pipeline(self, *, _force_fresh: bool = False) -> None:
        self._jobs = []
        self._steps = []
        self._current_step = 0
        self._last_step_value = 0
        self._last_translation_count = 0
        self._translation_active = False
        self._set_ring_status("执行中", 0)
        segmentation_enabled = self._is_segmentation_feature_enabled()
        segment_on = segmentation_enabled and self.segment_switch.isChecked()
        self._enforce_cleanup_optimize_mutex(prefer="optimize")
        clean_on = bool(getattr(self, "clean_switch", None) and self.clean_switch.isChecked())
        optimize_on = bool(getattr(self, "optimize_switch", None) and self.optimize_switch.isChecked())
        post_clean_on = bool(getattr(self, "post_clean_switch", None) and self.post_clean_switch.isChecked())

        output_dir = self._resolve_output_dir()
        if not output_dir:
            show_warning(self, "输出目录无效", "请确认输出目录存在。")
            return
        self._output_dir = str(output_dir)
        self.output_dir_line.setText(self._output_dir)

        resume_pipeline = bool(
            getattr(self, "resume_pipeline_switch", None)
            and self.resume_pipeline_switch.isChecked()
        ) and not _force_fresh
        temp_root = self._find_resume_root(output_dir) if resume_pipeline else None
        if temp_root:
            self._append_log(f"检测到可续跑目录：{temp_root}")
        else:
            if resume_pipeline:
                self._append_log("未检测到可续跑目录，将新建任务目录。")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_root = output_dir / f"Temp_Automation_{timestamp}"
            try:
                temp_root.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                show_error(self, "准备失败", f"无法创建临时目录：{exc}")
                return
        self._temp_root = temp_root

        for idx, original in enumerate(self._file_paths, start=1):
            try:
                work_dir = temp_root / f"input_{idx}"
                work_dir.mkdir(parents=True, exist_ok=True)
                source_copy = work_dir / original.name
                if not source_copy.exists():
                    shutil.copy2(original, source_copy)
            except Exception as exc:
                self._temp_root = None
                show_error(self, "准备失败", f"无法准备文件：{exc}")
                return
            self._jobs.append(
                AutomationJob(
                    index=idx,
                    original_path=original,
                    work_dir=work_dir,
                    source_path=source_copy,
                )
            )
            if resume_pipeline:
                self._hydrate_job_from_workdir(self._jobs[-1])

        for job in self._jobs:
            if clean_on:
                if not resume_pipeline or not self._has_clean_output(job):
                    self._steps.append((f"文本净化（文件 {job.index}）", lambda j=job: self._run_cleanup(j)))
            if segment_on:
                if not resume_pipeline or not (job.segmented_srt and job.segmented_srt.exists() and job.segmented_json and job.segmented_json.exists()):
                    self._steps.append((f"语义分段（文件 {job.index}）", lambda j=job: self._run_segmentation(j)))
            if optimize_on:
                if not resume_pipeline or not (job.optimized_srt and job.optimized_srt.exists()):
                    self._steps.append((f"优化字幕（文件 {job.index}）", lambda j=job: self._run_optimize(j)))
            if self.translate_switch.isChecked():
                if not resume_pipeline or not self._should_skip_translation(job):
                    self._steps.append((f"自动翻译（文件 {job.index}）", lambda j=job: self._run_translation(j)))
            if post_clean_on:
                if job.translated_srt and job.translated_srt.exists():
                    post_clean_output = self._resolve_post_cleanup_output(job.translated_srt)
                    if post_clean_output and post_clean_output.exists():
                        job.post_cleaned_srt = post_clean_output
                if not resume_pipeline or not (job.post_cleaned_srt and job.post_cleaned_srt.exists()):
                    self._steps.append((f"译后清理（文件 {job.index}）", lambda j=job: self._run_post_cleanup(j)))

        if self.merge_switch.isChecked():
            self._steps.append(("合并字幕", self._run_merge))

        self._total_steps = len(self._steps)
        if self._total_steps == 0:
            if resume_pipeline and not _force_fresh:
                self._append_log("续跑模式下未发现待执行步骤，自动切换为新任务重跑。")
                self._prepare_pipeline(_force_fresh=True)
                return
            show_warning(self, "无可执行步骤", "请至少启用一个流程步骤。")
            return

        self._set_running(True)
        self._append_log(f"自动化目录：{temp_root}")
        self._run_next_step()

    def _find_resume_root(self, output_dir: Path) -> Path | None:
        candidates = sorted(output_dir.glob("Temp_Automation_*"), reverse=True)
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            if self._is_resume_root_usable(candidate):
                return candidate
        return None

    def _is_resume_root_usable(self, temp_root: Path) -> bool:
        for idx, original in enumerate(self._file_paths, start=1):
            work_dir = temp_root / f"input_{idx}"
            if not work_dir.is_dir():
                return False
            source_copy = work_dir / original.name
            if not source_copy.exists():
                return False
        return True

    def _hydrate_job_from_workdir(self, job: AutomationJob) -> None:
        cleaned_json = job.work_dir / f"{job.source_path.stem}_cleaned_whisper.json"
        if cleaned_json.exists():
            job.cleaned_json = cleaned_json
            optimized = cleaned_json.with_name(f"{cleaned_json.stem}_split.srt")
            if optimized.exists():
                job.optimized_srt = optimized
        if job.optimized_srt is None and job.source_path.suffix.lower() == ".json":
            optimized = job.source_path.with_name(f"{job.source_path.stem}_split.srt")
            if optimized.exists():
                job.optimized_srt = optimized

        semantic_srt = job.work_dir / f"{job.original_path.stem}_semantic.srt"
        legacy_segmented_srt = job.work_dir / f"{job.original_path.stem}_segmented.srt"
        processed_srt = job.work_dir / f"{job.source_path.stem}_processed.srt"
        if semantic_srt.exists():
            job.segmented_srt = semantic_srt
        elif legacy_segmented_srt.exists():
            job.segmented_srt = legacy_segmented_srt
        elif processed_srt.exists():
            job.segmented_srt = processed_srt

        if job.segmented_srt and job.segmented_srt.exists():
            segmented_json = self._resolve_segmented_json_output(job.segmented_srt)
            if segmented_json.exists():
                job.segmented_json = segmented_json

    def _has_clean_output(self, job: AutomationJob) -> bool:
        if job.source_path.suffix.lower() == ".json":
            return bool(job.cleaned_json and job.cleaned_json.exists())
        return bool(job.segmented_srt and job.segmented_srt.exists())

    def _get_translation_input(self, job: AutomationJob) -> Path | None:
        return (
            job.optimized_srt
            or job.segmented_srt
            or job.cleaned_json
            or job.source_path
        )

    def _get_resume_cache_path(self, job: AutomationJob, input_path: Path) -> Path:
        output_dir = Path(self._output_dir) if self._output_dir else input_path.parent
        cache_dir = output_dir / "automation_cache"
        base = job.original_path.stem or f"input_{job.index}"
        return cache_dir / f"{base}_prepared_{job.index}{input_path.suffix}"

    def _resolve_translation_output(self, job: AutomationJob, *, prefer_existing: bool = False) -> Path:
        output_dir = Path(self._output_dir)
        base = job.original_path.stem or f"input_{job.index}"
        candidate = output_dir / f"{base}_translated.srt"
        if prefer_existing and candidate.exists():
            return candidate
        return self._unique_path(candidate)

    def _should_skip_translation(self, job: AutomationJob) -> bool:
        input_path = self._get_translation_input(job)
        if not input_path or input_path.suffix.lower() != ".srt":
            return False

        output_path = self._resolve_translation_output(job, prefer_existing=True)
        resume_enabled = bool(
            getattr(self, "resume_translate_switch", None)
            and self.resume_translate_switch.isChecked()
        )
        state_candidates = [input_path]
        if resume_enabled:
            cache_path = self._get_resume_cache_path(job, input_path)
            if cache_path.resolve() != input_path.resolve():
                state_candidates.append(cache_path)

        for candidate in state_candidates:
            if not candidate.exists():
                continue
            state_manager = TranslationStateManager(str(candidate))
            if not state_manager.has_valid_state():
                continue
            valid, reason = state_manager.validate_source_file()
            if not valid:
                self._append_log(f"断点续译状态不可用：{reason}")
                state_manager.cleanup()
                continue
            total_blocks, translated_count, _ = state_manager.get_total_blocks_info()
            if total_blocks <= 0:
                return False
            if translated_count < total_blocks:
                self._append_log(
                    f"检测到未完成进度（{translated_count}/{total_blocks}），继续翻译。"
                )
                return False
            if output_path.exists():
                job.translated_srt = output_path
                return True
            return False

        if output_path.exists():
            job.translated_srt = output_path
            return True
        return False

    def _resolve_output_dir(self) -> Path | None:
        target = self.output_dir_line.text().strip() or self._output_dir
        if target:
            path = Path(target)
            if path.exists() and path.is_dir():
                return path
        if self._file_paths:
            return self._file_paths[0].parent
        return None

    def _run_next_step(self) -> None:
        if not self._steps:
            self._finish_pipeline()
            return
        self._current_step += 1
        label, action = self._steps.pop(0)
        self._update_progress()
        self._append_log(f"开始步骤：{label}")
        action()

    def _update_progress(self) -> None:
        if self._total_steps <= 0:
            self._set_ring_status("执行中", 0)
            return
        percent = int(((self._current_step - 1) / self._total_steps) * 100)
        percent = max(0, min(100, percent))
        self._set_ring_status("执行中", float(percent))
        delta = max((self._current_step - 1) - self._last_step_value, 0)
        self._last_step_value = self._current_step - 1
        self._add_wave_value(delta)

    def _run_cleanup(self, job: AutomationJob) -> None:
        worker = CleanupSubtitleWorker(
            "standard",
            [job.source_path],
            do_cleaning=True,
            do_formatting=False,
            punc_split=False,
            ellipses_mode="replace",
            merge_short=False,
        )
        worker.log.connect(self._append_log)
        worker.finished.connect(lambda: self._on_cleanup_finished(job))
        worker.failed.connect(self._on_step_failed)
        worker.stopped.connect(self._on_step_stopped)
        self._active_worker = worker
        worker.start()

    def _on_cleanup_finished(self, job: AutomationJob) -> None:
        output = self._resolve_cleanup_output(job.source_path)
        if not output or not output.exists():
            self._on_step_failed("文本净化未生成预期输出文件。")
            return
        if output.suffix.lower() == ".json":
            job.cleaned_json = output
        else:
            job.segmented_srt = output
        self._active_worker = None
        self._run_next_step()

    def _resolve_cleanup_output(self, source_path: Path) -> Path | None:
        if source_path.suffix.lower() == ".json":
            return source_path.with_name(f"{source_path.stem}_cleaned_whisper.json")
        if source_path.suffix.lower() == ".srt":
            return source_path.with_name(f"{source_path.stem}_processed.srt")
        return None

    def _resolve_segmented_json_output(self, segmented_srt: Path) -> Path:
        return segmented_srt.with_name(f"{segmented_srt.stem}.whisper.json")

    def _resolve_optimize_output(self, source_path: Path) -> Path | None:
        if source_path.suffix.lower() != ".json":
            return None
        return source_path.with_name(f"{source_path.stem}_split.srt")

    def _resolve_post_cleanup_output(self, source_path: Path) -> Path | None:
        if source_path.suffix.lower() != ".srt":
            return None
        return source_path.with_name(f"{source_path.stem}_post_cleaned.srt")

    def _run_optimize(self, job: AutomationJob) -> None:
        input_path = job.segmented_json or job.cleaned_json or job.source_path
        if not input_path or input_path.suffix.lower() != ".json":
            self._on_step_failed("优化字幕仅支持 JSON 输入。")
            return
        worker = CleanupSubtitleWorker(
            "optimize",
            [input_path],
        )
        worker.log.connect(self._append_log)
        worker.finished.connect(lambda: self._on_optimize_finished(job, input_path))
        worker.failed.connect(self._on_step_failed)
        worker.stopped.connect(self._on_step_stopped)
        self._active_worker = worker
        worker.start()

    def _on_optimize_finished(self, job: AutomationJob, input_path: Path) -> None:
        output = self._resolve_optimize_output(input_path)
        if not output or not output.exists():
            self._on_step_failed("优化字幕未生成预期输出文件。")
            return
        job.optimized_srt = output
        self._active_worker = None
        self._run_next_step()

    def _run_segmentation(self, job: AutomationJob) -> None:
        input_path = job.cleaned_json or job.source_path
        seg_config = self._build_segmentation_config()
        if seg_config is None:
            return
        if seg_config.debug_mode:
            seg_config.debug_batch_index = job.index
            temp_root = getattr(self, "_temp_root", None)
            if temp_root and not seg_config.debug_task_id:
                seg_config.debug_task_id = temp_root.name
        output_name = f"{job.original_path.stem or 'output'}_semantic.srt"
        pre_output = job.work_dir / output_name
        worker = SegmentationWorker(
            input_path=str(input_path),
            pre_output_path=str(pre_output),
            segmentation_config=seg_config,
        )
        worker.log.connect(self._append_log)
        worker.finished.connect(lambda path: self._on_segmentation_finished(job, Path(path)))
        worker.failed.connect(self._on_step_failed)
        worker.stopped.connect(self._on_step_failed)
        self._active_worker = worker
        worker.start()

    def _on_segmentation_finished(self, job: AutomationJob, output_path: Path) -> None:
        output_json = self._resolve_segmented_json_output(output_path)
        if not output_path.exists() or not output_json.exists():
            self._on_step_failed("语义分段未生成完整的中间产物。")
            return
        job.segmented_srt = output_path
        job.segmented_json = output_json
        self._active_worker = None
        self._run_next_step()

    def _run_translation(self, job: AutomationJob) -> None:
        input_path = self._get_translation_input(job)
        if not input_path or input_path.suffix.lower() != ".srt":
            self._on_step_failed("翻译输入必须为 SRT 文件。")
            return
        input_path = Path(input_path)

        provider = self.provider_combo.currentText().strip()
        model = self.model_combo.currentText().strip()
        api_key = self._get_api_key_for_provider(provider)
        if not provider or not model or not api_key:
            self._on_step_failed("翻译配置不完整。")
            return
        resume_enabled = bool(
            getattr(self, "resume_translate_switch", None)
            and self.resume_translate_switch.isChecked()
        )
        if resume_enabled:
            input_path = self._prepare_resume_input(job, input_path)

        self._settings = self._load_settings()
        self._settings["current_provider"] = provider
        self._settings["model"] = model
        api_keys = self._settings.get("api_keys", {})
        api_keys[provider] = api_key
        self._settings["api_keys"] = api_keys
        self._config_store.save_user_settings(self._settings)

        output_path = self._resolve_translation_output(job, prefer_existing=resume_enabled)
        job.translated_srt = output_path

        self._translation_active = True
        self._last_translation_count = 0
        worker = TranslationWorker(
            input_path=str(input_path),
            output_path=str(output_path),
            settings=self._settings,
            provider=provider,
            api_key=api_key,
            model=model,
            resume=resume_enabled,
            source_type="srt",
            segmentation_config=None,
            preprocessed_path=None,
        )
        worker.log.connect(self._append_log)
        worker.progress.connect(self._on_translation_progress)
        worker.progress_detail.connect(self._on_translation_progress_detail)
        worker.finished.connect(lambda _: self._on_translation_finished(job))
        worker.partial.connect(self._on_step_failed)
        worker.failed.connect(self._on_step_failed)
        worker.stopped.connect(self._on_step_failed)
        self._active_worker = worker
        worker.start()

    def _build_translation_output(self, job: AutomationJob) -> Path:
        return self._resolve_translation_output(job, prefer_existing=False)

    def _on_translation_progress(self, value: int) -> None:
        self._set_ring_status("翻译中", float(value))

    def _on_translation_progress_detail(self, translated: int, total: int) -> None:
        delta = max(translated - self._last_translation_count, 0)
        self._last_translation_count = translated
        self._add_wave_value(delta)

    def _on_translation_finished(self, job: AutomationJob) -> None:
        if not job.translated_srt or not job.translated_srt.exists():
            self._on_step_failed("翻译未生成输出文件。")
            return
        self._active_worker = None
        self._translation_active = False
        self._run_next_step()

    def _run_post_cleanup(self, job: AutomationJob) -> None:
        input_path = job.translated_srt
        if not input_path or input_path.suffix.lower() != ".srt":
            self._on_step_failed("译后清理仅支持 SRT 输入。")
            return
        worker = CleanupSubtitleWorker(
            "post_clean",
            [input_path],
        )
        worker.log.connect(self._append_log)
        worker.finished.connect(lambda: self._on_post_cleanup_finished(job, input_path))
        worker.failed.connect(self._on_step_failed)
        worker.stopped.connect(self._on_step_stopped)
        self._active_worker = worker
        worker.start()

    def _on_post_cleanup_finished(self, job: AutomationJob, input_path: Path) -> None:
        output = self._resolve_post_cleanup_output(input_path)
        if not output or not output.exists():
            self._on_step_failed("译后清理未生成预期输出文件。")
            return
        job.post_cleaned_srt = output
        self._active_worker = None
        self._run_next_step()

    def _run_merge(self) -> None:
        if len(self._jobs) < 2:
            self._on_step_failed("合并需要两个翻译结果。")
            return
        pass1 = self._jobs[0].post_cleaned_srt or self._jobs[0].translated_srt
        pass2 = self._jobs[1].post_cleaned_srt or self._jobs[1].translated_srt
        if not pass1 or not pass2:
            self._on_step_failed("合并缺少翻译结果。")
            return
        output_path = self._build_merge_output(pass1, pass2)
        strategy = self._current_merge_strategy_key()
        worker = MergeSubtitleWorker(
            pass1_path=str(pass1),
            pass2_path=str(pass2),
            output_path=str(output_path),
            strategy=strategy,
        )
        worker.finished.connect(lambda _: self._on_merge_finished(output_path))
        worker.failed.connect(self._on_step_failed)
        self._active_worker = worker
        worker.start()

    def _prepare_resume_input(self, job: AutomationJob, input_path: Path) -> Path:
        state_manager = TranslationStateManager(str(input_path))
        if input_path.exists() and state_manager.has_valid_state():
            valid, reason = state_manager.validate_source_file()
            if valid:
                self._append_log("检测到断点续译输入，继续使用原始输入文件。")
                return input_path
            self._append_log(f"断点续译不可用：{reason}，将重新生成预处理文件。")
            state_manager.cleanup()

        candidate = self._get_resume_cache_path(job, input_path)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        state_manager = TranslationStateManager(str(candidate))
        if candidate.exists() and state_manager.has_valid_state():
            valid, reason = state_manager.validate_source_file()
            if valid:
                self._append_log("检测到断点续译输入，继续使用缓存文件。")
                return candidate
            self._append_log(f"断点续译不可用：{reason}，将重新生成预处理文件。")
            state_manager.cleanup()
        if candidate.resolve() != input_path.resolve():
            try:
                shutil.copy2(input_path, candidate)
            except Exception as exc:
                self._append_log(f"断点续译准备失败，继续使用原输入：{exc}")
                return input_path
        return candidate

    def _build_merge_output(self, pass1: Path, pass2: Path) -> Path:
        def normalize_base(path: Path, fallback: str) -> str:
            stem = path.stem
            for suffix in ("_translated_post_cleaned", "_translated", "_post_cleaned"):
                if stem.endswith(suffix):
                    stem = stem[: -len(suffix)]
            return stem or fallback

        base1 = normalize_base(pass1, "pass1")
        base2 = normalize_base(pass2, "pass2")
        name = f"{base1}_{base2}_merged.srt"
        candidate = Path(self._output_dir) / name
        return self._unique_path(candidate)

    @staticmethod
    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        for idx in range(1, 100):
            candidate = path.with_name(f"{path.stem}_{idx}{path.suffix}")
            if not candidate.exists():
                return candidate
        return path

    def _on_merge_finished(self, output_path: Path) -> None:
        self._append_log(f"合并完成：{output_path}")
        self._active_worker = None
        self._run_next_step()

    def _on_step_failed(self, message: str) -> None:
        self._append_log(f"失败：{message}")
        self._translation_active = False
        self._set_ring_status("失败")
        show_error(self, "流程中断", message)
        self._reset_running_state()

    def _on_step_stopped(self) -> None:
        self._append_log("流程已停止。")
        self._translation_active = False
        self._set_ring_status("已停止")
        self._reset_running_state()

    def _finish_pipeline(self) -> None:
        self._translation_active = False
        self._set_ring_status("完成", 100.0)
        self._collect_final_outputs()
        self._cleanup_temp_files()
        self._reset_running_state()
        show_info(self, "完成", "脚本自动化流程已完成。")
        self._clear_files()

    def _collect_final_outputs(self) -> None:
        if not self._jobs:
            return
        output_dir = Path(self._output_dir) if self._output_dir else None
        if not output_dir or not output_dir.exists():
            return

        exported: list[Path] = []
        for job in self._jobs:
            source = self._resolve_job_final_artifact(job)
            if not source or not source.exists():
                continue

            target = output_dir / source.name
            try:
                if source.resolve() == target.resolve():
                    exported.append(target)
                    continue
            except Exception:
                pass

            target = self._unique_path(target)
            try:
                shutil.copy2(source, target)
                exported.append(target)
            except Exception as exc:
                self._append_log(f"结果导出失败：{source.name} -> {target.name} ({exc})")

        if exported:
            names = ", ".join(path.name for path in exported)
            self._append_log(f"结果已导出到输出目录：{names}")

    @staticmethod
    def _resolve_job_final_artifact(job: AutomationJob) -> Path | None:
        for candidate in (
            job.post_cleaned_srt,
            job.translated_srt,
            job.segmented_srt,
            job.optimized_srt,
            job.cleaned_json,
        ):
            if candidate and candidate.exists():
                return candidate
        return None

    def _cleanup_temp_files(self) -> None:
        if not self._temp_root:
            return
        if hasattr(self, "keep_intermediate_switch") and self.keep_intermediate_switch.isChecked():
            self._append_log("已启用过程保留，中间文件未清理。")
            self._temp_root = None
            return
        temp_root = self._temp_root
        output_dir = Path(self._output_dir) if self._output_dir else None
        if output_dir and temp_root.is_dir():
            if temp_root.name.startswith("Temp_Automation_") and output_dir in temp_root.parents:
                try:
                    shutil.rmtree(temp_root)
                    self._append_log("中间文件已清理。")
                except Exception as exc:
                    self._append_log(f"中间文件清理失败：{exc}")
        self._temp_root = None

    def _reset_running_state(self) -> None:
        self._set_running(False)
        self._steps = []
        self._active_worker = None

    def _set_running(self, running: bool) -> None:
        self._running = running
        segmentation_enabled = self._is_segmentation_feature_enabled()
        self.start_btn.setEnabled(not running)
        self.clear_files_btn.setEnabled(not running)
        self.add_file_btn.setEnabled(not running)
        self.drop_area.setEnabled(not running)
        self.clean_switch.setEnabled(not running)
        if hasattr(self, "optimize_switch"):
            self.optimize_switch.setEnabled(not running)
        if hasattr(self, "post_clean_switch"):
            self.post_clean_switch.setEnabled(not running)
        self.segment_switch.setEnabled(segmentation_enabled and not running)
        self.translate_switch.setEnabled(not running)
        self.merge_switch.setEnabled(not running)
        if hasattr(self, "merge_strategy_combo"):
            self.merge_strategy_combo.setEnabled(not running)
        self.provider_combo.setEnabled(not running)
        self.model_combo.setEnabled(not running)
        if hasattr(self, "resume_translate_switch"):
            self.resume_translate_switch.setEnabled(not running)
        self.batch_slider.setEnabled(not running)
        self.concurrency_slider.setEnabled(not running)
        self.seg_provider_combo.setEnabled(segmentation_enabled and not running)
        self.seg_model_combo.setEnabled(segmentation_enabled and not running)
        self.seg_temperature.setEnabled(segmentation_enabled and not running)
        self.seg_timeout.setEnabled(segmentation_enabled and not running)
        self.seg_max_retries.setEnabled(segmentation_enabled and not running)
        self.seg_batch_concurrency.setEnabled(segmentation_enabled and not running)
        self.seg_failure_fallback.setEnabled(segmentation_enabled and not running)
        if hasattr(self, "seg_refresh_btn"):
            self.seg_refresh_btn.setEnabled(segmentation_enabled and not running)
        if hasattr(self, "keep_intermediate_switch"):
            self.keep_intermediate_switch.setEnabled(not running)
        if hasattr(self, "resume_pipeline_switch"):
            self.resume_pipeline_switch.setEnabled(not running)
        self.output_dir_line.setEnabled(not running)
        self.output_dir_btn.setEnabled(not running)
        self._apply_action_button_styles()
        if not running:
            self._update_merge_switch_state()

    def _reset_status(self) -> None:
        if self._running:
            return
        self._last_step_value = 0
        self._last_translation_count = 0
        self._translation_active = False
        self._set_ring_status("就绪")

    def _set_ring_status(self, label: str, percent: float | None = None) -> None:
        if not hasattr(self, "ring"):
            return
        if percent is None:
            self.ring.setValue(0)
            self.ring.setFormat(label)
            return
        value = max(0.0, min(100.0, percent))
        self.ring.setValue(int(value * 100))
        self.ring.setFormat(f"{label}\n{value:.2f}%")

    def _add_wave_value(self, value: int) -> None:
        if hasattr(self, "waveform"):
            self.waveform.add_value(max(value, 0))

    def _append_log(self, message: str) -> None:
        self.log_message.emit(message)
        text = self._format_log(message)
        if self._logger:
            self._logger.info(text)

    @staticmethod
    def _format_log(message: str) -> str:
        try:
            payload = json.loads(message)
            if isinstance(payload, dict) and "message" in payload:
                timestamp = payload.get("timestamp", "")
                status = payload.get("status")
                detail = payload.get("message")
                if status:
                    return f"[{timestamp}] {detail} ({status})"
                return f"[{timestamp}] {detail}"
        except Exception:
            pass
        return str(message)

    def _build_segmentation_config(self) -> SegmentationConfig | None:
        provider = self.seg_provider_combo.currentText().strip() or self._settings.get("segmentation_provider")
        model = self.seg_model_combo.currentText().strip() or self._settings.get("segmentation_model")
        if not provider or not model:
            show_warning(self, "分段配置缺失", "请先设置分段接口和模型。")
            return None

        api_key = self._get_api_key_for_provider(provider)
        if not api_key:
            show_warning(self, "缺少分段 API Key", "请先在接口管理中配置 API Key。")
            return None

        provider_info = self._api_manager.get_provider_info(provider) or {}
        provider_endpoint = str(provider_info.get("base_url", "") or "").strip()
        legacy_endpoint = str(
            self._settings.get("segmentation_endpoint")
            or self._settings.get("endpoint")
            or ""
        ).strip()
        endpoint = provider_endpoint or legacy_endpoint
        if not endpoint:
            show_warning(self, "接口地址缺失", "分段接口缺少 base_url 配置。")
            return None

        timeout_seconds = max(30, int(self.seg_timeout.value()))
        max_retries = max(0, int(self.seg_max_retries.value()))
        batch_concurrency = max(1, int(self.seg_batch_concurrency.value()))
        temperature = float(self.seg_temperature.value())
        fallback_on_failure = bool(self.seg_failure_fallback.isChecked())

        self._settings["segmentation_provider"] = provider
        self._settings["segmentation_model"] = model
        self._settings["segmentation_temperature"] = temperature
        self._settings["segmentation_timeout"] = timeout_seconds
        self._settings["segmentation_max_retries"] = max_retries
        self._settings["segmentation_batch_concurrency"] = batch_concurrency
        self._settings["segmentation_fallback_on_failure"] = fallback_on_failure
        self._settings["segmentation_endpoint"] = endpoint
        self._config_store.save_user_settings(self._settings)

        return SegmentationConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            endpoint=endpoint,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            fallback_on_failure=fallback_on_failure,
            batch_concurrency=batch_concurrency,
            provider_limits=self._api_manager.get_provider_limits(provider),
            proxy_enabled=bool(self._settings.get("proxy_enabled", False)),
            proxy_address=self._settings.get("proxy_address", ""),
            debug_mode=bool(self._settings.get("debug_mode", 0)),
        )
