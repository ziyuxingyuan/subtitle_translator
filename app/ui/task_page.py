from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QTextEdit,
    QFileDialog,
    QProgressBar,
    QMessageBox,
    QCheckBox,
    QSpinBox,
    QDoubleSpinBox,
    QFrame,
    QStackedWidget,
    QSizePolicy,
    QProgressDialog,
)
from qfluentwidgets import (
    CardWidget,
    TitleLabel,
    BodyLabel,
    StrongBodyLabel,
    CaptionLabel,
    PushButton,
    ProgressRing,
    SwitchButton,
    ComboBox,
    Slider,
    IconWidget,
    FluentIcon,
    SingleDirectionScrollArea,
    FlowLayout,
)
from qfluentwidgets.common.config import qconfig
from qfluentwidgets.common.style_sheet import themeColor, setCustomStyleSheet
from app.services.theme_palette import build_theme_palette, color_to_hex
from modules.api_manager import APIManager
from modules.translation_state_manager import TranslationStateManager
from modules.segmentation_engine import SegmentationConfig
from modules.segmentation_workflow import (
    build_base_segments,
    parse_transcript_json,
    smart_segment_and_save,
)
from app.data.config_store import ConfigStore
from app.services.logging_setup import get_logger
from app.services.prompt_resolver import resolve_system_prompt
from app.services.segmentation_worker import SegmentationWorker
from app.services.translation_worker import TranslationWorker
from app.ui.path_utils import to_native_path
from app.ui.waveform_widget import WaveformWidget


class DragDropFileArea(QFrame):
    pathChanged = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("drag_drop_file_area")
        self.setAcceptDrops(True)
        self.setProperty("dragging", False)
        self._current_path = ""
        self.setMinimumHeight(220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 36, 24)
        layout.setSpacing(6)

        self.title_label = BodyLabel("拖拽字幕文件到此处", self)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tip_label = BodyLabel("支持 .srt / .json", self)
        self.tip_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.path_label = BodyLabel("未选择", self)
        self.path_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.path_label.setWordWrap(True)

        self._action_container = QWidget(self)
        self._action_layout = QHBoxLayout(self._action_container)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(8)

        layout.addStretch(1)
        layout.addWidget(self.title_label)
        layout.addWidget(self.tip_label)
        layout.addSpacing(8)
        layout.addWidget(self.path_label)
        layout.addSpacing(14)
        layout.addWidget(self._action_container)
        layout.addStretch(1)

        self._apply_style()
        qconfig.themeColorChanged.connect(self._apply_style)

    def _apply_style(self, *_: object) -> None:
        primary = themeColor()
        palette = build_theme_palette(primary)
        border_color = color_to_hex(palette.border_strong, with_alpha=True)
        drop_bg = color_to_hex(palette.drop_area_bg, with_alpha=True)
        active = QColor(primary)
        active.setAlphaF(0.08)
        self.setStyleSheet(
            f"""
            QFrame#drag_drop_file_area {{
                border: 2px dashed {border_color};
                border-radius: 12px;
                background-color: {drop_bg};
            }}
            QFrame#drag_drop_file_area[dragging="true"] {{
                border: 2px solid {primary.name()};
                background-color: {active.name(QColor.NameFormat.HexArgb)};
            }}
            """
        )

    def set_action_buttons(self, buttons: list[QWidget]) -> None:
        while self._action_layout.count():
            item = self._action_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        self._action_layout.addStretch(1)
        for btn in buttons:
            self._action_layout.addWidget(btn)
        self._action_layout.addStretch(1)

    def set_path(self, path: str, emit_signal: bool = True) -> None:
        normalized = to_native_path(path)
        self._current_path = normalized
        if normalized:
            display_path = normalized if len(normalized) <= 60 else f"...{normalized[-57:]}"
            self.path_label.setText(display_path)
            self.path_label.setToolTip(normalized)
            if emit_signal:
                self.pathChanged.emit(normalized)
        else:
            self.path_label.setText("未选择")
            self.path_label.setToolTip("")

    def dragEnterEvent(self, event) -> None:
        if self._find_valid_path(event.mimeData().urls()):
            event.acceptProposedAction()
            self._set_dragging(True)
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_dragging(False)
        event.accept()

    def dropEvent(self, event) -> None:
        self._set_dragging(False)
        if not event.mimeData().hasUrls():
            return
        path = self._find_valid_path(event.mimeData().urls())
        if path:
            self.set_path(path)

    def _set_dragging(self, dragging: bool) -> None:
        if self.property("dragging") == dragging:
            return
        self.setProperty("dragging", dragging)
        self.style().unpolish(self)
        self.style().polish(self)

    @staticmethod
    def _find_valid_path(urls) -> str:
        for url in urls:
            if not url.isLocalFile():
                continue
            path = to_native_path(url.toLocalFile())
            if not path:
                continue
            suffix = Path(path).suffix.lower()
            if suffix in (".srt", ".json") and Path(path).exists():
                return path
        return ""


class StartTranslationPage(QWidget):
    proceedRequested = pyqtSignal(str, str)
    outputDirChanged = pyqtSignal(str)
    outputPathChanged = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_path = ""
        self._output_dir = ""
        self._output_path = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        card = CardWidget(self)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(8)

        card_layout.addWidget(TitleLabel("开始翻译", card))
        desc_label = BodyLabel("拖拽字幕文件或点击选择后进入翻译设置。", card)
        desc_label.setWordWrap(True)
        card_layout.addWidget(desc_label)

        self.drop_area = DragDropFileArea(card)
        card_layout.addWidget(self.drop_area, 1)

        self.select_btn = PushButton("选择文件")
        self.clear_btn = PushButton("清空选择")
        self.next_btn = PushButton("进入翻译设置")
        self.next_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        self.drop_area.set_action_buttons([self.select_btn, self.clear_btn, self.next_btn])

        root.addWidget(card)

        output_card = CardWidget(self)
        output_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        output_layout = QVBoxLayout(output_card)
        output_layout.setContentsMargins(16, 16, 16, 16)
        output_layout.setSpacing(8)

        output_layout.addWidget(TitleLabel("翻译输出文件", output_card))
        self.output_path_input = QLineEdit(output_card)
        self.output_path_input.setPlaceholderText("当前输出文件为 未设置")
        self.output_path_input.setFixedHeight(32)
        output_layout.addWidget(self.output_path_input)

        output_action = QHBoxLayout()
        output_action.addStretch(1)
        self.output_btn = PushButton("选择输出文件")
        output_action.addWidget(self.output_btn)
        output_layout.addLayout(output_action)

        root.addWidget(output_card)
        root.addStretch(1)

        self.drop_area.pathChanged.connect(self._on_path_changed)
        self.select_btn.clicked.connect(self._select_file)
        self.clear_btn.clicked.connect(self._clear_selection)
        self.output_btn.clicked.connect(self._select_output_file)
        self.output_path_input.editingFinished.connect(self._on_output_path_edited)
        self.next_btn.clicked.connect(self._request_proceed)
        self._update_button_style()
        qconfig.themeColorChanged.connect(self._update_button_style)

    def set_path(self, path: str) -> None:
        normalized = to_native_path(path)
        self._current_path = normalized
        self.drop_area.set_path(normalized, emit_signal=False)
        self.next_btn.setEnabled(bool(normalized))
        self.clear_btn.setEnabled(bool(normalized))
        if normalized and not self._output_path:
            base_name = Path(normalized).stem or "output"
            self.set_output_path(str(Path(normalized).parent / f"{base_name}_translated.srt"), emit_signal=True)
        elif normalized and self._output_path:
            self.set_output_path(self._output_path, emit_signal=True)

    def set_output_dir(self, output_dir: str, emit_signal: bool = False) -> None:
        self._output_dir = to_native_path(output_dir) if output_dir else ""
        if self._current_path and self._output_dir:
            base_name = Path(self._current_path).stem or "output"
            self.set_output_path(str(Path(self._output_dir) / f"{base_name}_translated.srt"), emit_signal=emit_signal)

    def _resolve_output_path(self, output_path: str) -> str:
        candidate = to_native_path(output_path)
        if not candidate:
            return ""
        path = Path(candidate)
        if path.suffix.lower() != ".srt":
            path = path.with_suffix(".srt")
        if path.parent == Path("."):
            if self._current_path:
                path = Path(self._current_path).parent / path.name
            elif self._output_dir:
                path = Path(self._output_dir) / path.name
        return to_native_path(str(path))

    def set_output_path(self, output_path: str, emit_signal: bool = False) -> None:
        resolved = self._resolve_output_path(output_path)
        self._output_path = resolved
        self.output_path_input.setText(resolved)
        self.output_path_input.setToolTip(resolved)
        if resolved:
            self._output_dir = to_native_path(str(Path(resolved).parent))
        if emit_signal:
            self.outputPathChanged.emit(self._output_path)
            if self._output_dir:
                self.outputDirChanged.emit(self._output_dir)

    def _on_path_changed(self, path: str) -> None:
        self.set_path(path)

    @staticmethod
    def _scale_color(color: QColor, factor: float) -> QColor:
        """缩放颜色亮度"""
        return QColor(
            max(0, min(255, int(color.red() * factor))),
            max(0, min(255, int(color.green() * factor))),
            max(0, min(255, int(color.blue() * factor))),
        )

    def _update_button_style(self, *_: object) -> None:
        """更新按钮样式以匹配当前主题色"""
        base = QColor(themeColor())
        hover = self._scale_color(base, 0.9)
        palette = build_theme_palette(base)
        neutral_disabled_bg = "#ECEFF3"
        neutral_disabled_border = "#D4DAE2"
        neutral_disabled_text = "#99A1AB"
        secondary_bg = color_to_hex(palette.surface_2)
        secondary_hover = color_to_hex(palette.surface_3)
        secondary_border = color_to_hex(palette.border_strong, with_alpha=True)
        primary_style = (
            "PushButton, QPushButton {"
            f"background-color: {base.name()};"
            f"border: 1px solid {base.name()};"
            "color: #FFFFFF;"
            "border-radius: 6px;"
            "padding: 6px 16px;"
            "}"
            "PushButton:hover, QPushButton:hover {"
            f"background-color: {hover.name()};"
            f"border-color: {hover.name()};"
            "}"
            "PushButton:disabled, QPushButton:disabled {"
            f"background-color: {neutral_disabled_bg};"
            f"border-color: {neutral_disabled_border};"
            f"color: {neutral_disabled_text};"
            "}"
        )
        secondary_style = (
            "PushButton, QPushButton {"
            f"background-color: {secondary_bg};"
            f"border: 1px solid {secondary_border};"
            "color: #1F2937;"
            "border-radius: 6px;"
            "padding: 6px 16px;"
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
        for attr in ("next_btn", "select_btn"):
            button = getattr(self, attr, None)
            if button is not None:
                setCustomStyleSheet(button, primary_style, primary_style)
        for attr in ("clear_btn", "output_btn"):
            button = getattr(self, attr, None)
            if button is not None:
                setCustomStyleSheet(button, secondary_style, secondary_style)

    def _select_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择字幕文件",
            "",
            "字幕文件 (*.srt *.json)",
        )
        if not path:
            return
        self.set_path(path)

    def _select_output_file(self) -> None:
        start_path = self._output_path
        if not start_path and self._current_path:
            base_name = Path(self._current_path).stem or "output"
            start_path = str(Path(self._current_path).parent / f"{base_name}_translated.srt")
        selected, _ = QFileDialog.getSaveFileName(self, "选择输出文件", start_path, "SRT 文件 (*.srt)")
        if not selected:
            return
        self.set_output_path(selected, emit_signal=True)

    def _on_output_path_edited(self) -> None:
        self.set_output_path(self.output_path_input.text(), emit_signal=True)

    def _clear_selection(self) -> None:
        self._current_path = ""
        self._output_path = ""
        self._output_dir = ""
        self.drop_area.set_path("", emit_signal=False)
        self.output_path_input.setText("")
        self.output_path_input.setToolTip("")
        self.next_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)

    def _request_proceed(self) -> None:
        if not self._current_path:
            return
        if not Path(self._current_path).exists():
            QMessageBox.warning(self, "文件不存在", "输入文件不存在或无法访问。")
            return
        self.proceedRequested.emit(self._current_path, self._output_path)


class DashboardCard(CardWidget):
    def __init__(self, parent: QWidget, title: str, value: str, unit: str) -> None:
        super().__init__(parent)
        self.setMinimumWidth(140)
        self.setFixedHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(6)

        self.title_label = BodyLabel(title, self)
        self.title_label.setWordWrap(True)
        root.addWidget(self.title_label)

        value_row = QHBoxLayout()
        self.value_label = TitleLabel(value, self)
        value_row.addWidget(self.value_label, 1)
        self.unit_label = BodyLabel(unit, self)
        self.unit_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value_row.addWidget(self.unit_label)
        root.addLayout(value_row)
        root.addStretch(1)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class TaskPage(QWidget):
    translation_active_changed = pyqtSignal(bool)
    provider_changed = pyqtSignal(str)
    log_message = pyqtSignal(str)
    log_cleared = pyqtSignal()
    debug_log_cleared = pyqtSignal()
    progress_value_changed = pyqtSignal(int)
    progress_detail_changed = pyqtSignal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("task_page")

        self._config_store = ConfigStore()
        self._settings = self._load_settings()
        self._api_manager = APIManager()
        self._worker: TranslationWorker | None = None
        self._seg_worker: SegmentationWorker | None = None
        self._seg_progress: QProgressDialog | None = None
        self._logger = get_logger("app")
        self._input_type = "SRT"
        self._preprocessed_path: Path | None = None
        self._last_progress_value = 0
        self._last_task_status = "idle"

        self._build_ui()
        self.log_cleared.connect(self.log_view.clear)
        self.progress_value_changed.connect(self.progress.setValue)
        self.progress_detail_changed.connect(self._update_progress_label)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._page_stack = QStackedWidget(self)
        root.addWidget(self._page_stack)

        self._start_page = StartTranslationPage(self)
        self._start_page.proceedRequested.connect(self._enter_task_page)
        self._start_page.outputDirChanged.connect(self._on_output_dir_changed)
        self._start_page.set_output_dir(self._get_saved_output_dir(), emit_signal=False)

        self._task_page = QWidget()
        self._build_task_page(self._task_page)

        self._page_stack.addWidget(self._start_page)
        self._page_stack.addWidget(self._task_page)
        self._page_stack.setCurrentWidget(self._start_page)

        self._load_translation_provider_model()
        self._apply_translation_settings()
        self._load_segmentation_provider_model()
        self._apply_segmentation_settings()
        self._refresh_input_type()
        self.apply_segmentation_feature_state(None)
        self.translation_active_changed.connect(self._on_translation_state_changed)
        qconfig.themeColorChanged.connect(self._apply_action_button_styles)
        self._apply_action_button_styles()
        self._on_translation_state_changed(self.is_translating())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_stats_spacing()

    def _update_stats_spacing(self) -> None:
        if not hasattr(self, "stats_container") or not hasattr(self, "stats_layout"):
            return
        if not hasattr(self, "line_card"):
            return
        container_width = self.stats_container.width()
        if container_width <= 0:
            return
        margins = self.stats_layout.contentsMargins()
        available = container_width - margins.left() - margins.right()
        card_width = self.line_card.width() or 204
        target_cols = 4
        if available >= card_width * target_cols:
            spacing = int((available - card_width * target_cols) / (target_cols - 1))
            self.stats_layout.setHorizontalSpacing(max(8, spacing))
        else:
            self.stats_layout.setHorizontalSpacing(8)

    def _build_task_page(self, page: QWidget) -> None:
        page.setStyleSheet("background: transparent;")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll_area = SingleDirectionScrollArea(page, Qt.Orientation.Vertical)
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.enableTransparentBackground()
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        scroll_area.setWidget(content)
        root.addWidget(scroll_area, 1)

        self.input_line = QLineEdit(page)
        self.output_line = QLineEdit(page)
        self.input_line.textChanged.connect(self._on_input_text_changed)
        self.output_line.textChanged.connect(self._on_output_text_changed)

        self.input_type_combo = QComboBox(page)
        self.input_type_combo.addItems(["SRT", "JSON"])
        self.input_type_combo.setEnabled(False)

        self.preprocess_label = QLabel("未生成", page)
        for widget in (
            self.input_line,
            self.output_line,
            self.input_type_combo,
            self.preprocess_label,
        ):
            widget.setVisible(False)

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
        head_layout.addStretch(1)
        head_layout.addWidget(waveform_container)
        head_layout.addStretch(1)
        layout.addWidget(head_container)

        stats_container = QWidget(content)
        stats_layout = FlowLayout(stats_container, needAni=False)
        stats_layout.setSpacing(8)
        stats_layout.setContentsMargins(0, 0, 0, 0)

        self.line_card = DashboardCard(stats_container, "翻译行数", "0", "行")
        self.remaining_line_card = DashboardCard(stats_container, "剩余行数", "0", "行")
        self.token_card = DashboardCard(stats_container, "累计消耗", "-", "token")
        self.task_card = DashboardCard(
            stats_container,
            "并行任务",
            "0",
            "个",
        )

        stats_layout.addWidget(self.line_card)
        stats_layout.addWidget(self.remaining_line_card)
        stats_layout.addWidget(self.token_card)
        stats_layout.addWidget(self.task_card)
        layout.addWidget(stats_container)

        segment_card, segment_layout = self._create_card(
            "语义分段",
            "分段配置仅影响语义分段，不影响翻译模型。",
        )
        self.segment_card = segment_card
        segment_toggle_row = QHBoxLayout()
        segment_toggle_row.addStretch(1)
        self.seg_enable_switch = SwitchButton(segment_card)
        self.seg_enable_switch.setOnText("启用")
        self.seg_enable_switch.setOffText("停用")
        self.seg_enable_switch.setChecked(False)
        segment_toggle_row.addWidget(self.seg_enable_switch)
        segment_layout.addLayout(segment_toggle_row)

        self.segment_body = QWidget(segment_card)
        segment_body_layout = QVBoxLayout(self.segment_body)
        segment_body_layout.setContentsMargins(0, 0, 0, 0)
        segment_body_layout.setSpacing(8)

        segment_desc = BodyLabel("分段配置仅影响语义分段，不影响翻译模型。", self.segment_body)
        segment_desc.setWordWrap(True)
        segment_desc.setStyleSheet("color: #6B6B6B; font-size: 12px;")
        segment_body_layout.addWidget(segment_desc)
        segment_body_layout.addSpacing(6)

        def add_segment_row(label_text: str, controls: QWidget, add_divider: bool = True) -> None:
            row_container = QWidget(self.segment_body)
            row_layout = QHBoxLayout(row_container)
            row_layout.setContentsMargins(0, 8, 0, 8)
            row_layout.setSpacing(8)

            row_label = BodyLabel(label_text, row_container)
            row_label.setFixedWidth(140)
            row_label.setStyleSheet("font-size: 13px;")
            row_layout.addWidget(row_label)
            row_layout.addStretch(1)

            control_container = QWidget(row_container)
            control_layout = QHBoxLayout(control_container)
            control_layout.setContentsMargins(0, 0, 0, 0)
            control_layout.setSpacing(8)
            control_layout.addWidget(controls)
            row_layout.addWidget(control_container, 0, Qt.AlignmentFlag.AlignRight)

            segment_body_layout.addWidget(row_container)
            if add_divider:
                divider = QFrame(self.segment_body)
                divider.setFixedHeight(1)
                divider.setStyleSheet("background-color: #E5E5E5;")
                segment_body_layout.addWidget(divider)

        self.seg_provider_combo = QComboBox()
        self.seg_provider_combo.currentTextChanged.connect(self._on_segmentation_provider_changed)
        provider_controls = QWidget(self.segment_body)
        provider_layout = QHBoxLayout(provider_controls)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(8)
        provider_layout.addWidget(self.seg_provider_combo, 1)
        refresh_btn = PushButton("刷新")
        refresh_btn.clicked.connect(self._reload_segmentation_providers)
        provider_layout.addWidget(refresh_btn)
        add_segment_row("分段接口", provider_controls)

        self.seg_model_combo = QComboBox()
        add_segment_row("分段模型", self.seg_model_combo)

        self.seg_temperature = QDoubleSpinBox()
        self.seg_temperature.setRange(0.0, 1.0)
        self.seg_temperature.setSingleStep(0.1)
        self.seg_temperature.setDecimals(2)
        add_segment_row("分段温度", self.seg_temperature)

        self.seg_timeout = QSpinBox()
        self.seg_timeout.setRange(30, 600)
        self.seg_timeout.setSingleStep(30)
        add_segment_row("超时（秒）", self.seg_timeout)

        self.seg_max_retries = QSpinBox()
        self.seg_max_retries.setRange(0, 5)
        add_segment_row("最大重试", self.seg_max_retries)

        self.seg_batch_concurrency = QSpinBox()
        self.seg_batch_concurrency.setRange(1, 5)
        add_segment_row("同时发送批次数", self.seg_batch_concurrency)

        self.seg_failure_fallback = QCheckBox()
        self.seg_failure_fallback.setText("")
        add_segment_row("失败回退", self.seg_failure_fallback)

        actions_container = QWidget(self.segment_body)
        actions_layout = QHBoxLayout(actions_container)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)
        self.save_segmentation_btn = PushButton("保存分段配置")
        self.save_segmentation_btn.clicked.connect(self._save_segmentation_settings)
        self.smart_segment_btn = PushButton("智能分割并生成预处理SRT")
        self.smart_segment_btn.clicked.connect(self._run_smart_segmentation)
        actions_layout.addWidget(self.save_segmentation_btn)
        actions_layout.addWidget(self.smart_segment_btn)
        actions_layout.addStretch(1)
        add_segment_row("操作", actions_container, add_divider=False)

        segment_note = BodyLabel("仅 JSON 输入支持智能分割，翻译会自动应用分段结果。", self.segment_body)
        segment_note.setWordWrap(True)
        segment_note.setStyleSheet("color: #6B6B6B; font-size: 12px;")
        segment_body_layout.addSpacing(6)
        segment_body_layout.addWidget(segment_note)
        self.segment_body.setVisible(False)
        self.seg_enable_switch.checkedChanged.connect(self._toggle_segmentation_panel)
        segment_layout.addWidget(self.segment_body)
        layout.addWidget(segment_card)

        translation_card = CardWidget(content)
        translation_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        translation_layout = QVBoxLayout(translation_card)
        translation_layout.setContentsMargins(16, 16, 16, 16)
        translation_layout.setSpacing(8)

        translation_layout.addWidget(TitleLabel("开始翻译", translation_card))

        translation_body = QWidget(translation_card)
        translation_body_layout = QVBoxLayout(translation_body)
        translation_body_layout.setContentsMargins(0, 0, 0, 0)
        translation_body_layout.setSpacing(0)

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

            if add_divider:
                divider = QFrame(translation_body)
                divider.setFixedHeight(1)
                divider.setStyleSheet("background-color: #E5E5E5;")
                translation_body_layout.addWidget(divider)

        self.provider_combo = ComboBox()
        self.provider_combo.setMinimumWidth(240)
        self.provider_combo.currentTextChanged.connect(self._on_translation_provider_changed)
        provider_controls = QWidget(translation_body)
        provider_layout = QHBoxLayout(provider_controls)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(8)
        provider_layout.addWidget(self.provider_combo, 1)
        self.translation_refresh_btn = PushButton("刷新")
        self.translation_refresh_btn.clicked.connect(self._reload_translation_providers)
        provider_layout.addWidget(self.translation_refresh_btn)
        add_translation_row(
            FluentIcon.CONNECT,
            "翻译接口",
            "选择翻译服务提供商",
            provider_controls,
        )

        self.model_combo = ComboBox()
        self.model_combo.setMinimumWidth(240)
        add_translation_row(
            FluentIcon.ROBOT,
            "翻译模型",
            "选择要使用的模型",
            self.model_combo,
        )

        self.api_key_line = QLineEdit()
        self.api_key_line.setEchoMode(QLineEdit.EchoMode.Password)
        add_translation_row(
            FluentIcon.CONNECT,
            "API 密钥",
            "当前翻译接口对应的密钥",
            self.api_key_line,
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
        batch_value = int(self._settings.get("batch_size", 120))
        batch_value = max(10, min(300, batch_value))
        batch_value = int(round(batch_value / 10) * 10)
        self.batch_slider.setValue(batch_value)
        self.batch_value_label.setText(str(batch_value))
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
        concurrency_value = int(self._settings.get("concurrency", 2))
        concurrency_value = max(1, min(10, concurrency_value))
        self.concurrency_slider.setValue(concurrency_value)
        self.concurrency_value_label.setText(str(concurrency_value))
        self.concurrency_slider.valueChanged.connect(self._on_concurrency_slider_changed)
        add_translation_row(
            FluentIcon.SPEED_HIGH,
            "并发数",
            "同时进行的翻译任务数",
            concurrency_container,
            add_divider=False,
        )

        translation_layout.addWidget(translation_body)
        layout.addWidget(translation_card)

        progress_card, progress_layout = self._create_card("翻译进度")
        self.progress = QProgressBar(progress_card)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress_label = BodyLabel("0 / 0 (0.0%)", progress_card)
        progress_layout.addWidget(self.progress)
        progress_layout.addWidget(self.progress_label)
        layout.addWidget(progress_card)

        log_card, log_layout = self._create_card("运行日志")
        self.log_view = QTextEdit(log_card)
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(200)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_card)

        layout.addStretch(1)

        bottom_bar = CardWidget(page)
        bottom_bar.setObjectName("task_bottom_bar")
        bottom_bar.setFixedHeight(60)
        bottom_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(24, 6, 24, 6)
        bottom_layout.setSpacing(12)

        self.back_btn = PushButton("返回")
        self.back_btn.setFixedHeight(32)
        self.back_btn.clicked.connect(self._return_to_start_page)
        bottom_layout.addWidget(self.back_btn)
        bottom_layout.addStretch(1)

        self.start_translation_btn = PushButton("开始翻译")
        self.start_translation_btn.setFixedHeight(32)
        self.start_translation_btn.setMinimumWidth(120)
        self.start_translation_btn.clicked.connect(self._start_translation_from_ui)
        self.stop_translation_btn = PushButton("停止翻译")
        self.stop_translation_btn.setFixedHeight(32)
        self.stop_translation_btn.setMinimumWidth(120)
        self.stop_translation_btn.clicked.connect(self._stop_translation_from_ui)
        bottom_layout.addWidget(self.start_translation_btn)
        bottom_layout.addWidget(self.stop_translation_btn)

        root.addWidget(bottom_bar)

    @staticmethod
    def _scale_color(color: QColor, factor: float) -> QColor:
        return QColor(
            max(0, min(255, int(color.red() * factor))),
            max(0, min(255, int(color.green() * factor))),
            max(0, min(255, int(color.blue() * factor))),
        )

    def _apply_action_button_styles(self, *_: object) -> None:
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
            "start_translation_btn": primary_qss,
            "stop_translation_btn": secondary_qss,
            "back_btn": secondary_qss,
            "translation_refresh_btn": secondary_qss,
            "save_segmentation_btn": secondary_qss,
            "smart_segment_btn": secondary_qss,
        }
        for attr, qss in button_styles.items():
            button = getattr(self, attr, None)
            if button is not None:
                setCustomStyleSheet(button, qss, qss)

    def _create_card(self, title: str, description: str | None = None) -> tuple[CardWidget, QVBoxLayout]:
        card = CardWidget(self)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        layout.addWidget(TitleLabel(title, card))
        if description:
            desc_label = BodyLabel(description, card)
            desc_label.setWordWrap(True)
            layout.addWidget(desc_label)
        return card, layout

    def _enter_task_page(self, input_path: str, output_hint: str) -> None:
        input_path = to_native_path(input_path)
        output_hint = to_native_path(output_hint) if output_hint else ""
        if not input_path:
            QMessageBox.warning(self, "缺少输入", "请选择输入文件。")
            return
        if not Path(input_path).exists():
            QMessageBox.warning(self, "文件不存在", "输入文件不存在或无法访问。")
            return
        resolved_output_path = ""
        if output_hint and Path(output_hint).suffix.lower() == ".srt":
            resolved_output_path = output_hint
        if not resolved_output_path:
            resolved_output_dir = self._resolve_output_dir(Path(input_path), output_hint)
            resolved_output_path = self._compose_output_path(Path(input_path), resolved_output_dir)
        previous_input = self.input_line.text().strip()
        self.input_line.setText(input_path)
        if not self.output_line.text() or previous_input != input_path:
            self.output_line.setText(resolved_output_path)
        self._remember_output_dir(self._output_dir_from_path(resolved_output_path))
        self._clear_preprocessed_path()
        self._refresh_input_type()
        self._update_segmentation_controls()
        self._update_translation_controls()
        self._page_stack.setCurrentWidget(self._task_page)

    def _return_to_start_page(self) -> None:
        current_input = self.input_line.text().strip()
        if current_input:
            self._start_page.set_path(current_input)
        output_dir = self._output_dir_from_path(self.output_line.text().strip())
        if output_dir:
            self._start_page.set_output_dir(output_dir, emit_signal=False)
        else:
            self._start_page.set_output_dir(self._get_saved_output_dir(), emit_signal=False)
        self._page_stack.setCurrentWidget(self._start_page)

    def _pick_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择字幕文件",
            "",
            "字幕文件 (*.srt *.json)",
        )
        if not path:
            return
        path = to_native_path(path)
        self.input_line.setText(path)
        if not self.output_line.text():
            self.output_line.setText(self._compose_output_path(Path(path)))
        self._remember_output_dir(self._output_dir_from_path(self.output_line.text().strip()))
        self._refresh_input_type()
        self._clear_preprocessed_path()
        self._update_segmentation_controls()
        self._update_translation_controls()

    def _pick_output(self) -> None:
        start_dir = self._get_saved_output_dir()
        if not start_dir and self.output_line.text().strip():
            start_dir = self._output_dir_from_path(self.output_line.text().strip())
        path, _ = QFileDialog.getSaveFileName(self, "保存字幕文件", start_dir, "SRT 文件 (*.srt)")
        if not path:
            return
        path = to_native_path(path)
        self.output_line.setText(path)
        self._remember_output_dir(self._output_dir_from_path(path))
        self._clear_preprocessed_path()

    def start_translation(self, provider: str, model: str, api_key: str) -> bool:
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "翻译进行中", "当前已有翻译任务在运行。")
            return False

        input_path = self.input_line.text().strip()
        output_path = self.output_line.text().strip()

        if not input_path:
            QMessageBox.warning(self, "缺少输入", "请选择输入文件。")
            return False
        if not Path(input_path).exists():
            QMessageBox.warning(self, "文件不存在", "输入文件不存在或无法访问。")
            return False
        if self._detect_input_type(input_path) == "JSON":
            QMessageBox.information(self, "不支持", "JSON 输入仅支持语义分段，请先生成预处理 SRT。")
            return False
        if not output_path:
            QMessageBox.warning(self, "缺少输出", "请选择输出路径。")
            return False
        if not provider or not model:
            QMessageBox.warning(self, "缺少接口", "请选择接口和模型。")
            return False
        if not api_key:
            QMessageBox.warning(self, "缺少 API 密钥", "请先在接口管理中配置 API 密钥。")
            return False

        self._settings = self._load_settings()
        self._persist_settings(provider, model, api_key)
        self._persist_segmentation_settings()

        input_type = self._detect_input_type(input_path)
        self._input_type = input_type
        self._refresh_input_type()
        self._update_segmentation_controls()

        segmentation_config = None
        preprocessed_path: Path | None = None
        resume_source = input_path

        if input_type == "JSON":
            segmentation_config = self._build_segmentation_config(api_key)
            if segmentation_config is None:
                return False
            preprocessed_path = self._preprocessed_path or self._get_preprocessed_path(
                Path(input_path), Path(output_path)
            )
            if preprocessed_path:
                self._set_preprocessed_path(preprocessed_path)
                resume_source = str(preprocessed_path)

        resume = self._should_resume(resume_source)

        self.progress_value_changed.emit(0)
        self.progress_detail_changed.emit(0, 0)
        self.log_cleared.emit()
        self.debug_log_cleared.emit()
        self._append_log("开始翻译...")

        self._logger.info("开始翻译: %s", input_path)

        self._worker = TranslationWorker(
            input_path=input_path,
            output_path=output_path,
            settings=self._settings,
            provider=provider,
            api_key=api_key,
            model=model,
            resume=resume,
            source_type=input_type.lower(),
            segmentation_config=segmentation_config,
            preprocessed_path=str(preprocessed_path) if preprocessed_path else None,
        )
        self._worker.log.connect(self._append_log)
        self._worker.progress.connect(self._on_progress_value)
        self._worker.progress_detail.connect(self._on_progress_detail)
        self._worker.stats_updated.connect(self._update_runtime_stats)
        self._worker.finished.connect(self._on_finished)
        self._worker.partial.connect(self._on_partial)
        self._worker.stopped.connect(self._on_stopped)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

        self.translation_active_changed.emit(True)
        return True

    def stop_translation(self) -> bool:
        if self._worker and self._worker.isRunning():
            self._append_log("已请求停止...")
            self._worker.stop()
            return True
        return False

    def is_translating(self) -> bool:
        return bool(self._worker and self._worker.isRunning())

    def _on_finished(self, output_path: str) -> None:
        self._append_log(f"完成: {output_path}")
        self._last_task_status = "completed"
        self._worker = None
        self.translation_active_changed.emit(False)

    def _on_failed(self, message: str) -> None:
        self._append_log(f"失败: {message}")
        QMessageBox.critical(self, "翻译失败", message)
        self._last_task_status = "failed"
        self._worker = None
        self.translation_active_changed.emit(False)

    def _on_partial(self, message: str) -> None:
        self._append_log(message)
        QMessageBox.information(self, "翻译未完成", message)
        self._last_task_status = "partial"
        self._worker = None
        self.translation_active_changed.emit(False)

    def _on_stopped(self, message: str) -> None:
        self._append_log(message)
        QMessageBox.information(self, "翻译已停止", message)
        self._last_task_status = "stopped"
        self._worker = None
        self.translation_active_changed.emit(False)

    def _append_log(self, message: str) -> None:
        raw = str(message)
        self.log_view.append(self._extract_log_message(raw))
        self.log_message.emit(raw)

    def _on_progress_value(self, value: int) -> None:
        self.progress_value_changed.emit(value)

    def _on_progress_detail(self, current: int, total: int) -> None:
        self.progress_detail_changed.emit(current, total)

    @staticmethod
    def _extract_log_message(raw: str) -> str:
        text = str(raw)
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                payload = json.loads(stripped)
            except Exception:
                return text
            if isinstance(payload, dict):
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    return message
        return text

    def _update_progress_label(self, current: int, total: int) -> None:
        total_safe = total if total > 0 else 1
        percent = (current / total_safe) * 100
        self.progress_label.setText(f"{current} / {total} ({percent:.1f}%)")
        self._update_dashboard_cards(current, total)
        self._update_head_visuals(current, total)

    def _update_dashboard_cards(self, current: int, total: int) -> None:
        if not hasattr(self, "line_card"):
            return
        self.line_card.set_value(str(current))
        remaining = max(total - current, 0)
        self.remaining_line_card.set_value(str(remaining))

    def _update_runtime_stats(self, total_tokens: int, active_workers: int) -> None:
        if hasattr(self, "token_card"):
            self.token_card.set_value(str(total_tokens))
        if hasattr(self, "task_card"):
            self.task_card.set_value(str(max(active_workers, 0)))

    def _update_head_visuals(self, current: int, total: int) -> None:
        if not hasattr(self, "ring"):
            return
        total_safe = total if total > 0 else 1
        ratio = current / total_safe
        self.ring.setValue(int(ratio * 10000))
        self.ring.setFormat(f"翻译中\n{ratio * 100:.2f}%")
        if hasattr(self, "waveform"):
            delta = max(current - self._last_progress_value, 0)
            self._last_progress_value = current
            self.waveform.add_value(delta)

    def _on_batch_slider_changed(self, value: int) -> None:
        normalized = int(round(value / 10.0) * 10)
        normalized = max(10, min(300, normalized))
        if normalized != value:
            self.batch_slider.blockSignals(True)
            self.batch_slider.setValue(normalized)
            self.batch_slider.blockSignals(False)
        self.batch_value_label.setText(str(normalized))
        self._settings["batch_size"] = normalized

    def _on_concurrency_slider_changed(self, value: int) -> None:
        normalized = max(1, min(10, int(value)))
        if normalized != value:
            self.concurrency_slider.blockSignals(True)
            self.concurrency_slider.setValue(normalized)
            self.concurrency_slider.blockSignals(False)
        self.concurrency_value_label.setText(str(normalized))
        self._settings["concurrency"] = normalized
        if hasattr(self, "task_card") and self.is_translating():
            self.task_card.set_value(str(normalized))

    def _on_input_text_changed(self) -> None:
        self._refresh_input_type()
        self._clear_preprocessed_path()
        self._update_segmentation_controls()
        self._update_translation_controls()

    def _on_output_text_changed(self) -> None:
        if self._preprocessed_path:
            self._clear_preprocessed_path()
        self._remember_output_dir(self._output_dir_from_path(self.output_line.text().strip()))
        self._update_translation_controls()

    def _refresh_input_type(self) -> None:
        input_path = self.input_line.text().strip()
        input_type = self._detect_input_type(input_path)
        self._input_type = input_type
        index = self.input_type_combo.findText(input_type)
        if index >= 0:
            self.input_type_combo.setCurrentIndex(index)

    def _detect_input_type(self, input_path: str) -> str:
        if not input_path:
            return self._input_type or "SRT"
        suffix = Path(input_path).suffix.lower()
        return "JSON" if suffix == ".json" else "SRT"

    def _suggest_output_path(self, input_path: Path) -> str:
        return self._compose_output_path(input_path)

    def _clear_preprocessed_path(self) -> None:
        self._preprocessed_path = None
        self._update_preprocess_label()

    def _set_preprocessed_path(self, path: Path) -> None:
        self._preprocessed_path = path
        self._update_preprocess_label()

    def _update_preprocess_label(self) -> None:
        if self._preprocessed_path:
            self.preprocess_label.setText(str(self._preprocessed_path))
        else:
            self.preprocess_label.setText("未生成")

    def _resolve_output_dir(self, input_path: Path, preferred_dir: str | None = None) -> str:
        candidates = []
        if preferred_dir:
            candidates.append(preferred_dir)
        saved = self._get_saved_output_dir()
        if saved:
            candidates.append(saved)
        candidates.append(str(input_path.parent))
        for item in candidates:
            if item and Path(item).is_dir():
                return item
        return str(input_path.parent)

    def _compose_output_path(self, input_path: Path, output_dir: str | None = None) -> str:
        base_name = input_path.stem or "output"
        resolved_dir = self._resolve_output_dir(input_path, output_dir)
        return str(Path(resolved_dir) / f"{base_name}_translated.srt")

    @staticmethod
    def _output_dir_from_path(path_text: str) -> str:
        if not path_text:
            return ""
        path = Path(path_text)
        parent = path.parent
        if not parent or str(parent) in (".", ""):
            return ""
        return str(parent)

    def _remember_output_dir(self, output_dir: str) -> None:
        if not output_dir:
            return
        if not Path(output_dir).is_dir():
            return
        current = self._load_settings()
        if current.get("last_output_dir") == output_dir:
            self._settings = current
            return
        current["last_output_dir"] = output_dir
        self._settings = current
        self._config_store.save_user_settings(current)

    def _get_saved_output_dir(self) -> str:
        stored = self._settings.get("last_output_dir", "")
        if stored and Path(stored).is_dir():
            return stored
        return ""

    @staticmethod
    def _get_preprocessed_path(input_path: Path, output_path: Path) -> Path:
        base_name = input_path.stem or "output"
        output_dir = output_path.parent if output_path and output_path.parent else input_path.parent
        return output_dir / f"{base_name}_semantic.srt"

    def _load_translation_provider_model(self) -> None:
        providers = self._api_manager.get_providers()
        self.provider_combo.blockSignals(True)
        try:
            self.provider_combo.clear()
            self.provider_combo.addItems(providers)
            preferred = self._settings.get("current_provider", "")
            if preferred and preferred in providers:
                self.provider_combo.setCurrentText(preferred)
            elif providers and not self.provider_combo.currentText():
                self.provider_combo.setCurrentIndex(0)
        finally:
            self.provider_combo.blockSignals(False)
        self._refresh_translation_models(self.provider_combo.currentText())

    def reload_provider_configs(self) -> None:
        self._settings = self._load_settings()
        self._api_manager.reload_providers()
        self._load_translation_provider_model()
        self._apply_translation_settings()
        self._load_segmentation_provider_model()
        self._apply_segmentation_settings()

    def _reload_translation_providers(self) -> None:
        self.reload_provider_configs()

    def _refresh_translation_models(self, provider: str) -> None:
        models = self._api_manager.get_available_models(provider)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        model = self._settings.get("model", "")
        if model in models:
            self.model_combo.setCurrentText(model)
        elif models:
            default_model = self._api_manager.get_default_model(provider)
            if default_model in models:
                self.model_combo.setCurrentText(default_model)
            else:
                self.model_combo.setCurrentIndex(0)

    def _on_translation_provider_changed(self, provider: str) -> None:
        self._refresh_translation_models(provider)
        if hasattr(self, "api_key_line"):
            self.api_key_line.setText(self._get_api_key_for_provider(provider))
        self.provider_changed.emit(provider)
        self._update_translation_controls()

    def _apply_translation_settings(self) -> None:
        provider = self._settings.get("current_provider", "")
        if provider:
            index = self.provider_combo.findText(provider)
            if index >= 0:
                self.provider_combo.setCurrentIndex(index)
        self._refresh_translation_models(self.provider_combo.currentText())
        self.api_key_line.setText(self._get_api_key_for_provider(self.provider_combo.currentText()))

    def _start_translation_from_ui(self) -> None:
        provider = self.provider_combo.currentText().strip()
        model = self.model_combo.currentText().strip()
        api_key = self.api_key_line.text().strip()
        self.start_translation(provider, model, api_key)

    def _stop_translation_from_ui(self) -> None:
        self.stop_translation()

    def _on_output_dir_changed(self, output_dir: str) -> None:
        self._remember_output_dir(output_dir)

    def _on_translation_state_changed(self, active: bool) -> None:
        if active:
            self._last_task_status = "running"
        self.stop_translation_btn.setEnabled(active)
        self.provider_combo.setEnabled(not active)
        self.model_combo.setEnabled(not active)
        self.api_key_line.setEnabled(not active)
        self.translation_refresh_btn.setEnabled(not active)
        if hasattr(self, "back_btn"):
            self.back_btn.setEnabled(not active)
        if hasattr(self, "ring"):
            self.ring.setValue(0)
            if active:
                self.ring.setFormat("翻译中\n0%")
            elif self._last_task_status == "completed":
                self.ring.setFormat("完成")
            elif self._last_task_status == "stopped":
                self.ring.setFormat("已停止")
            elif self._last_task_status == "failed":
                self.ring.setFormat("失败")
            elif self._last_task_status == "partial":
                self.ring.setFormat("未完成")
            else:
                self.ring.setFormat("就绪")
        if hasattr(self, "waveform"):
            self.waveform.add_value(0)
        if hasattr(self, "task_card") and not active:
            self.task_card.set_value("0")
        self._last_progress_value = 0
        self._update_translation_controls()
        self._apply_action_button_styles()

    def _load_segmentation_provider_model(self) -> None:
        providers = self._api_manager.get_providers()
        self.seg_provider_combo.blockSignals(True)
        try:
            self.seg_provider_combo.clear()
            self.seg_provider_combo.addItems(providers)
            if providers:
                current = (
                    self._settings.get("segmentation_provider")
                    or self._settings.get("current_provider")
                    or providers[0]
                )
                if current in providers:
                    self.seg_provider_combo.setCurrentText(current)
                else:
                    self.seg_provider_combo.setCurrentIndex(0)
        finally:
            self.seg_provider_combo.blockSignals(False)
        self._refresh_segmentation_models(self.seg_provider_combo.currentText())

    def _reload_segmentation_providers(self) -> None:
        self.reload_provider_configs()

    def _refresh_segmentation_models(self, provider: str) -> None:
        models = self._api_manager.get_available_models(provider)
        self.seg_model_combo.clear()
        self.seg_model_combo.addItems(models)
        model = self._settings.get("segmentation_model") or self._settings.get("model", "")
        if model in models:
            self.seg_model_combo.setCurrentText(model)
        elif models:
            default_model = self._api_manager.get_default_model(provider)
            if default_model in models:
                self.seg_model_combo.setCurrentText(default_model)
            else:
                self.seg_model_combo.setCurrentIndex(0)

    def _on_segmentation_provider_changed(self, provider: str) -> None:
        self._refresh_segmentation_models(provider)

    def _apply_segmentation_settings(self) -> None:
        provider = self._settings.get("segmentation_provider") or self._settings.get("current_provider", "")
        if provider:
            index = self.seg_provider_combo.findText(provider)
            if index >= 0:
                self.seg_provider_combo.setCurrentIndex(index)
        self._refresh_segmentation_models(self.seg_provider_combo.currentText())

        self.seg_temperature.setValue(float(self._settings.get("segmentation_temperature", 0.2)))
        self.seg_timeout.setValue(int(self._settings.get("segmentation_timeout", self._settings.get("timeout", 180))))
        self.seg_max_retries.setValue(int(self._settings.get("segmentation_max_retries", self._settings.get("max_retries", 1))))
        self.seg_batch_concurrency.setValue(int(self._settings.get("segmentation_batch_concurrency", 2)))
        self.seg_failure_fallback.setChecked(bool(self._settings.get("segmentation_fallback_on_failure", False)))

    def _save_segmentation_settings(self) -> None:
        self._persist_segmentation_settings()
        QMessageBox.information(self, "分段配置", "分段配置已保存。")

    def _persist_segmentation_settings(self) -> None:
        self._settings = self._load_settings()
        self._settings["segmentation_provider"] = self.seg_provider_combo.currentText()
        self._settings["segmentation_model"] = self.seg_model_combo.currentText()
        self._settings["segmentation_temperature"] = float(self.seg_temperature.value())
        self._settings["segmentation_timeout"] = self.seg_timeout.value()
        self._settings["segmentation_max_retries"] = self.seg_max_retries.value()
        self._settings["segmentation_batch_concurrency"] = self.seg_batch_concurrency.value()
        self._settings["segmentation_fallback_on_failure"] = self.seg_failure_fallback.isChecked()
        self._config_store.save_user_settings(self._settings)

    def _get_api_key_for_provider(self, provider: str, fallback_key: str | None = None) -> str:
        api_keys = self._settings.get("api_keys", {})
        api_key = api_keys.get(provider, "")
        if not api_key and fallback_key and provider == self._settings.get("current_provider"):
            api_key = fallback_key
        return api_key or ""

    def _build_segmentation_config(self, fallback_api_key: str | None) -> SegmentationConfig | None:
        provider = self.seg_provider_combo.currentText() or self._settings.get("segmentation_provider")
        model = self.seg_model_combo.currentText() or self._settings.get("segmentation_model")
        if not provider or not model:
            QMessageBox.warning(self, "分段配置缺失", "请先设置分段接口和模型。")
            return None

        api_key = self._get_api_key_for_provider(provider, fallback_api_key)
        if not api_key:
            QMessageBox.warning(self, "缺少分段 API 密钥", "请先为分段接口配置 API 密钥。")
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
            QMessageBox.warning(self, "接口地址缺失", "分段接口缺少 base_url 配置。")
            return None

        timeout_seconds = max(30, int(self.seg_timeout.value()))
        max_retries = max(0, int(self.seg_max_retries.value()))
        batch_concurrency = max(1, int(self.seg_batch_concurrency.value()))
        temperature = float(self.seg_temperature.value())
        fallback_on_failure = bool(self.seg_failure_fallback.isChecked())

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

    def _update_segmentation_controls(self) -> None:
        if not self._is_segmentation_feature_enabled():
            self.smart_segment_btn.setEnabled(False)
            self.save_segmentation_btn.setEnabled(False)
            if hasattr(self, "seg_enable_switch"):
                self.seg_enable_switch.setEnabled(False)
                self.seg_enable_switch.blockSignals(True)
                self.seg_enable_switch.setChecked(False)
                self.seg_enable_switch.blockSignals(False)
            if hasattr(self, "segment_body"):
                self.segment_body.setVisible(False)
            self._apply_action_button_styles()
            return
        input_type = self._detect_input_type(self.input_line.text().strip())
        is_json = input_type == "JSON"

        panel_open = True
        if hasattr(self, "seg_enable_switch"):
            self.seg_enable_switch.setEnabled(is_json)
            if not is_json and self.seg_enable_switch.isChecked():
                self.seg_enable_switch.blockSignals(True)
                self.seg_enable_switch.setChecked(False)
                self.seg_enable_switch.blockSignals(False)
            panel_open = bool(self.seg_enable_switch.isChecked())
        if hasattr(self, "segment_body"):
            self.segment_body.setVisible(panel_open)
        if not panel_open:
            self.save_segmentation_btn.setEnabled(False)
            self.smart_segment_btn.setEnabled(False)
            self._apply_action_button_styles()
            return
        can_segment = is_json
        if self._seg_worker and self._seg_worker.isRunning():
            can_segment = False
        self.save_segmentation_btn.setEnabled(True)
        self.smart_segment_btn.setEnabled(can_segment)
        self._apply_action_button_styles()

    def _update_translation_controls(self) -> None:
        if not hasattr(self, "start_translation_btn"):
            return
        input_type = self._detect_input_type(self.input_line.text().strip())
        is_json = input_type == "JSON"
        can_start = (not self.is_translating()) and (not is_json)
        self.start_translation_btn.setEnabled(can_start)
        self._apply_action_button_styles()

    def _toggle_segmentation_panel(self, checked: bool) -> None:
        if not self._is_segmentation_feature_enabled():
            if hasattr(self, "segment_body"):
                self.segment_body.setVisible(False)
            return
        if hasattr(self, "segment_body"):
            self.segment_body.setVisible(bool(checked))
        self._update_segmentation_controls()

    def _is_segmentation_feature_enabled(self) -> bool:
        return bool(self._settings.get("segmentation_enabled", False))

    def apply_segmentation_feature_state(self, enabled: bool | None = None) -> None:
        if enabled is None:
            self._settings = self._load_settings()
            enabled = bool(self._settings.get("segmentation_enabled", False))
        else:
            self._settings["segmentation_enabled"] = bool(enabled)
        enabled = bool(enabled)
        if hasattr(self, "segment_card"):
            self.segment_card.setVisible(enabled)
        if hasattr(self, "seg_enable_switch"):
            self.seg_enable_switch.setEnabled(enabled)
            if not enabled:
                self.seg_enable_switch.blockSignals(True)
                self.seg_enable_switch.setChecked(False)
                self.seg_enable_switch.blockSignals(False)
        if hasattr(self, "segment_body"):
            panel_open = bool(self.seg_enable_switch.isChecked()) if hasattr(self, "seg_enable_switch") else True
            self.segment_body.setVisible(enabled and panel_open)
        self._update_segmentation_controls()

    def _run_smart_segmentation(self) -> None:
        if self._seg_worker and self._seg_worker.isRunning():
            QMessageBox.information(self, "智能分割进行中", "当前已有智能分割任务在运行。")
            return
        if not self._is_segmentation_feature_enabled():
            QMessageBox.information(self, "语义分段已关闭", "请在项目设置中启用语义分段功能。")
            return
        input_path_text = self.input_line.text().strip()
        output_path_text = self.output_line.text().strip()
        if not input_path_text:
            QMessageBox.warning(self, "缺少输入", "请先选择 JSON 输入文件。")
            return
        if self._detect_input_type(input_path_text) != "JSON":
            QMessageBox.information(self, "不支持", "智能分割仅支持 JSON 输入。")
            return
        if not output_path_text:
            QMessageBox.warning(self, "缺少输出", "请先设置输出路径。")
            return

        input_path = Path(input_path_text)
        if not input_path.exists():
            QMessageBox.warning(self, "文件不存在", "输入文件不存在或无法访问。")
            return

        self._settings = self._load_settings()
        self._persist_segmentation_settings()
        seg_config = self._build_segmentation_config(None)
        if seg_config is None:
            return

        pre_path = self._get_preprocessed_path(input_path, Path(output_path_text))
        self._seg_worker = SegmentationWorker(
            input_path=str(input_path),
            pre_output_path=str(pre_path),
            segmentation_config=seg_config,
        )
        self._seg_worker.log.connect(self._append_log)
        self._seg_worker.finished.connect(self._on_segmentation_finished)
        self._seg_worker.failed.connect(self._on_segmentation_failed)
        self._seg_worker.stopped.connect(self._on_segmentation_stopped)
        self._set_segmentation_busy(True)
        self._seg_worker.start()

    def _set_segmentation_busy(self, busy: bool) -> None:
        self.smart_segment_btn.setEnabled(not busy)
        self.save_segmentation_btn.setEnabled(not busy)
        self._apply_action_button_styles()
        if busy:
            if not self._seg_progress:
                self._seg_progress = QProgressDialog("正在智能分割，请稍候...", "取消", 0, 0, self)
                self._seg_progress.setWindowTitle("智能分割")
                self._seg_progress.setWindowModality(Qt.WindowModality.WindowModal)
                self._seg_progress.setAutoClose(False)
                self._seg_progress.setAutoReset(False)
                self._seg_progress.canceled.connect(self._cancel_segmentation)
            self._seg_progress.show()
        elif self._seg_progress:
            self._seg_progress.reset()
            self._seg_progress.close()
            self._seg_progress.deleteLater()
            self._seg_progress = None

    def _cancel_segmentation(self) -> None:
        if self._seg_worker and self._seg_worker.isRunning():
            self._append_log("已请求停止智能分割...")
            self._seg_worker.stop()

    def _on_segmentation_finished(self, pre_path: str) -> None:
        self._set_preprocessed_path(Path(pre_path))
        self._append_log("智能分割完成，预处理文件已生成。")
        self._seg_worker = None
        self._set_segmentation_busy(False)

    def _on_segmentation_failed(self, message: str) -> None:
        self._append_log(f"智能分割失败: {message}")
        QMessageBox.critical(self, "智能分割失败", message)
        self._seg_worker = None
        self._set_segmentation_busy(False)

    def _on_segmentation_stopped(self, message: str) -> None:
        self._append_log(message)
        QMessageBox.information(self, "智能分割已停止", message)
        self._seg_worker = None
        self._set_segmentation_busy(False)

    def _persist_settings(self, provider: str, model: str, api_key: str) -> None:
        self._settings["current_provider"] = provider
        self._settings["model"] = model
        if hasattr(self, "batch_slider"):
            self._settings["batch_size"] = int(self.batch_slider.value())
        if hasattr(self, "concurrency_slider"):
            self._settings["concurrency"] = int(self.concurrency_slider.value())
        api_keys = self._settings.get("api_keys", {})
        api_keys[provider] = api_key
        self._settings["api_keys"] = api_keys
        self._config_store.save_user_settings(self._settings)

    def _load_settings(self) -> Dict[str, Any]:
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
        }
        data = self._config_store.load_all()
        stored = data.get("user_settings") or {}
        merged = defaults.copy()
        merged.update(stored)
        system_prompts = data.get("system_prompts") or {}
        self._load_system_prompt(merged, system_prompts)
        if merged.get("system_prompt_id") and not stored.get("system_prompt_id"):
            stored["system_prompt_id"] = merged["system_prompt_id"]
            self._config_store.save_user_settings(stored)
        return merged

    def _load_system_prompt(self, settings: Dict[str, Any], system_prompts: Dict[str, Any] | List[Any]) -> None:
        resolved_prompt_id, content = resolve_system_prompt(settings, system_prompts)
        if resolved_prompt_id and not settings.get("system_prompt_id"):
            settings["system_prompt_id"] = resolved_prompt_id
        if content:
            settings["system_prompt"] = content

    def _should_resume(self, input_path: str) -> bool:
        state_manager = TranslationStateManager(input_path)
        if not state_manager.has_valid_state():
            return False

        valid, reason = state_manager.validate_source_file()
        if not valid:
            QMessageBox.information(self, "无法继续", reason)
            return False

        reply = QMessageBox.question(
            self,
            "继续翻译",
            "检测到历史进度，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return reply == QMessageBox.StandardButton.Yes
