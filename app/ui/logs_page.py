from __future__ import annotations

from datetime import datetime
import html
import json
from typing import Any, Dict

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTextEdit,
    QStackedWidget,
    QFrame,
)
from qfluentwidgets import (
    CheckBox,
    SingleDirectionScrollArea,
    SegmentedWidget,
    PushButton,
    BodyLabel,
)
from qfluentwidgets.common.config import qconfig
from qfluentwidgets.common.style_sheet import themeColor

from app.services.debug_log_buffer import debug_log_buffer
from app.services.theme_palette import build_theme_palette, color_to_hex


class LogsPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("logs_page")
        self._current_log_key = "operation"
        self._auto_scroll_enabled = True
        self._debug_enabled: bool | None = None

        self._build_ui()
        self._refresh_logs()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll_area = SingleDirectionScrollArea(self, Qt.Orientation.Vertical)
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.enableTransparentBackground()

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(12)

        scroll_area.setWidget(content)
        root.addWidget(scroll_area)

        header_row = QHBoxLayout()
        self.log_switch = SegmentedWidget(self)
        self.log_switch.addItem("operation", "操作日志")
        self.log_switch.addItem("debug", "调试日志")
        self.log_switch.setCurrentItem("operation")
        self.log_switch.currentItemChanged.connect(self._on_log_switch_changed)
        header_row.addWidget(self.log_switch)
        self.operation_label = BodyLabel("操作日志", self)
        self.operation_label.setVisible(False)
        header_row.addWidget(self.operation_label)
        header_row.addStretch(1)
        self.auto_scroll_check = CheckBox("自动滚动", self)
        self.auto_scroll_check.setChecked(True)
        self.auto_scroll_check.toggled.connect(self._on_auto_scroll_toggled)
        header_row.addWidget(self.auto_scroll_check)
        clear_btn = PushButton("清空")
        clear_btn.clicked.connect(self._clear_logs)
        header_row.addWidget(clear_btn)
        content_layout.addLayout(header_row)

        self.log_stack = QStackedWidget()
        content_layout.addWidget(self.log_stack, 1)

        self.operation_log = QTextEdit()
        self.operation_log.setReadOnly(True)
        self.operation_log.setMinimumHeight(220)
        self.operation_log.setFrameShape(QFrame.Shape.NoFrame)

        operation_panel = QWidget()
        operation_layout = QVBoxLayout(operation_panel)
        operation_layout.setContentsMargins(0, 0, 0, 0)
        operation_layout.setSpacing(8)
        operation_layout.addWidget(self.operation_log, 1)
        self.log_stack.addWidget(operation_panel)

        self.debug_log = QTextEdit()
        self.debug_log.setReadOnly(True)
        self.debug_log.setFrameShape(QFrame.Shape.NoFrame)

        debug_panel = QWidget()
        debug_layout = QVBoxLayout(debug_panel)
        debug_layout.setContentsMargins(0, 0, 0, 0)
        debug_layout.setSpacing(8)
        debug_layout.addWidget(self.debug_log, 1)
        self.log_stack.addWidget(debug_panel)

        self.log_stack.setCurrentIndex(0)

        self._debug_last_index = 0
        self._debug_timer = QTimer(self)
        self._debug_timer.setInterval(300)
        self._debug_timer.timeout.connect(self._flush_debug_entries)
        self._debug_timer.start()
        self._sync_debug_visibility()
        self._apply_palette()
        qconfig.themeColorChanged.connect(self._apply_palette)

    def _apply_palette(self, *_: object) -> None:
        palette = build_theme_palette(themeColor())
        log_bg = color_to_hex(palette.surface_2)
        self.operation_log.setStyleSheet(
            f"background-color: {log_bg}; border: none; font-family: \"HarmonyOS Sans SC\"; font-size: 12px;"
        )
        self.debug_log.setStyleSheet(
            f"background-color: {log_bg}; border: none; font-family: \"HarmonyOS Sans SC\"; font-size: 12px;"
        )

    def _on_log_switch_changed(self, route_key: str) -> None:
        self._current_log_key = route_key
        index = 0 if route_key == "operation" else 1
        self.log_stack.setCurrentIndex(index)

    def append_operation_log(self, message: str | Dict[str, Any]) -> None:
        self._append_operation_entry(message)

    def clear_operation_log(self) -> None:
        self.operation_log.clear()

    def clear_debug_log(self) -> None:
        self.debug_log.clear()
        self._debug_last_index = 0

    def _refresh_logs(self) -> None:
        self._flush_debug_entries()

    def _flush_debug_entries(self) -> None:
        self._sync_debug_visibility()
        if not debug_log_buffer.is_enabled():
            if self.debug_log.toPlainText():
                self.clear_debug_log()
            return
        entries, last_index, reset_required = debug_log_buffer.get_since(self._debug_last_index)
        if not entries:
            return
        if reset_required:
            self.debug_log.setPlainText("".join(entries))
        else:
            cursor = self.debug_log.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            for entry in entries:
                cursor.insertText(entry)
            self.debug_log.setTextCursor(cursor)
        if self._auto_scroll_enabled:
            self._scroll_to_bottom(self.debug_log)
        self._debug_last_index = last_index

    def _on_auto_scroll_toggled(self, checked: bool) -> None:
        self._auto_scroll_enabled = bool(checked)
        if self._auto_scroll_enabled:
            if self._current_log_key == "debug":
                self._scroll_to_bottom(self.debug_log)
            else:
                self._scroll_to_bottom(self.operation_log)

    def _clear_logs(self) -> None:
        if self._current_log_key == "debug":
            self.clear_debug_log()
        else:
            self.clear_operation_log()

    def _append_operation_entry(self, message: str | Dict[str, Any]) -> None:
        payload = self._normalize_payload(message)
        if not payload:
            return
        timestamp = payload.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        level = (payload.get("level") or self._infer_level(payload.get("message", ""))).upper()
        color = self._level_color(level)
        batch_index = payload.get("batch_index")
        total_batches = payload.get("total_batches")
        status = payload.get("status") or ""
        batch_text = ""
        if isinstance(batch_index, int) and batch_index > 0:
            if isinstance(total_batches, int) and total_batches > 0:
                batch_text = f"批次 {batch_index}/{total_batches}"
            else:
                batch_text = f"批次 {batch_index}"
        message_text = str(payload.get("message", "")).strip()
        safe_message = html.escape(message_text).replace("\n", "<br>")
        level_icon = self._build_level_icon(level)
        level_text = f"{level}{level_icon}"

        entry_html = (
            f'{self._format_tag(f"[{timestamp}]", "#8A8A8A", 165)}'
            f'{self._format_tag(level_text, color, 60, bold=True)}'
            f'{self._format_tag(batch_text, "#6B6B6B", 130)}'
            f'{self._format_tag(status, "#6B6B6B", 70)}'
            f'<span>{safe_message}</span>'
        )
        cursor = self.operation_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(entry_html + "<br>")
        self.operation_log.setTextCursor(cursor)
        if self._auto_scroll_enabled:
            self._scroll_to_bottom(self.operation_log)

    @staticmethod
    def _normalize_payload(message: str | Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(message, dict):
            return message
        if not isinstance(message, str):
            return {"message": str(message)}
        raw = message.strip()
        if raw.startswith("{") and raw.endswith("}"):
            try:
                parsed = json.loads(raw)
            except Exception:
                return {"message": message}
            if isinstance(parsed, dict):
                return parsed
        return {"message": message}

    @staticmethod
    def _format_tag(text: str, color: str, width: int, bold: bool = False) -> str:
        safe_text = html.escape(text) if text else ""
        if not safe_text:
            safe_text = "&nbsp;"
        style = f"color:{color}; display:inline-block; min-width:{width}px;"
        if bold:
            style += " font-weight:600;"
        return f'<span style="{style}">{safe_text}</span>'

    @staticmethod
    def _scroll_to_bottom(text_edit: QTextEdit) -> None:
        cursor = text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        text_edit.setTextCursor(cursor)
        text_edit.ensureCursorVisible()

    @staticmethod
    def _infer_level(message: str) -> str:
        lower = message.lower()
        if any(key in lower for key in ("错误", "失败", "exception", "traceback", "error", "invalid")):
            return "ERROR"
        if any(key in lower for key in ("警告", "重试", "超时", "retry", "timeout", "warning")):
            return "WARN"
        return "INFO"

    @staticmethod
    def _level_color(level: str) -> str:
        if level == "ERROR":
            return "#D13438"
        if level == "WARN":
            return "#C19C00"
        return "#6B6B6B"

    @staticmethod
    def _build_level_icon(level: str) -> str:
        emoji = "ℹ️"
        if level == "ERROR":
            emoji = "❌"
        elif level == "WARN":
            emoji = "⚠️"
        elif level == "INFO":
            emoji = "ℹ️"
        return emoji

    def _sync_debug_visibility(self) -> None:
        enabled = debug_log_buffer.is_enabled()
        if self._debug_enabled == enabled:
            return
        self._debug_enabled = enabled
        self._update_debug_visibility(enabled)

    def _update_debug_visibility(self, enabled: bool) -> None:
        self.log_switch.setVisible(enabled)
        self.operation_label.setVisible(not enabled)
        if not enabled:
            if self._current_log_key != "operation":
                self._current_log_key = "operation"
                self.log_stack.setCurrentIndex(0)
            self.log_switch.blockSignals(True)
            try:
                self.log_switch.setCurrentItem("operation")
            finally:
                self.log_switch.blockSignals(False)
