from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QListWidget,
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
    SingleDirectionScrollArea,
)
from qfluentwidgets.common.config import qconfig
from qfluentwidgets.common.style_sheet import themeColor

from app.services.cleanup_subtitle_worker import CleanupSubtitleWorker
from app.services.theme_palette import build_theme_palette, color_to_hex
from app.ui.message_dialog import show_warning, show_info, show_error
from app.ui.path_utils import to_native_path


class DropFileArea(QFrame):
    filesDropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("cleanup_drop_area")
        self.setAcceptDrops(True)
        self.setProperty("dragging", False)
        self.setMinimumHeight(140)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(6)

        title = BodyLabel("拖拽字幕文件到此处", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip = BodyLabel("支持 .srt / .json", self)
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
            QFrame#cleanup_drop_area {{
                border: 2px dashed {border_color};
                border-radius: 12px;
                background-color: {drop_bg};
            }}
            QFrame#cleanup_drop_area[dragging="true"] {{
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
            suffix = Path(path).suffix.lower()
            if suffix in (".srt", ".json", ".txt") and Path(path).exists():
                valid.append(path)
        return valid


class CleanupSubtitlePage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("cleanup_subtitle_page")
        self._file_paths: list[Path] = []
        self._worker: CleanupSubtitleWorker | None = None
        self._align_json: Path | None = None
        self._align_srt: Path | None = None

        self._build_ui()
        self._update_mode_visibility()
        self._update_align_status()

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
        input_layout.addWidget(TitleLabel("清理字幕", input_card))
        input_layout.addWidget(BodyLabel("支持 .srt / .json", input_card))

        self.drop_area = DropFileArea(input_card)
        self.drop_area.filesDropped.connect(self._on_files_dropped)
        input_layout.addWidget(self.drop_area)

        self.file_list = QListWidget(input_card)
        self.file_list.setMinimumHeight(0)
        self.file_list.setMaximumHeight(84)
        self.file_list.setVisible(False)
        input_layout.addWidget(self.file_list)
        self._apply_palette()
        qconfig.themeColorChanged.connect(self._apply_palette)

        content_layout.addWidget(input_card)

        mode_card = CardWidget(content)
        mode_layout = QVBoxLayout(mode_card)
        mode_layout.setContentsMargins(16, 16, 16, 16)
        mode_layout.setSpacing(8)
        mode_layout.addWidget(TitleLabel("处理方式", mode_card))

        self.clean_switch = SwitchButton(mode_card)
        self.clean_switch.setOnText("启用")
        self.clean_switch.setOffText("停用")
        self.clean_switch.checkedChanged.connect(lambda checked: self._on_mode_toggled("clean", checked))
        self._add_option_row(
            mode_layout,
            "文本净化",
            "清理重复噪音、无意义词、重叠噪音。",
            self.clean_switch,
        )

        self.format_switch = SwitchButton(mode_card)
        self.format_switch.setOnText("启用")
        self.format_switch.setOffText("停用")
        self.format_switch.checkedChanged.connect(lambda checked: self._on_mode_toggled("format", checked))
        self._add_option_row(
            mode_layout,
            "排版整理",
            "断句、短句合并、停顿符号规范化。",
            self.format_switch,
        )

        self.full_switch = SwitchButton(mode_card)
        self.full_switch.setOnText("启用")
        self.full_switch.setOffText("停用")
        self.full_switch.checkedChanged.connect(lambda checked: self._on_mode_toggled("full", checked))
        self._add_option_row(
            mode_layout,
            "完整流程",
            "文本净化 + 排版整理一次完成。",
            self.full_switch,
        )

        self.format_options = QWidget(mode_card)
        format_options_layout = QVBoxLayout(self.format_options)
        format_options_layout.setContentsMargins(16, 0, 0, 0)
        format_options_layout.setSpacing(6)

        self.punc_split_switch = SwitchButton(self.format_options)
        self.punc_split_switch.setOnText("启用")
        self.punc_split_switch.setOffText("停用")
        self.punc_split_switch.setChecked(True)
        self._add_option_row(
            format_options_layout,
            "智能标点断句",
            "处理长句时优先在标点处拆分。",
            self.punc_split_switch,
            add_divider=False,
        )

        self.merge_short_switch = SwitchButton(self.format_options)
        self.merge_short_switch.setOnText("启用")
        self.merge_short_switch.setOffText("停用")
        self.merge_short_switch.setChecked(True)
        self._add_option_row(
            format_options_layout,
            "短句合并",
            "减少碎片化字幕行数。",
            self.merge_short_switch,
            add_divider=False,
        )

        self.ellipses_combo = ComboBox(self.format_options)
        self.ellipses_combo.addItems(["替换为标准标点（，。）", "规范化为省略号（……）"])
        self._add_option_row(
            format_options_layout,
            "停顿符号处理",
            "统一省略号的显示方式。",
            self.ellipses_combo,
            add_divider=False,
        )

        mode_layout.addWidget(self.format_options)

        self.optimize_switch = SwitchButton(mode_card)
        self.optimize_switch.setOnText("启用")
        self.optimize_switch.setOffText("停用")
        self.optimize_switch.checkedChanged.connect(lambda checked: self._on_mode_toggled("optimize", checked))
        self._add_option_row(
            mode_layout,
            "优化字幕",
            "仅 JSON：去呻吟/噪音 + 词级拆短，输出 *_split.srt。",
            self.optimize_switch,
        )

        self.post_clean_switch = SwitchButton(mode_card)
        self.post_clean_switch.setOnText("启用")
        self.post_clean_switch.setOffText("停用")
        self.post_clean_switch.checkedChanged.connect(lambda checked: self._on_mode_toggled("post_clean", checked))
        self._add_option_row(
            mode_layout,
            "译后清理",
            "仅 SRT：删除译文中的单行语气词/呻吟词，输出 *_post_cleaned.srt。",
            self.post_clean_switch,
        )

        self.align_switch = SwitchButton(mode_card)
        self.align_switch.setOnText("启用")
        self.align_switch.setOffText("停用")
        self.align_switch.checkedChanged.connect(lambda checked: self._on_mode_toggled("align", checked))
        self._align_divider = self._add_option_row(
            mode_layout,
            "对齐修段",
            "词级时间戳对齐拆段，输出 *_fixed.srt。",
            self.align_switch,
        )

        self.align_status = QWidget(mode_card)
        align_layout = QVBoxLayout(self.align_status)
        align_layout.setContentsMargins(16, 0, 0, 0)
        align_layout.setSpacing(4)

        self.align_json_status = BodyLabel("", self.align_status)
        self.align_srt_status = BodyLabel("", self.align_status)
        align_layout.addWidget(self.align_json_status)
        align_layout.addWidget(self.align_srt_status)

        mode_layout.addWidget(self.align_status)
        content_layout.addWidget(mode_card)

        bottom_bar = CardWidget(self)
        bottom_bar.setFixedHeight(60)
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(24, 6, 24, 6)
        bottom_layout.setSpacing(12)
        self.clear_files_btn = PushButton("清空列表")
        self.clear_files_btn.setFixedHeight(32)
        self.clear_files_btn.clicked.connect(self._clear_files)
        bottom_layout.addWidget(self.clear_files_btn)
        bottom_layout.addStretch(1)
        self.start_btn = PushButton("开始处理")
        self.start_btn.setFixedHeight(32)
        self.start_btn.setMinimumWidth(120)
        self.start_btn.clicked.connect(self._start_processing)
        bottom_layout.addWidget(self.start_btn)
        root.addWidget(bottom_bar)

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

    def _add_option_row(
        self,
        parent_layout: QVBoxLayout,
        title: str,
        desc: str,
        control: QWidget,
        *,
        add_divider: bool = True,
    ) -> QFrame | None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 10, 0, 10)
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

        if add_divider:
            divider = QFrame()
            divider.setFixedHeight(1)
            divider.setStyleSheet("background-color: #E5E5E5;")
            parent_layout.addWidget(divider)
            return divider
        return None

    def _on_mode_toggled(self, mode: str, checked: bool) -> None:
        if checked:
            switch_map = {
                "clean": self.clean_switch,
                "format": self.format_switch,
                "full": self.full_switch,
                "optimize": self.optimize_switch,
                "post_clean": self.post_clean_switch,
                "align": self.align_switch,
            }
            for key, switch in switch_map.items():
                if key == mode:
                    continue
                switch.blockSignals(True)
                switch.setChecked(False)
                switch.blockSignals(False)
        self._update_mode_visibility()

    def _update_mode_visibility(self) -> None:
        use_format_options = self.format_switch.isChecked() or self.full_switch.isChecked()
        self.format_options.setVisible(use_format_options)
        self.align_status.setVisible(self.align_switch.isChecked())
        self.ellipses_combo.setEnabled(use_format_options)
        if self._align_divider is not None:
            self._align_divider.setVisible(self.align_switch.isChecked())
        self._update_align_status()

    def _on_files_dropped(self, paths: Iterable[str]) -> None:
        for path in paths:
            file_path = Path(path)
            if file_path not in self._file_paths:
                self._file_paths.append(file_path)
        self._refresh_file_list()
        self._update_align_status()

    def _refresh_file_list(self) -> None:
        self.file_list.clear()
        if not self._file_paths:
            self.file_list.setVisible(False)
            return
        for path in self._file_paths:
            self.file_list.addItem(path.name)
        self.file_list.setVisible(True)

    def _clear_files(self) -> None:
        self._file_paths = []
        self._refresh_file_list()
        self._update_align_status()

    def _match_align_files(self) -> tuple[Path | None, Path | None]:
        json_files = [p for p in self._file_paths if p.suffix.lower() == ".json"]
        srt_files = [p for p in self._file_paths if p.suffix.lower() == ".srt"]

        json_path = None
        if json_files:
            json_path = max(json_files, key=lambda p: p.stat().st_size)

        def looks_like_ja_name(path: Path) -> bool:
            name = path.name.lower()
            return any(key in name for key in ["_pre", "_ja", "-ja", "jpn", "日文", "日本語"])

        def kana_count(path: Path) -> int:
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                return 0
            return len(re.findall(r"[ぁ-ゖァ-ヺー]", text))

        srt_path = None
        if srt_files:
            srt_path = sorted(
                srt_files,
                key=lambda p: (looks_like_ja_name(p), kana_count(p)),
                reverse=True,
            )[0]

        return json_path, srt_path

    def _update_align_status(self) -> None:
        json_path, srt_path = self._match_align_files()
        self._align_json = json_path
        self._align_srt = srt_path

        self._set_status_label(self.align_json_status, "Whisper JSON", bool(json_path))
        self._set_status_label(self.align_srt_status, "日文语义 SRT", bool(srt_path))

    @staticmethod
    def _set_status_label(label: BodyLabel, name: str, matched: bool) -> None:
        state_text = "已匹配" if matched else "缺失"
        color = "#2E8B57" if matched else "#9B9B9B"
        label.setText(f"{name}：{state_text}")
        label.setStyleSheet(f"color: {color};")

    def _current_mode(self) -> str | None:
        if self.clean_switch.isChecked():
            return "clean"
        if self.format_switch.isChecked():
            return "format"
        if self.full_switch.isChecked():
            return "full"
        if self.optimize_switch.isChecked():
            return "optimize"
        if self.post_clean_switch.isChecked():
            return "post_clean"
        if self.align_switch.isChecked():
            return "align"
        return None

    def _start_processing(self) -> None:
        if self._worker and self._worker.isRunning():
            show_info(self, "处理中", "当前已有清理任务在运行。")
            return
        if not self._file_paths:
            show_warning(self, "缺少文件", "请先拖入字幕文件。")
            return

        mode = self._current_mode()
        if not mode:
            show_warning(self, "未选择处理方式", "请先启用一种处理方式。")
            return

        if mode == "align":
            if not self._align_json or not self._align_srt:
                show_warning(self, "缺少文件", "对齐修段需要 JSON 与日文 SRT。")
                return
            self._worker = CleanupSubtitleWorker(
                "align",
                [],
                align_json=self._align_json,
                align_srt=self._align_srt,
            )
        elif mode == "optimize":
            json_files = [path for path in self._file_paths if path.suffix.lower() == ".json"]
            if not json_files:
                show_warning(self, "缺少文件", "优化字幕仅支持 JSON 输入。")
                return
            self._worker = CleanupSubtitleWorker(
                "optimize",
                json_files,
            )
        elif mode == "post_clean":
            srt_files = [path for path in self._file_paths if path.suffix.lower() == ".srt"]
            if not srt_files:
                show_warning(self, "缺少文件", "译后清理仅支持 SRT 输入。")
                return
            self._worker = CleanupSubtitleWorker(
                "post_clean",
                srt_files,
            )
        else:
            do_cleaning = mode in ("clean", "full")
            do_formatting = mode in ("format", "full")
            ellipses_mode = "replace"
            if self.ellipses_combo.currentText().startswith("规范化"):
                ellipses_mode = "normalize"
            self._worker = CleanupSubtitleWorker(
                "standard",
                self._file_paths,
                do_cleaning=do_cleaning,
                do_formatting=do_formatting,
                punc_split=self.punc_split_switch.isChecked(),
                ellipses_mode=ellipses_mode,
                merge_short=self.merge_short_switch.isChecked(),
            )

        self._set_processing_state(True)
        self._worker.finished.connect(self._on_process_finished)
        self._worker.failed.connect(self._on_process_failed)
        self._worker.start()

    def _on_process_finished(self) -> None:
        self._set_processing_state(False)
        show_info(self, "处理完成", "字幕清理已完成。")
        self._clear_files()

    def _on_process_failed(self, message: str) -> None:
        show_error(self, "处理失败", message)
        self._set_processing_state(False)

    def _set_processing_state(self, active: bool) -> None:
        self.start_btn.setEnabled(not active)
        self.clear_files_btn.setEnabled(not active)
        self.drop_area.setEnabled(not active)
        self.clean_switch.setEnabled(not active)
        self.format_switch.setEnabled(not active)
        self.full_switch.setEnabled(not active)
        self.optimize_switch.setEnabled(not active)
        self.post_clean_switch.setEnabled(not active)
        self.align_switch.setEnabled(not active)
        self.punc_split_switch.setEnabled(not active)
        self.merge_short_switch.setEnabled(not active)
        use_format_options = self.format_switch.isChecked() or self.full_switch.isChecked()
        self.ellipses_combo.setEnabled(not active and use_format_options)
