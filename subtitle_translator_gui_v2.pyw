#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QFontDatabase, QIcon
from PyQt6.QtWidgets import QApplication

from app.ui.main_window import MainWindow
from app.services.logging_setup import setup_logging
from modules.config_migration import migrate_to_data_config
from modules.segmentation_engine import clear_segmentation_resume_cache


def _load_app_icon() -> QIcon | None:
    root = Path(__file__).resolve().parent
    candidates = [
        root / "app" / "icons" / "app.ico",
        root / "frontend" / "dist-electron" / "win-unpacked" / "resources" / "app" / "dist" / "assets" / "favicon-BnwTBXWB.ico",
        root / "frontend" / "build" / "icon.ico",
        root / "build" / "icon.ico",
        root / "resources" / "icon.ico",
    ]
    for path in candidates:
        if path.exists():
            return QIcon(str(path))
    return None


def _apply_app_font(app: QApplication) -> None:
    font_name = "HarmonyOS Sans SC"
    font_path = (
        Path(__file__).resolve().parent
        / "app"
        / "font"
        / "HarmonyOS_Sans_SC_Regular.ttf"
    )
    if font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id != -1 else []
        if families:
            font_name = families[0]
    font = QFont(font_name)
    font.setWeight(QFont.Weight.Normal)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    font.setStyleStrategy(
        QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality
    )
    app.setFont(font)
    app.setStyleSheet(f'QWidget {{ font-family: "{font_name}"; font-weight: 400; }}')


def main() -> int:
    if hasattr(Qt, "ApplicationAttribute") and hasattr(
        Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"
    ):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    setup_logging()
    migrate_to_data_config()

    app = QApplication(sys.argv)
    app.aboutToQuit.connect(clear_segmentation_resume_cache)
    _apply_app_font(app)
    app_icon = _load_app_icon()
    if app_icon and not app_icon.isNull():
        app.setWindowIcon(app_icon)
    window = MainWindow()
    if app_icon and not app_icon.isNull():
        window.setWindowIcon(app_icon)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
