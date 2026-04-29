from __future__ import annotations

import json
import re
from urllib.parse import urlparse
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from PyQt6.QtCore import Qt, QSize, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QAction, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QDialog,
    QListWidget,
    QAbstractItemView,
    QInputDialog,
    QMessageBox,
    QLineEdit,
    QFrame,
    QSizePolicy,
    QGraphicsDropShadowEffect,
)
from qfluentwidgets.common.config import qconfig
from qfluentwidgets.common.style_sheet import setCustomStyleSheet, themeColor
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    DropDownPushButton,
    FlowLayout,
    FluentIcon,
    LineEdit,
    PlainTextEdit,
    MessageBoxBase,
    PushButton,
    RoundMenu,
    SingleDirectionScrollArea,
    SpinBox,
    SwitchButton,
    TitleLabel,
)
from app.services.theme_palette import build_theme_palette, color_to_hex


class ProviderActionButton(DropDownPushButton):
    primaryClicked = pyqtSignal(str)

    def __init__(self, text: str, provider_key: str, parent: QWidget | None = None) -> None:
        # Avoid PushButton singledispatch calling self.__init__(parent=...) recursively.
        DropDownPushButton.__init__(self, parent)
        self.setText(text)
        self._provider_key = provider_key
        self._drop_width = 26
        self.setObjectName("provider_action_button")
        self._active = False
        self._apply_style(active=False)
        qconfig.themeColorChanged.connect(self._on_theme_changed)

    def set_active(self, active: bool) -> None:
        self._active = active
        self._apply_style(active)
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def _apply_style(self, active: bool) -> None:
        palette = build_theme_palette(themeColor())
        inactive_bg = color_to_hex(palette.surface_1)
        inactive_border = color_to_hex(palette.border_strong, with_alpha=True)
        if active:
            style = (
                "QPushButton#provider_action_button {"
                "background-color: --ThemeColorPrimary;"
                "border: 1px solid --ThemeColorPrimary;"
                "color: #FFFFFF;"
                "padding: 6px 30px 6px 52px;"
                "border-radius: 6px;"
                "text-align: left;"
                "}"
            )
        else:
            style = (
                "QPushButton#provider_action_button {"
                f"background-color: {inactive_bg};"
                f"border: 1px solid {inactive_border};"
                "padding: 6px 30px 6px 52px;"
                "border-radius: 6px;"
                "text-align: left;"
                "}"
            )
        setCustomStyleSheet(self, style, style)
        self._apply_shadow()

    def _on_theme_changed(self, *_: object) -> None:
        self._apply_style(self._active)

    def _apply_shadow(self) -> None:
        if not hasattr(self, "_shadow_effect"):
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(12)
            shadow.setOffset(0, 2)
            shadow.setColor(QColor(0, 0, 0, 40))
            self._shadow_effect = shadow
        self.setGraphicsEffect(self._shadow_effect)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and e.position().x() >= self.width() - self._drop_width:
            self._showMenu()
            self.setDown(False)
            self.update()
            return
        PushButton.mouseReleaseEvent(self, e)
        if e.button() == Qt.MouseButton.LeftButton:
            self.primaryClicked.emit(self._provider_key)

from app.data.config_store import ConfigStore
from modules.api_manager import APIManager
from modules.config_paths import get_config_dir
from modules.custom_provider_manager import CustomProviderManager
from app.ui.message_dialog import ask_confirm, show_info, show_warning


class ModelFetchWorker(QThread):
    result_ready = pyqtSignal(list, str)

    def __init__(
        self,
        base_url: str,
        api_key: str,
        auth_type: str,
        api_key_header: str,
        api_key_format: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.base_url = base_url
        self.api_key = api_key
        self.auth_type = auth_type
        self.api_key_header = api_key_header or "Authorization"
        self.api_key_format = api_key_format or "Bearer {key}"

    def run(self) -> None:
        try:
            base_url = self.base_url.strip().rstrip("/")
            if not base_url:
                self.result_ready.emit([], "接口地址为空")
                return

            if base_url.endswith("/v1"):
                models_url = f"{base_url}/models"
            else:
                models_url = f"{base_url}/v1/models"

            headers = {}
            if self.api_key:
                header_value = self.api_key_format.format(key=self.api_key)
                headers[self.api_key_header] = header_value

            response = requests.get(models_url, headers=headers, timeout=30)
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}"
                try:
                    error_data = response.json()
                    if "error" in error_data:
                        error_msg += f": {error_data['error'].get('message', '')}"
                except Exception:
                    pass
                self.result_ready.emit([], error_msg)
                return

            data = response.json()
            models: List[str] = []
            if isinstance(data, dict):
                if isinstance(data.get("data"), list):
                    for item in data["data"]:
                        model_id = item.get("id")
                        if model_id:
                            models.append(str(model_id))
                elif isinstance(data.get("models"), list):
                    models = [str(item) for item in data["models"]]

            if not models:
                self.result_ready.emit([], "接口返回的模型列表为空")
                return

            self.result_ready.emit(models, "")
        except Exception as exc:
            self.result_ready.emit([], f"获取模型失败: {exc}")


class ProviderEditDialog(MessageBoxBase):
    DEFAULT_AUTH_TYPE = "bearer"
    DEFAULT_API_KEY_HEADER = "Authorization"
    DEFAULT_API_KEY_FORMAT = "Bearer {key}"
    _active_fetch_workers: set[ModelFetchWorker] = set()

    def __init__(
        self,
        parent: QWidget | None,
        provider_key: str,
        config: Dict[str, Any],
        api_key: str,
        is_builtin: bool,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("接口设置")
        self._init_size()
        self._apply_palette()
        qconfig.themeColorChanged.connect(self._apply_palette)

        self._provider_key = provider_key
        self._original_name = config.get("name") or provider_key
        self._is_builtin = is_builtin
        self._payload: Tuple[str, Dict[str, Any], str] | None = None
        self._fetch_worker: ModelFetchWorker | None = None

        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.viewLayout.setContentsMargins(0, 0, 0, 0)
        self.viewLayout.setSpacing(0)

        scroll_area = SingleDirectionScrollArea(self, Qt.Orientation.Vertical)
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.setSpacing(12)

        scroll_area.setWidget(content)
        self.viewLayout.addWidget(scroll_area)

        card = CardWidget(content)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)

        content_layout.addWidget(card)
        content_layout.addStretch(1)

        card_layout.addWidget(TitleLabel("接口设置", card))
        desc = BodyLabel("配置接口地址、模型列表与限额参数。", card)
        desc.setWordWrap(True)
        card_layout.addWidget(desc)

        form = QVBoxLayout()
        form.setSpacing(0)

        self.name_line = LineEdit(self)
        self.name_line.setText(self._original_name)
        self.name_line.setPlaceholderText("请输入接口名称")
        if self._is_builtin:
            self.name_line.setEnabled(False)

        self.base_url_line = LineEdit(self)
        self.base_url_line.setText(config.get("base_url", ""))
        self.base_url_line.setPlaceholderText("例如：https://api.example.com/v1")

        self.description_edit = PlainTextEdit(self)
        self.description_edit.setPlainText(config.get("description", ""))
        self.description_edit.setPlaceholderText("补充该接口的适用场景或调用说明")
        self.description_edit.setFixedHeight(80)

        self.api_key_line = LineEdit(self)
        self.api_key_line.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_line.setText(api_key or "")
        self.api_key_line.setPlaceholderText("sk-********************")
        self._api_key_visible = False
        self._api_key_toggle = QAction(FluentIcon.VIEW.icon(), "", self)
        self._api_key_toggle.setToolTip("显示 API Key")
        self._api_key_toggle.triggered.connect(self._toggle_api_key_visibility)
        self.api_key_line.addAction(self._api_key_toggle, QLineEdit.ActionPosition.TrailingPosition)

        self.models_list = QListWidget(self)
        self.models_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for model in config.get("models", []) or []:
            self.models_list.addItem(str(model))
        self.models_list.setMinimumHeight(120)

        self.fetch_models_btn = PushButton("获取模型", self)
        self.fetch_models_btn.clicked.connect(self._fetch_models)
        self.add_model_btn = PushButton("添加模型", self)
        self.add_model_btn.clicked.connect(self._add_model)
        self.remove_model_btn = PushButton("移除模型", self)
        self.remove_model_btn.clicked.connect(self._remove_model)

        self.default_model_combo = ComboBox(self)
        self._refresh_default_model_options()
        default_model = config.get("default_model", "")
        if default_model:
            index = self.default_model_combo.findText(default_model)
            if index >= 0:
                self.default_model_combo.setCurrentIndex(index)

        self.max_tokens = SpinBox(self)
        self.max_tokens.setRange(0, 200000)
        self.max_tokens.setValue(int(config.get("max_tokens", 4096)))

        self.rpm_limit = SpinBox(self)
        self.rpm_limit.setRange(0, 200000)
        self.rpm_limit.setValue(int(config.get("requests_per_minute", 0)))

        self.tpm_limit = SpinBox(self)
        self.tpm_limit.setRange(0, 200000)
        self.tpm_limit.setValue(int(config.get("tokens_per_minute", 0)))

        self.supports_stream = SwitchButton(self)
        self.supports_stream.setOnText("开启")
        self.supports_stream.setOffText("关闭")
        self.supports_stream.setChecked(bool(config.get("supports_stream", False)))

        form.addLayout(self._build_row("接口名称", "用于区分与选择接口", self.name_line))
        form.addWidget(self._divider())
        form.addLayout(self._build_row("接口地址", "请确认是否需要 /v1 结尾", self.base_url_line))
        form.addWidget(self._divider())
        form.addLayout(self._build_row("接口说明", "可选，方便区分用途", self.description_edit))
        form.addWidget(self._divider())
        form.addLayout(self._build_row("API Key", "可选，获取模型时使用", self.api_key_line))
        form.addWidget(self._divider())
        form.addLayout(self._build_model_row())
        form.addWidget(self._divider())
        form.addLayout(self._build_row("默认模型", "从模型列表中选择", self.default_model_combo))
        form.addWidget(self._divider())
        form.addLayout(self._build_row("最大 Token 数", "每次请求的输出上限", self.max_tokens))
        form.addWidget(self._divider())
        form.addLayout(self._build_row("请求频率（RPM）", "每分钟请求数限制", self.rpm_limit))
        form.addWidget(self._divider())
        form.addLayout(self._build_row("Token 速率（TPM）", "每分钟 Token 数限制", self.tpm_limit))
        form.addWidget(self._divider())
        form.addLayout(self._build_row("流式输出", "接口是否支持流式返回", self.supports_stream))

        card_layout.addLayout(form)

    def _apply_palette(self, *_: object) -> None:
        palette = build_theme_palette(themeColor())
        bg = color_to_hex(palette.bg_secondary)
        self.widget.setStyleSheet(
            f"QFrame#centerWidget {{ background-color: {bg}; border-radius: 10px; }}"
        )

    def _init_size(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen:
            max_height = int(screen.availableGeometry().height() * 0.8)
            self.widget.setFixedSize(720, min(620, max_height))
        else:
            self.widget.setFixedSize(720, 620)

    def _build_row(self, title: str, desc: str, control: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(12)

        text_container = QWidget(self)
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.addWidget(BodyLabel(title, text_container))
        if desc:
            tip = CaptionLabel(desc, text_container)
            tip.setStyleSheet("color: #6B6B6B;")
            text_layout.addWidget(tip)
        text_container.setFixedWidth(240)

        row.addWidget(text_container)
        row.addStretch(1)
        row.addWidget(control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return row

    def _build_model_row(self) -> QHBoxLayout:
        container = QWidget(self)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(8)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        action_row.addWidget(self.fetch_models_btn)
        action_row.addWidget(self.add_model_btn)
        action_row.addWidget(self.remove_model_btn)
        action_row.addStretch(1)
        container_layout.addLayout(action_row)
        container_layout.addWidget(self.models_list)

        return self._build_row("模型列表", "支持获取与手动添加", container)

    @staticmethod
    def _divider() -> QFrame:
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background-color: #E5E5E5;")
        return divider

    def _refresh_default_model_options(self) -> None:
        models = [self.models_list.item(i).text() for i in range(self.models_list.count())]
        current = self.default_model_combo.currentText()
        self.default_model_combo.clear()
        self.default_model_combo.addItems(models)
        if current in models:
            self.default_model_combo.setCurrentText(current)
        elif models:
            self.default_model_combo.setCurrentIndex(0)

    def _add_model(self) -> None:
        model, ok = QInputDialog.getText(self, "添加模型", "请输入模型名称：")
        if not ok:
            return
        model = model.strip()
        if not model:
            return
        existing = [self.models_list.item(i).text() for i in range(self.models_list.count())]
        if model in existing:
            QMessageBox.information(self, "提示", "模型已存在。")
            return
        self.models_list.addItem(model)
        self._refresh_default_model_options()

    def _remove_model(self) -> None:
        current = self.models_list.currentRow()
        if current < 0:
            QMessageBox.information(self, "提示", "请选择要移除的模型。")
            return
        self.models_list.takeItem(current)
        self._refresh_default_model_options()

    def _fetch_models(self) -> None:
        self._prune_finished_fetch_workers()
        if self._fetch_worker is not None or any(
            worker.isRunning() for worker in self._active_fetch_workers
        ):
            show_info(self, "处理中", "模型列表正在获取中，请稍候。")
            return

        base_url = self.base_url_line.text().strip()
        if not base_url:
            QMessageBox.warning(self, "提示", "请先填写接口地址。")
            return

        api_key = self.api_key_line.text().strip()
        if not api_key:
            api_key, ok = QInputDialog.getText(
                self,
                "需要授权",
                "请输入 API Key 以获取模型列表：",
                QLineEdit.EchoMode.Password,
            )
            if not ok or not api_key:
                return
            self.api_key_line.setText(api_key.strip())

        self.fetch_models_btn.setEnabled(False)
        self.fetch_models_btn.setText("获取中...")

        worker = ModelFetchWorker(
            base_url=base_url,
            api_key=api_key,
            auth_type=self.DEFAULT_AUTH_TYPE,
            api_key_header=self.DEFAULT_API_KEY_HEADER,
            api_key_format=self.DEFAULT_API_KEY_FORMAT,
            parent=None,
        )
        self._fetch_worker = worker
        self._active_fetch_workers.add(worker)
        worker.result_ready.connect(self._on_models_fetched)
        worker.finished.connect(lambda: self._on_fetch_worker_finished(worker))
        worker.start()

    def _prune_finished_fetch_workers(self) -> None:
        for worker in list(self._active_fetch_workers):
            if worker is self._fetch_worker:
                continue
            if not worker.isRunning():
                self._active_fetch_workers.discard(worker)
                worker.deleteLater()

    def _on_fetch_worker_finished(self, worker: ModelFetchWorker) -> None:
        if self._fetch_worker is worker:
            self._fetch_worker = None
        self._active_fetch_workers.discard(worker)
        worker.deleteLater()

    def _on_models_fetched(self, models: List[str], error: str) -> None:
        self.fetch_models_btn.setEnabled(True)
        self.fetch_models_btn.setText("获取模型")
        if error:
            QMessageBox.warning(self, "获取模型失败", error)
            return

        existing = {self.models_list.item(i).text() for i in range(self.models_list.count())}
        added = 0
        for model in models:
            if model not in existing:
                self.models_list.addItem(model)
                existing.add(model)
                added += 1
        self._refresh_default_model_options()
        QMessageBox.information(self, "完成", f"模型列表已更新，新增 {added} 个模型。")

    def closeEvent(self, event) -> None:
        self._prune_finished_fetch_workers()
        if self._fetch_worker is not None or any(
            worker.isRunning() for worker in self._active_fetch_workers
        ):
            show_warning(self, "请稍候", "正在获取模型，请等待完成后再关闭窗口。")
            event.ignore()
            return
        super().closeEvent(event)

    def _handle_save(self) -> bool:
        name = self.name_line.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "接口名称不能为空。")
            return False

        base_url = self.base_url_line.text().strip()
        if not base_url:
            QMessageBox.warning(self, "提示", "接口地址不能为空。")
            return False

        models = [self.models_list.item(i).text() for i in range(self.models_list.count())]
        if not models:
            QMessageBox.warning(self, "提示", "模型列表不能为空。")
            return False

        default_model = self.default_model_combo.currentText().strip() or (models[0] if models else "")
        if default_model not in models:
            QMessageBox.warning(self, "提示", "默认模型必须在模型列表中。")
            return False

        config = {
            "name": name,
            "description": self.description_edit.toPlainText().strip(),
            "base_url": base_url,
            "models": models,
            "default_model": default_model,
            "auth_type": self.DEFAULT_AUTH_TYPE,
            "api_key_header": self.DEFAULT_API_KEY_HEADER,
            "api_key_format": self.DEFAULT_API_KEY_FORMAT,
            "max_tokens": int(self.max_tokens.value()),
            "requests_per_minute": int(self.rpm_limit.value()),
            "tokens_per_minute": int(self.tpm_limit.value()),
            "supports_stream": bool(self.supports_stream.isChecked()),
        }
        api_key = self.api_key_line.text().strip()
        self._payload = (name, config, api_key)
        return True

    def validate(self) -> bool:
        return self._handle_save()

    def _toggle_api_key_visibility(self) -> None:
        self._api_key_visible = not self._api_key_visible
        if self._api_key_visible:
            self.api_key_line.setEchoMode(QLineEdit.EchoMode.Normal)
            self._api_key_toggle.setIcon(FluentIcon.HIDE.icon())
            self._api_key_toggle.setToolTip("隐藏 API Key")
        else:
            self.api_key_line.setEchoMode(QLineEdit.EchoMode.Password)
            self._api_key_toggle.setIcon(FluentIcon.VIEW.icon())
            self._api_key_toggle.setToolTip("显示 API Key")

    def get_payload(self) -> Tuple[str, Dict[str, Any], str] | None:
        return self._payload

    @property
    def provider_key(self) -> str:
        return self._provider_key

    @property
    def is_builtin(self) -> bool:
        return self._is_builtin


class ProvidersPage(QWidget):
    providers_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("providers_page")

        self._api_manager = APIManager()
        self._custom_manager = CustomProviderManager()
        self._store = ConfigStore()
        self._settings = self._store.load_user_settings() or {}
        self._provider_buttons: Dict[str, ProviderActionButton] = {}
        self._icon_cache_dir = Path("data") / "cache" / "icons"
        self._icon_cache_dir.mkdir(parents=True, exist_ok=True)

        self._build_ui()
        self._refresh_provider_buttons()
        self._apply_page_palette()
        qconfig.themeColorChanged.connect(self._apply_page_palette)

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

        card = CardWidget(content)
        self._main_card = card
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)

        header_row = QHBoxLayout()
        header_title = TitleLabel("接口管理", card)
        header_row.addWidget(header_title)
        header_row.addStretch(1)
        self.add_button = DropDownPushButton("新增接口", card)
        self.add_button.setIcon(FluentIcon.ADD_TO)
        header_row.addWidget(self.add_button)
        card_layout.addLayout(header_row)

        desc = BodyLabel("在此管理翻译接口配置，点击按钮即可启用接口。", card)
        desc.setWordWrap(True)
        card_layout.addWidget(desc)

        self._builtin_label = BodyLabel("内置接口", card)
        card_layout.addWidget(self._builtin_label)
        builtin_container = QWidget(card)
        self._builtin_flow = FlowLayout(builtin_container, needAni=False)
        self._builtin_flow.setContentsMargins(0, 0, 0, 0)
        self._builtin_flow.setSpacing(8)
        card_layout.addWidget(builtin_container)

        self._custom_label = BodyLabel("自定义接口", card)
        card_layout.addWidget(self._custom_label)
        custom_container = QWidget(card)
        self._custom_flow = FlowLayout(custom_container, needAni=False)
        self._custom_flow.setContentsMargins(0, 0, 0, 0)
        self._custom_flow.setSpacing(8)
        card_layout.addWidget(custom_container)

        content_layout.addWidget(card)
        content_layout.addStretch(1)

        self._build_add_menu()

    def _apply_page_palette(self, *_: object) -> None:
        palette = build_theme_palette(themeColor())
        card_bg = color_to_hex(palette.bg_secondary)
        border = color_to_hex(palette.border_strong, with_alpha=True)
        main_qss = (
            "CardWidget {"
            f"background-color: {card_bg};"
            f"border: 1px solid {border};"
            "border-radius: 10px;"
            "}"
        )
        if hasattr(self, "_main_card"):
            setCustomStyleSheet(self._main_card, main_qss, main_qss)

    def _build_add_menu(self) -> None:
        menu = RoundMenu("", self.add_button)
        menu.addAction(Action(FluentIcon.ADD_TO, "空白接口", triggered=self._add_blank_provider))
        templates = self._api_manager.get_config_templates() or {}
        if templates:
            menu.addSeparator()
            for key, template in templates.items():
                menu.addAction(
                    Action(
                        FluentIcon.DICTIONARY,
                        key,
                        triggered=partial(self._add_provider_from_template, key, template),
                    )
                )
        self.add_button.setMenu(menu)

    def _sanitize_provider_key(self, provider_key: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", provider_key.strip())
        return safe or "provider"

    def _get_domain_key(self, base_url: str) -> Optional[str]:
        if not base_url:
            return None
        parsed = urlparse(base_url.strip())
        if not parsed.netloc:
            return None
        host = parsed.netloc.split("@")[-1].split(":")[0].strip().lower()
        return host or None

    def _get_cached_icon_path(self, domain_key: str | None, provider_key: str) -> Optional[Path]:
        safe = self._sanitize_provider_key(domain_key or provider_key)
        for suffix in (".ico", ".png"):
            candidate = self._icon_cache_dir / f"{safe}{suffix}"
            if candidate.exists():
                return candidate
        return None

    def _fetch_favicon(self, base_url: str) -> Optional[Tuple[bytes, str]]:
        if not base_url:
            return None
        parsed = urlparse(base_url.strip())
        if not parsed.scheme or not parsed.netloc:
            return None
        base = f"{parsed.scheme}://{parsed.netloc}"
        headers = {"User-Agent": "Mozilla/5.0"}
        for suffix in (".ico", ".png"):
            favicon_url = f"{base}/favicon{suffix}"
            try:
                response = requests.get(favicon_url, timeout=10, headers=headers)
            except Exception:
                continue
            if response.status_code != 200:
                continue
            content_type = (response.headers.get("Content-Type") or "").lower()
            content = response.content
            if not content:
                continue
            if "image" in content_type and ("png" in content_type or favicon_url.endswith(".png")):
                return content, ".png"
            if "image" in content_type and ("icon" in content_type or "ico" in content_type or favicon_url.endswith(".ico")):
                return content, ".ico"
            if content_type == "" and favicon_url.endswith(".ico"):
                return content, ".ico"
            continue
        return None

    def _cache_provider_icon(self, provider_key: str, base_url: str) -> None:
        try:
            icon_info = self._fetch_favicon(base_url)
            domain_key = self._get_domain_key(base_url)
            safe = self._sanitize_provider_key(domain_key or provider_key)
            ico_path = self._icon_cache_dir / f"{safe}.ico"
            png_path = self._icon_cache_dir / f"{safe}.png"
            if ico_path.exists() or png_path.exists():
                return
            if not icon_info:
                for path in (ico_path, png_path):
                    if path.exists():
                        try:
                            path.unlink()
                        except Exception:
                            pass
                return
            content, suffix = icon_info
            target = png_path if suffix == ".png" else ico_path
            try:
                target.write_bytes(content)
            except Exception:
                return
            for path in (ico_path, png_path):
                if path != target and path.exists():
                    try:
                        path.unlink()
                    except Exception:
                        pass
        except Exception:
            return

    def _clear_flow(self, flow: FlowLayout) -> None:
        while flow.count():
            item = flow.takeAt(0)
            widget = item.widget() if hasattr(item, "widget") else item
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _refresh_provider_buttons(self) -> None:
        self._api_manager.reload_providers()
        self._custom_manager = CustomProviderManager()
        self._settings = self._store.load_user_settings() or {}
        current = self._settings.get("current_provider", "")

        builtin = self._api_manager.providers_config.get("providers", {})
        custom = self._custom_manager.get_all_providers()

        self._clear_flow(self._builtin_flow)
        self._clear_flow(self._custom_flow)
        self._provider_buttons.clear()

        for key, info in builtin.items():
            self._builtin_flow.addWidget(self._build_provider_button(key, info, current))

        for name, info in custom.items():
            self._custom_flow.addWidget(self._build_provider_button(name, info, current))

    def _build_provider_button(
        self,
        provider_key: str,
        info: Dict[str, Any],
        current_provider: str,
    ) -> QWidget:
        display_name = info.get("name") or provider_key
        button = ProviderActionButton(display_name, provider_key, self)
        icon_path = self._get_cached_icon_path(self._get_domain_key(info.get("base_url", "")), provider_key)
        if icon_path:
            pixmap = QPixmap(str(icon_path))
            if not pixmap.isNull():
                button.setIcon(QIcon(pixmap))
                button.setIconSize(QSize(18, 18))
                button.setText(f"    {display_name}")
        button.setFixedWidth(192)
        button.setToolTip(info.get("description", ""))
        button.set_active(provider_key == current_provider)
        button.primaryClicked.connect(self._activate_provider)

        menu = RoundMenu("", button)
        menu.addAction(
            Action(
                FluentIcon.EDIT,
                "编辑接口",
                triggered=partial(self._edit_provider, provider_key),
            )
        )
        menu.addSeparator()
        menu.addAction(
            Action(
                FluentIcon.SEND,
                "测试接口",
                triggered=partial(self._test_provider, provider_key),
            )
        )

        if self._custom_manager.provider_exists(provider_key):
            menu.addSeparator()
            menu.addAction(
                Action(
                    FluentIcon.DELETE,
                    "删除接口",
                    triggered=partial(self._delete_provider, provider_key),
                )
            )

        button.setMenu(menu)
        self._provider_buttons[provider_key] = button
        return button

    def _activate_provider(self, provider: str) -> None:
        providers = self._api_manager.get_providers()
        if provider not in providers:
            QMessageBox.warning(self, "启用失败", "接口不存在或已被移除。")
            return

        settings = self._store.load_user_settings() or {}
        settings["current_provider"] = provider

        models = self._api_manager.get_available_models(provider)
        current_model = settings.get("model", "")
        if current_model not in models:
            fallback = self._api_manager.get_default_model(provider) or (models[0] if models else "")
            settings["model"] = fallback

        self._store.save_user_settings(settings)
        self._settings = settings
        self._set_active_provider(provider)
        self.providers_changed.emit()

    def sync_active_provider(self, provider: str) -> None:
        provider = provider.strip()
        if not provider:
            return
        providers = self._api_manager.get_providers()
        if provider not in providers:
            return
        settings = self._store.load_user_settings() or {}
        if settings.get("current_provider") == provider:
            self._set_active_provider(provider)
            return
        settings["current_provider"] = provider
        models = self._api_manager.get_available_models(provider)
        current_model = settings.get("model", "")
        if current_model not in models:
            fallback = self._api_manager.get_default_model(provider) or (models[0] if models else "")
            settings["model"] = fallback
        self._store.save_user_settings(settings)
        self._settings = settings
        self._set_active_provider(provider)
        self.providers_changed.emit()

    def _add_blank_provider(self) -> None:
        self._open_edit_dialog(
            provider_key="",
            config={
                "name": "",
                "description": "",
                "base_url": "",
                "models": [],
                "default_model": "",
                "auth_type": "bearer",
                "api_key_header": "Authorization",
                "api_key_format": "Bearer {key}",
                "max_tokens": 4096,
                "requests_per_minute": 0,
                "tokens_per_minute": 0,
                "supports_stream": False,
            },
            is_custom=True,
        )

    def _add_provider_from_template(self, key: str, template: Dict[str, Any]) -> None:
        provider_id = template.get("provider")
        provider_info = self._api_manager.get_provider_info(provider_id) if provider_id else {}
        models = provider_info.get("models") or ([template.get("model")] if template.get("model") else [])
        default_model = provider_info.get("default_model") or template.get("model", "")
        config = {
            "name": template.get("name") or key,
            "description": template.get("description", ""),
            "base_url": template.get("endpoint") or provider_info.get("base_url", ""),
            "models": models,
            "default_model": default_model,
            "auth_type": provider_info.get("auth_type", "bearer"),
            "api_key_header": provider_info.get("api_key_header", "Authorization"),
            "api_key_format": provider_info.get("api_key_format", "Bearer {key}"),
            "max_tokens": provider_info.get("max_tokens", 4096),
            "requests_per_minute": provider_info.get("requests_per_minute", 0),
            "tokens_per_minute": provider_info.get("tokens_per_minute", 0),
            "supports_stream": provider_info.get("supports_stream", False),
        }
        self._open_edit_dialog(provider_key="", config=config, is_custom=True)

    def _edit_provider(self, provider_key: str) -> None:
        is_custom = self._custom_manager.provider_exists(provider_key)
        info = self._api_manager.get_provider_info(provider_key) or {}
        api_key = (self._store.load_user_settings() or {}).get("api_keys", {}).get(provider_key, "")
        self._open_edit_dialog(provider_key=provider_key, config=info, is_custom=is_custom, api_key=api_key)

    def _open_edit_dialog(
        self,
        provider_key: str,
        config: Dict[str, Any],
        is_custom: bool,
        api_key: str = "",
    ) -> None:
        dialog = ProviderEditDialog(self.window(), provider_key, config, api_key, is_builtin=not is_custom)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        payload = dialog.get_payload()
        if payload is None:
            return

        name, new_config, api_key_value = payload
        if not name:
            return

        if is_custom:
            self._save_custom_provider(provider_key, name, new_config, api_key_value)
        else:
            self._save_builtin_provider(provider_key, new_config, api_key_value)

    def _save_custom_provider(self, original_name: str, name: str, config: Dict[str, Any], api_key: str) -> None:
        all_providers = set(self._api_manager.get_providers())
        if name != original_name and name in all_providers:
            QMessageBox.warning(self, "保存失败", f"接口名称“{name}”已存在。")
            return

        if original_name:
            if name != original_name:
                success, message = self._custom_manager.rename_provider(original_name, name)
                if not success:
                    QMessageBox.warning(self, "保存失败", message)
                    return
            success, message = self._custom_manager.update_provider(name, config)
        else:
            success, message = self._custom_manager.add_provider(name, config)

        if not success:
            QMessageBox.warning(self, "保存失败", message)
            return

        if original_name and name != original_name:
            self._remove_api_key(original_name)
        self._save_api_key(name, api_key)
        self._cache_provider_icon(name, config.get("base_url", ""))
        self._schedule_refresh()
        self.providers_changed.emit()

    def _save_builtin_provider(self, provider_key: str, config: Dict[str, Any], api_key: str) -> None:
        config_path = get_config_dir() / "api_providers.json"
        raw = {}
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        providers = raw.get("providers", {})
        if provider_key not in providers:
            QMessageBox.warning(self, "保存失败", "内置接口不存在。")
            return

        merged = providers.get(provider_key, {}).copy()
        merged.update(config)
        providers[provider_key] = merged
        raw["providers"] = providers
        raw.setdefault("templates", {})

        try:
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", f"写入配置失败: {exc}")
            return

        self._save_api_key(provider_key, api_key)
        self._cache_provider_icon(provider_key, config.get("base_url", ""))
        self._schedule_refresh()
        self.providers_changed.emit()

    def _save_api_key(self, provider: str, api_key: str) -> None:
        if api_key is None:
            return
        settings = self._store.load_user_settings() or {}
        api_keys = settings.get("api_keys", {})
        if api_key:
            api_keys[provider] = api_key
        elif provider in api_keys:
            del api_keys[provider]
        settings["api_keys"] = api_keys
        self._store.save_user_settings(settings)

    def _remove_api_key(self, provider: str) -> None:
        settings = self._store.load_user_settings() or {}
        api_keys = settings.get("api_keys", {})
        if provider in api_keys:
            del api_keys[provider]
            settings["api_keys"] = api_keys
            self._store.save_user_settings(settings)

    def _delete_provider(self, name: str) -> None:
        reply = QMessageBox.question(
            self,
            "删除接口",
            f"确认删除接口“{name}”？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        success, message = self._custom_manager.delete_provider(name)
        if not success:
            QMessageBox.warning(self, "删除失败", message)
            return

        settings = self._store.load_user_settings() or {}
        api_keys = settings.get("api_keys", {})
        if name in api_keys:
            del api_keys[name]
        settings["api_keys"] = api_keys
        if settings.get("current_provider") == name:
            settings["current_provider"] = ""
        self._store.save_user_settings(settings)

        self._schedule_refresh()
        self.providers_changed.emit()

    def _test_provider(self, provider_key: str) -> None:
        api_key = (self._store.load_user_settings() or {}).get("api_keys", {}).get(provider_key, "")
        if not api_key:
            api_key, ok = QInputDialog.getText(
                self,
                "测试接口",
                f"请输入 '{provider_key}' 的 API Key：",
                QLineEdit.EchoMode.Password,
            )
            if not ok or not api_key:
                return

        success, message = self._api_manager.test_api_connection(provider_key, api_key)
        if success:
            QMessageBox.information(self, "测试成功", message)
        else:
            QMessageBox.warning(self, "测试失败", message)

    def _schedule_refresh(self) -> None:
        QTimer.singleShot(0, self._refresh_provider_buttons)

    def _set_active_provider(self, provider: str) -> None:
        for key, button in self._provider_buttons.items():
            button.set_active(key == provider)
