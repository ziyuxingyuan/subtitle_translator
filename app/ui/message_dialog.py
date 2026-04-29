from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    FluentIcon,
    IconWidget,
    MessageBoxBase,
    TitleLabel,
)
from qfluentwidgets.common.style_sheet import themeColor

from app.services.theme_palette import build_theme_palette, color_to_hex


class FluentMessageDialog(MessageBoxBase):
    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        message: str,
        confirm_text: str = "确定",
        cancel_text: str = "取消",
        show_cancel: bool = False,
        icon: FluentIcon | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowTitle(title)

        self.yesButton.setText(confirm_text)
        self.cancelButton.setText(cancel_text)
        if not show_cancel:
            self.cancelButton.hide()

        self.viewLayout.setContentsMargins(0, 0, 0, 0)
        self.viewLayout.setSpacing(0)
        palette = build_theme_palette(themeColor())
        dialog_bg = color_to_hex(palette.bg_secondary)
        self.widget.setStyleSheet(
            f"QFrame#centerWidget {{ background-color: {dialog_bg}; border-radius: 10px; }}"
        )
        self.widget.setMinimumWidth(560)

        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.setSpacing(12)

        card = CardWidget(content)
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)

        if icon is not None:
            icon_widget = IconWidget(icon, card)
            icon_widget.setFixedSize(28, 28)
            card_layout.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignTop)

        text_container = QWidget(card)
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)

        title_label = TitleLabel(title, text_container)
        title_label.setWordWrap(True)
        text_layout.addWidget(title_label)

        body_label = BodyLabel(message, text_container)
        body_label.setWordWrap(True)
        text_layout.addWidget(body_label)

        card_layout.addWidget(text_container, 1)
        content_layout.addWidget(card)
        self.viewLayout.addWidget(content)


def show_info(parent: QWidget | None, title: str, message: str) -> None:
    dialog = FluentMessageDialog(parent, title, message, icon=FluentIcon.INFO)
    dialog.exec()


def show_warning(parent: QWidget | None, title: str, message: str) -> None:
    dialog = FluentMessageDialog(parent, title, message, icon=FluentIcon.INFO)
    dialog.exec()


def show_error(parent: QWidget | None, title: str, message: str) -> None:
    dialog = FluentMessageDialog(parent, title, message, icon=FluentIcon.INFO)
    dialog.exec()


def ask_confirm(
    parent: QWidget | None,
    title: str,
    message: str,
    confirm_text: str = "确定",
    cancel_text: str = "取消",
    default_yes: bool = True,
) -> bool:
    dialog = FluentMessageDialog(
        parent,
        title,
        message,
        confirm_text=confirm_text,
        cancel_text=cancel_text,
        show_cancel=True,
        icon=FluentIcon.INFO,
    )
    if default_yes:
        dialog.yesButton.setFocus()
    else:
        dialog.cancelButton.setFocus()
    return dialog.exec() == QDialog.DialogCode.Accepted
