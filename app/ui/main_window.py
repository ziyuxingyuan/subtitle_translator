from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QWidget,
    QLabel,
    QVBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QComboBox,
    QAbstractSpinBox,
)
from qfluentwidgets import FluentWindow, NavigationItemPosition, FluentIcon, SingleDirectionScrollArea
from qfluentwidgets.common.config import qconfig
from qfluentwidgets.common.style_sheet import setCustomStyleSheet, themeColor

from app.services.theme_manager import ThemeManager
from app.services.theme_palette import build_theme_palette, color_to_hex
from app.ui.task_page import TaskPage
from app.ui.providers_page import ProvidersPage
from app.ui.prompts_page import PromptsPage
from app.ui.settings_page import SettingsPage
from app.ui.logs_page import LogsPage
from app.ui.project_settings_page import ProjectSettingsPage
from app.ui.cleanup_subtitle_page import CleanupSubtitlePage
from app.ui.merge_subtitle_page import MergeSubtitlePage
from app.ui.theme_page import ThemePage
from app.ui.toolbox_automation_page import ToolboxAutomationPage


class MainWindow(FluentWindow):
    NAV_WIDTH = 200

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("main_window")
        self.setWindowTitle("字幕翻译器")
        self.resize(1200, 800)
        self.setMinimumSize(1000, 700)
        self.navigationInterface.setExpandWidth(self.NAV_WIDTH)

        panel = getattr(self.navigationInterface, "panel", None)
        if panel is not None and not panel.objectName():
            panel.setObjectName("app_navigation_panel")
        stacked = getattr(self, "stackedWidget", None)
        if stacked is not None and not stacked.objectName():
            stacked.setObjectName("app_stacked_widget")
        if hasattr(self, "titleBar") and self.titleBar is not None:
            self.titleBar.setObjectName("app_title_bar")
            self.titleBar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self.titleBar.setAutoFillBackground(True)

        if hasattr(self, "BORDER_WIDTH"):
            self.BORDER_WIDTH = 2
        if hasattr(self, "setMicaEffectEnabled"):
            self.setMicaEffectEnabled(False)

        self._theme_manager = ThemeManager()
        self._theme_manager.apply_saved()
        self._apply_background()
        self._apply_chrome_theme()
        qconfig.themeColorChanged.connect(self._apply_background)
        qconfig.themeColorChanged.connect(self._apply_chrome_theme)
        qconfig.themeColorChanged.connect(self._apply_input_theme)

        self.task_page = TaskPage()
        self.providers_page = ProvidersPage()

        self._add_pages()
        self._hide_nav_return_button()
        self._bind_signals()
        self._set_nav_menu_logo()
        self._apply_input_theme()
        QTimer.singleShot(0, self._center_on_screen)

        if hasattr(self, "titleBar") and hasattr(self.titleBar, "iconLabel"):
            self.titleBar.iconLabel.hide()

    def _add_pages(self) -> None:
        self._add_project_pages()
        self._add_task_pages()
        self._add_quality_pages()
        self._add_extra_pages()
        self._add_bottom_pages()

        self.switchTo(self.task_page)

    def _bind_signals(self) -> None:
        if hasattr(self, "providers_page"):
            self.providers_page.providers_changed.connect(self.task_page.reload_provider_configs)
            self.task_page.provider_changed.connect(self.providers_page.sync_active_provider)
            if (
                hasattr(self, "tool_box_page")
                and hasattr(self.tool_box_page, "reload_provider_configs")
                and hasattr(self.tool_box_page, "provider_changed")
            ):
                self.providers_page.providers_changed.connect(self.tool_box_page.reload_provider_configs)
                self.tool_box_page.provider_changed.connect(self.providers_page.sync_active_provider)
        if hasattr(self, "project_settings_page"):
            self.project_settings_page.segmentation_feature_changed.connect(
                self.task_page.apply_segmentation_feature_state
            )
            if hasattr(self, "tool_box_page") and hasattr(self.tool_box_page, "apply_segmentation_feature_state"):
                self.project_settings_page.segmentation_feature_changed.connect(
                    self.tool_box_page.apply_segmentation_feature_state
                )
        if hasattr(self, "logs_page"):
            if hasattr(self.logs_page, "append_operation_log"):
                self.task_page.log_message.connect(self.logs_page.append_operation_log)
            if hasattr(self.logs_page, "clear_operation_log"):
                self.task_page.log_cleared.connect(self.logs_page.clear_operation_log)
            if hasattr(self.logs_page, "clear_debug_log"):
                self.task_page.debug_log_cleared.connect(self.logs_page.clear_debug_log)
            if hasattr(self, "tool_box_page") and hasattr(self.tool_box_page, "log_message"):
                self.tool_box_page.log_message.connect(self.logs_page.append_operation_log)
            if hasattr(self.logs_page, "set_progress_value"):
                self.task_page.progress_value_changed.connect(self.logs_page.set_progress_value)
            if hasattr(self.logs_page, "set_progress_detail"):
                self.task_page.progress_detail_changed.connect(self.logs_page.set_progress_detail)
        if hasattr(self, "navigationInterface"):
            self.navigationInterface.displayModeChanged.connect(self._normalize_nav_panel_mode)

    def _apply_background(self, *_: object) -> None:
        palette = build_theme_palette(themeColor())
        bg = color_to_hex(palette.bg_primary)

        window_qss = f"QWidget#main_window {{ background-color: {bg}; }}"
        setCustomStyleSheet(self, window_qss, window_qss)
        if hasattr(self, "setCustomBackgroundColor"):
            self.setCustomBackgroundColor(bg, bg)

        stacked = getattr(self, "stackedWidget", None)
        if stacked is not None:
            stacked_qss = (
                "QWidget#app_stacked_widget {"
                f"background-color: {bg};"
                "border: none;"
                "}"
            )
            setCustomStyleSheet(stacked, stacked_qss, stacked_qss)

    def _apply_chrome_theme(self, *_: object) -> None:
        palette = build_theme_palette(themeColor())
        nav_bg = color_to_hex(palette.bg_primary)

        panel = getattr(self.navigationInterface, "panel", None)
        if panel is not None:
            nav_qss = (
                "QFrame#app_navigation_panel {"
                f"background-color: {nav_bg};"
                "border: none;"
                "}"
                f"QWidget#scrollWidget {{ background-color: {nav_bg}; border: none; }}"
            )
            setCustomStyleSheet(panel, nav_qss, nav_qss)

        if hasattr(self, "titleBar") and self.titleBar is not None:
            title_qss = (
                "QWidget#app_title_bar {"
                f"background-color: {nav_bg};"
                "border: none;"
                "}"
            )
            setCustomStyleSheet(self.titleBar, title_qss, title_qss)

    def _apply_input_theme(self, *_: object) -> None:
        palette = build_theme_palette(themeColor())
        field_bg = color_to_hex(palette.surface_2)
        field_border = color_to_hex(palette.border_strong, with_alpha=True)
        focus_border = color_to_hex(themeColor())

        line_qss = (
            "QLineEdit, QLineEdit[transparent=\"true\"] {"
            f"background-color: {field_bg};"
            f"border: 1px solid {field_border};"
            "border-radius: 6px;"
            "}"
            f"QLineEdit:focus, QLineEdit[transparent=\"true\"]:focus {{ border-color: {focus_border}; }}"
        )
        plain_qss = (
            "QPlainTextEdit, QPlainTextEdit[transparent=\"true\"] {"
            f"background-color: {field_bg};"
            f"border: 1px solid {field_border};"
            "border-radius: 8px;"
            "}"
            f"QPlainTextEdit:focus, QPlainTextEdit[transparent=\"true\"]:focus {{ border-color: {focus_border}; }}"
        )
        combo_qss = (
            "QComboBox {"
            f"background-color: {field_bg};"
            f"border: 1px solid {field_border};"
            "border-radius: 6px;"
            "padding: 4px 6px;"
            "}"
            f"QComboBox:focus {{ border-color: {focus_border}; }}"
            "QComboBox QAbstractItemView {"
            f"background-color: {field_bg};"
            f"border: 1px solid {field_border};"
            "}"
        )
        spin_qss = (
            "QAbstractSpinBox {"
            f"background-color: {field_bg};"
            f"border: 1px solid {field_border};"
            "border-radius: 6px;"
            "}"
            f"QAbstractSpinBox:focus {{ border-color: {focus_border}; }}"
        )

        for widget in self.findChildren(QLineEdit):
            setCustomStyleSheet(widget, line_qss, line_qss)
        for widget in self.findChildren(QPlainTextEdit):
            setCustomStyleSheet(widget, plain_qss, plain_qss)
        for widget in self.findChildren(QComboBox):
            setCustomStyleSheet(widget, combo_qss, combo_qss)
        for widget in self.findChildren(QAbstractSpinBox):
            setCustomStyleSheet(widget, spin_qss, spin_qss)

    def _on_navigation_mode_changed(self, _mode) -> None:
        self._refresh_current_page_layout()
        QTimer.singleShot(200, self._refresh_current_page_layout)

    def _refresh_current_page_layout(self) -> None:
        current = self.stackedWidget.currentWidget() if hasattr(self, "stackedWidget") else None
        if current:
            current.updateGeometry()
            current.adjustSize()
            current.repaint()
        if hasattr(self, "stackedWidget"):
            self.stackedWidget.updateGeometry()
            view = getattr(self.stackedWidget, "view", None)
            if view:
                view.updateGeometry()

    def _add_project_pages(self) -> None:
        self.addSubInterface(
            self.providers_page,
            FluentIcon.IOT,
            "接口管理",
            NavigationItemPosition.SCROLL,
        )

        self.project_settings_page = ProjectSettingsPage()
        self.addSubInterface(
            self.project_settings_page,
            FluentIcon.FOLDER,
            "项目设置",
            NavigationItemPosition.SCROLL,
        )

    def _add_task_pages(self) -> None:
        self.addSubInterface(
            self.task_page,
            FluentIcon.PLAY,
            "任务",
            NavigationItemPosition.SCROLL,
        )

    def _add_quality_pages(self) -> None:
        self.prompts_page = PromptsPage()
        self.addSubInterface(
            self.prompts_page,
            FluentIcon.BOOK_SHELF,
            "翻译提示词",
            NavigationItemPosition.SCROLL,
        )

    def _add_extra_pages(self) -> None:
        self.tool_box_page = ToolboxAutomationPage()
        self.addSubInterface(
            self.tool_box_page,
            FluentIcon.TILES,
            "自动化",
            NavigationItemPosition.SCROLL,
        )

        self.cleanup_subtitle_page = CleanupSubtitlePage()
        self.addSubInterface(
            self.cleanup_subtitle_page,
            FluentIcon.FONT_SIZE,
            "清理字幕",
            NavigationItemPosition.SCROLL,
        )

        self.merge_subtitle_page = MergeSubtitlePage()
        self.addSubInterface(
            self.merge_subtitle_page,
            FluentIcon.DOCUMENT,
            "合并字幕",
            NavigationItemPosition.SCROLL,
        )

        self.logs_page = LogsPage()
        self.addSubInterface(
            self.logs_page,
            FluentIcon.INFO,
            "日志",
            NavigationItemPosition.SCROLL,
        )

    def _add_bottom_pages(self) -> None:
        self.theme_page = ThemePage(self._theme_manager)
        self.addSubInterface(
            self.theme_page,
            FluentIcon.PALETTE,
            "主题配色",
            NavigationItemPosition.BOTTOM,
        )

        self.app_settings_page = SettingsPage()
        self.addSubInterface(
            self.app_settings_page,
            FluentIcon.SETTING,
            "应用设置",
            NavigationItemPosition.BOTTOM,
        )

    @staticmethod
    def _create_placeholder(title: str) -> QWidget:
        page = QWidget()
        page.setObjectName(f"{title}_page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll_area = SingleDirectionScrollArea(page, Qt.Orientation.Vertical)
        scroll_area.setWidgetResizable(True)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.enableTransparentBackground()
        scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(12)

        label = QLabel(f"{title} 页面尚未实现。")
        content_layout.addWidget(label)
        content_layout.addStretch(1)

        scroll_area.setWidget(content)
        layout.addWidget(scroll_area)
        return page

    def _normalize_nav_panel_mode(self, _mode) -> None:
        """标准化导航面板模式，确保 MENU 模式转换为 COMPACT。"""
        from qfluentwidgets import NavigationDisplayMode

        panel = getattr(self.navigationInterface, "panel", None)
        if panel is None:
            return
        if panel.displayMode == NavigationDisplayMode.MENU:
            panel.displayMode = NavigationDisplayMode.COMPACT
            panel.setProperty("menu", False)
            panel.setStyle(panel.style())
            panel.resize(48, panel.height())
        self._hide_nav_return_button()
        self._refresh_current_page_layout()
        QTimer.singleShot(200, self._refresh_current_page_layout)

    def _hide_nav_return_button(self) -> None:
        """隐藏导航面板的返回按钮。"""
        panel = getattr(self.navigationInterface, "panel", None)
        if panel is None:
            return
        if hasattr(panel, "setReturnButtonVisible"):
            panel.setReturnButtonVisible(False)

    def _set_nav_menu_logo(self) -> None:
        """设置导航菜单按钮的图标，图标文件不存在时安全降级。"""
        panel = getattr(self.navigationInterface, "panel", None)
        if panel is None or not hasattr(panel, "menuButton"):
            return
        icon_path = Path(__file__).resolve().parents[1] / "icons" / "app.ico"
        if icon_path.exists():
            panel.menuButton.setIcon(QIcon(str(icon_path)))
            if hasattr(panel.menuButton, "_icon_size"):
                panel.menuButton._icon_size = int(round(16 * 1.2))
                panel.menuButton.update()

    def _center_on_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = available.x() + (available.width() - self.width()) // 2
        y = available.y() + (available.height() - self.height()) // 2
        self.move(x, y)
