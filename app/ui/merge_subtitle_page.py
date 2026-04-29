from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFrame, QFileDialog
from qfluentwidgets import (
    CardWidget,
    TitleLabel,
    BodyLabel,
    CaptionLabel,
    StrongBodyLabel,
    PushButton,
    ComboBox,
    LineEdit,
    SingleDirectionScrollArea,
)
from qfluentwidgets.common.config import qconfig
from qfluentwidgets.common.style_sheet import themeColor

from app.services.merge_subtitle_worker import MergeSubtitleWorker
from app.services.theme_palette import build_theme_palette, color_to_hex
from app.ui.message_dialog import show_warning, show_info, show_error
from app.ui.path_utils import to_native_path


class MergeDropArea(QFrame):
    filesDropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("merge_drop_area")
        self.setAcceptDrops(True)
        self.setProperty("dragging", False)
        self.setMinimumHeight(120)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(6)

        title = BodyLabel("拖拽两个 SRT 文件到此处", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip = BodyLabel("支持 .srt", self)
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(tip)
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
            QFrame#merge_drop_area {{
                border: 2px dashed {border_color};
                border-radius: 12px;
                background-color: {drop_bg};
            }}
            QFrame#merge_drop_area[dragging="true"] {{
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
        valid = []
        for url in urls:
            if not url.isLocalFile():
                continue
            path = to_native_path(url.toLocalFile())
            if not path:
                continue
            if Path(path).suffix.lower() == ".srt" and Path(path).exists():
                valid.append(path)
        return valid


class MergeSubtitlePage(QWidget):
    STRATEGY_LABELS = {
        "pass1_primary": "Pass1 主导（无重叠）",
        "pass1_overlap": "Pass1 主导（30% 重叠容忍）",
    }
    STRATEGY_DESC = {
        "pass1_primary": "完全保留 Pass1，Pass2 仅在无重叠时补充。",
        "pass1_overlap": "以 Pass1 为主，允许与 Pass1 时间重叠不超过 30% 的 Pass2 补充。",
    }

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("merge_subtitle_page")
        self._pass1_path: Path | None = None
        self._pass2_path: Path | None = None
        self._output_path: Path | None = None
        self._worker: MergeSubtitleWorker | None = None

        self._build_ui()
        self._update_strategy_desc()

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

        input_card = CardWidget(content)
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(16, 16, 16, 16)
        input_layout.setSpacing(8)
        input_layout.addWidget(TitleLabel("合并字幕", input_card))
        input_layout.addWidget(BodyLabel("选择 Pass1 与 Pass2 的 SRT 文件进行合并。", input_card))

        self.drop_area = MergeDropArea(input_card)
        self.drop_area.filesDropped.connect(self._on_files_dropped)
        input_layout.addWidget(self.drop_area)

        self.pass1_line = LineEdit(input_card)
        self.pass1_line.setPlaceholderText("请选择 Pass1 SRT 文件")
        self.pass1_line.setReadOnly(True)
        self.pass1_btn = PushButton("选择 Pass1")
        self.pass1_btn.setFixedHeight(32)
        self.pass1_btn.clicked.connect(self._select_pass1)
        input_layout.addWidget(
            self._build_row("Pass1 字幕", "主要字幕来源。", self._build_path_control(self.pass1_line, self.pass1_btn))
        )

        self.pass2_line = LineEdit(input_card)
        self.pass2_line.setPlaceholderText("请选择 Pass2 SRT 文件")
        self.pass2_line.setReadOnly(True)
        self.pass2_btn = PushButton("选择 Pass2")
        self.pass2_btn.setFixedHeight(32)
        self.pass2_btn.clicked.connect(self._select_pass2)
        input_layout.addWidget(
            self._build_row("Pass2 字幕", "用于补充 Pass1 的空白段落。", self._build_path_control(self.pass2_line, self.pass2_btn))
        )

        content_layout.addWidget(input_card)

        strategy_card = CardWidget(content)
        strategy_layout = QVBoxLayout(strategy_card)
        strategy_layout.setContentsMargins(16, 16, 16, 16)
        strategy_layout.setSpacing(8)
        strategy_layout.addWidget(TitleLabel("合并策略", strategy_card))

        self.strategy_combo = ComboBox(strategy_card)
        self.strategy_combo.addItems(list(self.STRATEGY_LABELS.values()))
        self.strategy_combo.currentIndexChanged.connect(self._update_strategy_desc)
        strategy_layout.addWidget(self.strategy_combo)

        self.strategy_desc = CaptionLabel("", strategy_card)
        self.strategy_desc.setStyleSheet("color: #6B6B6B;")
        self.strategy_desc.setWordWrap(True)
        strategy_layout.addWidget(self.strategy_desc)
        content_layout.addWidget(strategy_card)

        output_card = CardWidget(content)
        output_layout = QVBoxLayout(output_card)
        output_layout.setContentsMargins(16, 16, 16, 16)
        output_layout.setSpacing(8)
        output_layout.addWidget(TitleLabel("输出文件", output_card))

        self.output_line = LineEdit(output_card)
        self.output_line.setPlaceholderText("请选择输出 SRT 文件")
        self.output_line.setReadOnly(False)
        self.output_line.editingFinished.connect(self._on_output_text_edited)
        self.output_btn = PushButton("选择输出文件")
        self.output_btn.setFixedHeight(32)
        self.output_btn.clicked.connect(self._select_output)
        output_layout.addWidget(self._build_path_control(self.output_line, self.output_btn))

        content_layout.addWidget(output_card)
        content_layout.addStretch(1)

        bottom_bar = CardWidget(self)
        bottom_bar.setFixedHeight(60)
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(24, 6, 24, 6)
        bottom_layout.setSpacing(12)
        self.clear_files_btn = PushButton("清空文件")
        self.clear_files_btn.setFixedHeight(32)
        self.clear_files_btn.clicked.connect(self._clear_files)
        bottom_layout.addWidget(self.clear_files_btn)
        bottom_layout.addStretch(1)
        self.start_btn = PushButton("开始合并")
        self.start_btn.setFixedHeight(32)
        self.start_btn.setMinimumWidth(120)
        self.start_btn.clicked.connect(self._start_merge)
        bottom_layout.addWidget(self.start_btn)
        root.addWidget(bottom_bar)

    @staticmethod
    def _build_path_control(line: LineEdit, button: PushButton) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        line.setMinimumWidth(320)
        layout.addWidget(line, 1)
        layout.addWidget(button)
        return container

    @staticmethod
    def _build_row(title: str, desc: str, control: QWidget) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(12)

        text_container = QWidget(row)
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.addWidget(StrongBodyLabel(title, text_container))
        if desc:
            desc_label = CaptionLabel(desc, text_container)
            desc_label.setStyleSheet("color: #6B6B6B;")
            desc_label.setWordWrap(True)
            text_layout.addWidget(desc_label)

        layout.addWidget(text_container, 1)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return row

    def _on_files_dropped(self, paths: Iterable[str]) -> None:
        for path in paths:
            file_path = Path(path)
            if self._pass1_path is None:
                self._set_pass1_path(file_path)
            elif self._pass2_path is None and file_path != self._pass1_path:
                self._set_pass2_path(file_path)
        if self._output_path is None:
            self._suggest_output_path()

    def _clear_files(self) -> None:
        self._pass1_path = None
        self._pass2_path = None
        self._output_path = None
        self.pass1_line.clear()
        self.pass1_line.setToolTip("")
        self.pass2_line.clear()
        self.pass2_line.setToolTip("")
        self.output_line.clear()
        self.output_line.setToolTip("")

    def _select_pass1(self) -> None:
        path = self._pick_srt("选择 Pass1 字幕")
        if path:
            self._set_pass1_path(Path(path))

    def _select_pass2(self) -> None:
        path = self._pick_srt("选择 Pass2 字幕")
        if path:
            self._set_pass2_path(Path(path))

    def _select_output(self) -> None:
        start_dir = ""
        if self._pass1_path:
            start_dir = str(self._pass1_path.parent)
        path, _ = QFileDialog.getSaveFileName(self, "保存合并字幕", start_dir, "SRT 文件 (*.srt)")
        if not path:
            return
        resolved = self._resolve_output_path(path)
        if resolved:
            self._set_output_path(Path(resolved))

    def _pick_srt(self, title: str) -> str:
        path, _ = QFileDialog.getOpenFileName(self, title, "", "SRT 文件 (*.srt)")
        return path

    def _set_pass1_path(self, path: Path) -> None:
        self._pass1_path = path
        self.pass1_line.setText(str(path))
        self.pass1_line.setToolTip(str(path))
        self._suggest_output_path()

    def _set_pass2_path(self, path: Path) -> None:
        self._pass2_path = path
        self.pass2_line.setText(str(path))
        self.pass2_line.setToolTip(str(path))

    def _set_output_path(self, path: Path) -> None:
        self._output_path = path
        self.output_line.setText(str(path))
        self.output_line.setToolTip(str(path))

    def _suggest_output_path(self) -> None:
        if self._output_path or not self._pass1_path:
            return
        suggested = self._pass1_path.with_name(f"{self._pass1_path.stem}_merged.srt")
        self._set_output_path(suggested)

    def _resolve_output_path(self, raw: str) -> str:
        text = raw.strip()
        if not text:
            return ""
        candidate = Path(text)
        if candidate.parent == Path("."):
            base_dir = None
            if self._output_path:
                base_dir = self._output_path.parent
            elif self._pass1_path:
                base_dir = self._pass1_path.parent
            if base_dir:
                candidate = base_dir / candidate.name
        if candidate.suffix.lower() != ".srt":
            candidate = candidate.with_suffix(".srt")
        return str(candidate)

    def _on_output_text_edited(self) -> None:
        resolved = self._resolve_output_path(self.output_line.text())
        if resolved != self.output_line.text():
            self.output_line.setText(resolved)
        if resolved:
            self._output_path = Path(resolved)
            self.output_line.setToolTip(resolved)
        else:
            self._output_path = None
            self.output_line.setToolTip("")

    def _current_strategy_key(self) -> str:
        label = self.strategy_combo.currentText()
        for key, value in self.STRATEGY_LABELS.items():
            if value == label:
                return key
        return "pass1_primary"

    def _update_strategy_desc(self) -> None:
        key = self._current_strategy_key()
        self.strategy_desc.setText(self.STRATEGY_DESC.get(key, ""))

    def _start_merge(self) -> None:
        if self._worker and self._worker.isRunning():
            show_info(self, "合并进行中", "当前已有合并任务在运行。")
            return
        if not self._pass1_path or not self._pass1_path.exists():
            show_warning(self, "缺少文件", "请先选择 Pass1 SRT 文件。")
            return
        if not self._pass2_path or not self._pass2_path.exists():
            show_warning(self, "缺少文件", "请先选择 Pass2 SRT 文件。")
            return
        if not self._output_path:
            show_warning(self, "缺少输出", "请先设置输出文件路径。")
            return

        strategy = self._current_strategy_key()
        self._set_processing_state(True)
        self._worker = MergeSubtitleWorker(
            pass1_path=str(self._pass1_path),
            pass2_path=str(self._pass2_path),
            output_path=str(self._output_path),
            strategy=strategy,
        )
        self._worker.finished.connect(self._on_merge_finished)
        self._worker.failed.connect(self._on_merge_failed)
        self._worker.start()

    def _on_merge_finished(self, stats: dict) -> None:
        self._set_processing_state(False)
        output_path = to_native_path(stats.get("output_path", "") or "")
        detail = (
            f"Pass1: {stats.get('pass1_count', 0)} 行，"
            f"Pass2: {stats.get('pass2_count', 0)} 行，"
            f"输出: {stats.get('merged_count', 0)} 行。"
        )
        show_info(self, "合并完成", f"{detail}\n{output_path}")
        self._clear_files()
        self._worker = None

    def _on_merge_failed(self, message: str) -> None:
        show_error(self, "合并失败", message)
        self._set_processing_state(False)
        self._worker = None

    def _set_processing_state(self, active: bool) -> None:
        self.start_btn.setEnabled(not active)
        self.clear_files_btn.setEnabled(not active)
        self.pass1_btn.setEnabled(not active)
        self.pass2_btn.setEnabled(not active)
        self.output_btn.setEnabled(not active)
        self.pass1_line.setEnabled(not active)
        self.pass2_line.setEnabled(not active)
        self.output_line.setEnabled(not active)
        self.strategy_combo.setEnabled(not active)
        self.drop_area.setEnabled(not active)
