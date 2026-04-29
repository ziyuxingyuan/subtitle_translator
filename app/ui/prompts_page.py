from __future__ import annotations

from typing import Any, Dict, Tuple

from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QGuiApplication, QColor
from PyQt6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QDialog,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    IconWidget,
    LineEdit,
    MessageBoxBase,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SingleDirectionScrollArea,
    StrongBodyLabel,
    TitleLabel,
)
from qfluentwidgets.common.config import qconfig
from qfluentwidgets.common.style_sheet import themeColor

from app.data.config_store import ConfigStore
from app.services.logging_setup import get_logger
from app.ui.message_dialog import ask_confirm, show_info, show_warning


class PromptCard(CardWidget):
    prompt_selected = pyqtSignal(str)
    edit_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)

    def __init__(self, prompt_id: str, prompt_data: Dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("prompt_card")
        self.prompt_id = prompt_id
        self.prompt_data = prompt_data
        self.is_default = bool(prompt_data.get("is_default"))
        self._selected = False
        self._build_ui()
        qconfig.themeColorChanged.connect(self._schedule_selected_style)

    def _build_ui(self) -> None:
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(160)
        self.setFixedWidth(260)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        name = self.prompt_data.get("name") or self.prompt_id
        name_label = StrongBodyLabel(name, self)
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        preview_label = CaptionLabel(self._build_preview_text(), self)
        preview_label.setWordWrap(True)
        layout.addWidget(preview_label)

        layout.addStretch(1)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(6)

        edit_btn = PushButton("编辑", self)
        edit_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.prompt_id))
        action_row.addWidget(edit_btn)

        if self.is_default:
            action_row.addStretch(1)
            tag = CaptionLabel("系统预设", self)
            tag.setStyleSheet("color: #6B6B6B;")
            action_row.addWidget(tag)
        else:
            delete_btn = PushButton("删除", self)
            delete_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            delete_btn.clicked.connect(lambda: self.delete_requested.emit(self.prompt_id))
            action_row.addWidget(delete_btn)
            action_row.addStretch(1)

        layout.addLayout(action_row)

    def _build_preview_text(self) -> str:
        text = self.prompt_data.get("description") or self.prompt_data.get("content") or ""
        text = " ".join(text.strip().splitlines())
        return (text[:140] + "…") if len(text) > 140 else text

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_selected_style()

    def _schedule_selected_style(self, *_: object) -> None:
        QTimer.singleShot(0, self._apply_selected_style)

    def _apply_selected_style(self, *_: object) -> None:
        if not self._selected:
            self.setStyleSheet("")
            return
        primary = themeColor()
        active = QColor(primary)
        active.setAlphaF(0.08)
        self.setStyleSheet(
            "PromptCard#prompt_card {"
            f"border: 2px solid {primary.name()};"
            f"background-color: {active.name(QColor.NameFormat.HexArgb)};"
            "border-radius: 8px;"
            "}"
        )

    def mousePressEvent(self, event) -> None:
        child = self.childAt(event.position().toPoint())
        while child is not None:
            if isinstance(child, QAbstractButton):
                return super().mousePressEvent(event)
            child = child.parentWidget()
        if event.button() == Qt.MouseButton.LeftButton:
            self.prompt_selected.emit(self.prompt_id)
        return super().mousePressEvent(event)


class PromptEditDialog(MessageBoxBase):
    def __init__(self, parent: QWidget | None, prompt_id: str = "", prompt_data: Dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self._prompt_id = prompt_id
        self._prompt_data = prompt_data or {}
        self._is_edit = bool(prompt_data)

        self.setWindowTitle("提示词设置")
        self._init_size()
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.viewLayout.setContentsMargins(0, 0, 0, 0)
        self.viewLayout.setSpacing(0)

        scroll_area = SingleDirectionScrollArea(self, Qt.Orientation.Vertical)
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollBar:vertical { width: 0px; }"
            "QScrollBar:horizontal { height: 0px; }"
        )

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.setSpacing(12)

        scroll_area.setWidget(content)
        self.viewLayout.addWidget(scroll_area)

        card = CardWidget(content)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(10)

        content_layout.addWidget(card)
        content_layout.addStretch(1)

        card_layout.addWidget(StrongBodyLabel("提示词信息", card))

        self.id_input = LineEdit(self)
        self.id_input.setPlaceholderText("用于保存的唯一 ID")
        if self._is_edit:
            self.id_input.setText(prompt_id)
            self.id_input.setEnabled(False)

        self.name_input = LineEdit(self)
        self.name_input.setPlaceholderText("用于显示的名称")
        self.name_input.setText(self._prompt_data.get("name", prompt_id))

        self.desc_input = LineEdit(self)
        self.desc_input.setPlaceholderText("可选，用于说明提示词的用途")
        self.desc_input.setText(self._prompt_data.get("description", ""))

        tags = self._prompt_data.get("tags", [])
        tags_text = ", ".join(tags) if isinstance(tags, list) else ""
        self.tags_input = LineEdit(self)
        self.tags_input.setPlaceholderText("用逗号分隔多个标签")
        self.tags_input.setText(tags_text)

        self.content_edit = PlainTextEdit(self)
        self.content_edit.setPlaceholderText("请输入提示词内容")
        self.content_edit.setPlainText(self._prompt_data.get("content", ""))
        self.content_edit.setMinimumHeight(200)
        self.content_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_edit.setStyleSheet(
            "QScrollBar:vertical { width: 0px; }"
            "QScrollBar:horizontal { height: 0px; }"
        )

        card_layout.addWidget(BodyLabel("提示词 ID", card))
        card_layout.addWidget(self.id_input)
        card_layout.addWidget(BodyLabel("名称", card))
        card_layout.addWidget(self.name_input)
        card_layout.addWidget(BodyLabel("描述", card))
        card_layout.addWidget(self.desc_input)
        card_layout.addWidget(BodyLabel("标签", card))
        card_layout.addWidget(self.tags_input)
        card_layout.addWidget(BodyLabel("提示词内容", card))
        card_layout.addWidget(self.content_edit)

    def _init_size(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen:
            geometry = screen.availableGeometry()
            max_height = int(geometry.height() * 0.8)
            max_width = int(geometry.width() * 0.9)
            self.widget.setFixedSize(min(900, max_width), min(640, max_height))
        else:
            self.widget.setFixedSize(900, 640)

    def get_payload(self) -> Tuple[str, Dict[str, Any]] | None:
        prompt_id = self.id_input.text().strip()
        if not prompt_id:
            return None
        name = self.name_input.text().strip() or prompt_id
        tags = [t.strip() for t in self.tags_input.text().split(",") if t.strip()]
        data = {
            "name": name,
            "description": self.desc_input.text().strip(),
            "tags": tags,
            "content": self.content_edit.toPlainText(),
        }
        return prompt_id, data


class PromptsPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("prompts_page")
        self._store = ConfigStore()
        self._logger = get_logger("app")
        self._data: Dict[str, Any] = {}
        self._prompt_cards: Dict[str, PromptCard] = {}
        self._selected_prompt_id = ""
        self._loading = True

        self._build_ui()
        self._load_prompts()
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
        scroll_area.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollBar:vertical { width: 0px; }"
            "QScrollBar:horizontal { height: 0px; }"
        )

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.setSpacing(12)

        scroll_area.setWidget(content)
        root.addWidget(scroll_area)

        header = TitleLabel("翻译提示词", content)
        content_layout.addWidget(header)
        desc = BodyLabel("在此管理翻译提示词模板，点击卡片即可设为当前提示词。", content)
        desc.setWordWrap(True)
        content_layout.addWidget(desc)

        self.current_card = CardWidget(content)
        current_layout = QVBoxLayout(self.current_card)
        current_layout.setContentsMargins(20, 15, 20, 15)
        current_layout.setSpacing(12)

        current_header = QHBoxLayout()
        pin_icon = IconWidget(FluentIcon.PIN, self.current_card)
        pin_icon.setFixedSize(18, 18)
        current_header.addWidget(pin_icon)
        current_header.addWidget(StrongBodyLabel("当前提示词", self.current_card))
        current_header.addStretch(1)
        self.edit_current_btn = PushButton("编辑当前", self.current_card)
        self.edit_current_btn.clicked.connect(self._edit_current_prompt)
        current_header.addWidget(self.edit_current_btn)
        current_layout.addLayout(current_header)
        current_layout.addWidget(self._divider())

        name_row = QHBoxLayout()
        name_row.addWidget(BodyLabel("名称：", self.current_card))
        self.current_name_label = StrongBodyLabel("", self.current_card)
        name_row.addWidget(self.current_name_label)
        name_row.addStretch(1)
        current_layout.addLayout(name_row)

        self.current_content = PlainTextEdit(self.current_card)
        self.current_content.setReadOnly(True)
        self.current_content.setMinimumHeight(200)
        self.current_content.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.current_content.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.current_content.setStyleSheet(
            "QScrollBar:vertical { width: 0px; }"
            "QScrollBar:horizontal { height: 0px; }"
        )
        current_layout.addWidget(self.current_content)

        content_layout.addWidget(self.current_card)

        self.library_card = CardWidget(content)
        self.library_card.setMinimumHeight(520)
        self.library_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        library_layout = QVBoxLayout(self.library_card)
        library_layout.setContentsMargins(20, 15, 20, 15)
        library_layout.setSpacing(12)

        library_header = QHBoxLayout()
        library_header.addWidget(StrongBodyLabel("提示词广场", self.library_card))
        library_header.addStretch(1)
        self.add_prompt_btn = PrimaryPushButton(FluentIcon.ADD, "创建新提示词", self.library_card)
        self.add_prompt_btn.clicked.connect(self._add_prompt)
        library_header.addWidget(self.add_prompt_btn)
        self.refresh_btn = PushButton("刷新", self.library_card)
        self.refresh_btn.clicked.connect(self._load_prompts)
        library_header.addWidget(self.refresh_btn)
        library_layout.addLayout(library_header)

        self.cards_scroll = ScrollArea(self.library_card)
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setMinimumHeight(360)
        self.cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cards_scroll.setStyleSheet(
            "background: transparent; border: none;"
            "QScrollBar:vertical { width: 0px; }"
            "QScrollBar:horizontal { height: 0px; }"
        )

        self.cards_container = QWidget()
        self.cards_container.setStyleSheet("background: transparent;")
        self.cards_grid = QGridLayout(self.cards_container)
        self.cards_grid.setSpacing(12)
        self.cards_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.cards_grid.setContentsMargins(0, 0, 0, 0)

        self.cards_scroll.setWidget(self.cards_container)
        library_layout.addWidget(self.cards_scroll)

        content_layout.addWidget(self.library_card, 1)
        content_layout.addStretch(1)

    def _load_prompts(self) -> None:
        self._loading = True
        self._data = self._store.load_system_prompts() or {}
        prompts = self._data.get("prompts", {})
        self._update_prompt_cards(prompts)

        current_prompt = self._data.get("current_prompt", "")
        if current_prompt and current_prompt in prompts:
            self._select_prompt(current_prompt)
        elif prompts:
            self._select_prompt(next(iter(prompts)))
        else:
            self._select_prompt("")
        self._loading = False

    def _update_prompt_cards(self, prompts: Dict[str, Any]) -> None:
        self._clear_layout(self.cards_grid)
        self._prompt_cards.clear()

        num_cols = 3
        row = 0
        col = 0
        for prompt_id, prompt_data in prompts.items():
            card = PromptCard(prompt_id, prompt_data, self.cards_container)
            card.prompt_selected.connect(self._select_prompt)
            card.edit_requested.connect(self._edit_prompt)
            card.delete_requested.connect(self._delete_prompt)
            self.cards_grid.addWidget(card, row, col)
            self._prompt_cards[prompt_id] = card
            col += 1
            if col >= num_cols:
                col = 0
                row += 1

    def _select_prompt(self, prompt_id: str) -> None:
        prompts = self._data.get("prompts", {})
        if not prompt_id or prompt_id not in prompts:
            if prompts:
                prompt_id = next(iter(prompts))
            else:
                self._selected_prompt_id = ""
                self.current_name_label.setText("")
                self.current_content.setPlainText("")
                self._highlight_selected("")
                return

        self._selected_prompt_id = prompt_id
        prompt = prompts.get(prompt_id, {})
        self.current_name_label.setText(prompt.get("name", prompt_id))
        self.current_content.setPlainText(prompt.get("content", ""))
        self._highlight_selected(prompt_id)

        if not self._loading:
            self._data["current_prompt"] = prompt_id
            self._save_data()
            self._sync_user_settings_prompt(prompt_id)

    def _highlight_selected(self, prompt_id: str) -> None:
        for key, card in self._prompt_cards.items():
            card.set_selected(key == prompt_id)

    def _add_prompt(self) -> None:
        dialog = PromptEditDialog(self.window())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        payload = dialog.get_payload()
        if not payload:
            QMessageBox.warning(self, "提示", "提示词 ID 不能为空。")
            return

        prompt_id, data = payload
        prompts = self._data.setdefault("prompts", {})
        if prompt_id in prompts:
            QMessageBox.warning(self, "重复", "提示词 ID 已存在。")
            return

        data["is_default"] = False
        prompts[prompt_id] = data
        self._save_data()
        self._load_prompts()
        self._select_prompt(prompt_id)

    def _edit_current_prompt(self) -> None:
        if not self._selected_prompt_id:
            QMessageBox.information(self, "提示", "暂无可编辑的提示词。")
            return
        self._edit_prompt(self._selected_prompt_id)

    def _edit_prompt(self, prompt_id: str) -> None:
        prompts = self._data.get("prompts", {})
        prompt = prompts.get(prompt_id, {})
        if not prompt:
            return

        dialog = PromptEditDialog(self.window(), prompt_id, prompt)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        payload = dialog.get_payload()
        if not payload:
            QMessageBox.warning(self, "提示", "提示词 ID 不能为空。")
            return

        _, data = payload
        data["is_default"] = bool(prompt.get("is_default"))
        prompts[prompt_id] = data
        self._save_data()
        self._load_prompts()
        self._select_prompt(prompt_id)
        self._logger.info("Prompt updated: %s", prompt_id)

    def _delete_prompt(self, prompt_id: str) -> None:
        prompts = self._data.get("prompts", {})
        prompt = prompts.get(prompt_id, {})
        if prompt.get("is_default"):
            QMessageBox.warning(self, "受保护", "系统默认提示词不能删除。")
            return

        reply = QMessageBox.question(
            self,
            "删除提示词",
            f"确认删除提示词“{prompt_id}”吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        prompts.pop(prompt_id, None)
        self._save_data()
        self._load_prompts()

    @staticmethod
    def _clear_layout(layout: QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @staticmethod
    def _divider() -> QFrame:
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background-color: #E5E5E5;")
        return divider

    def _save_data(self) -> None:
        if "version" not in self._data:
            self._data["version"] = "1.0"
        self._store.save_system_prompts(self._data)

    def _sync_user_settings_prompt(self, prompt_id: str) -> None:
        if not prompt_id:
            return
        settings = self._store.load_user_settings() or {}
        settings["system_prompt_id"] = prompt_id
        self._store.save_user_settings(settings)
