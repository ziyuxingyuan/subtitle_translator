from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtGui import QColor


def color_to_hex(color: QColor, *, with_alpha: bool = False) -> str:
    fmt = QColor.NameFormat.HexArgb if with_alpha else QColor.NameFormat.HexRgb
    return color.name(fmt)


def _clamp_ratio(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _rgba(red: int, green: int, blue: int, alpha: float) -> QColor:
    return QColor(red, green, blue, int(round(alpha * 255)))


def mix_srgb(primary: QColor, base: QColor, primary_ratio: float) -> QColor:
    ratio = _clamp_ratio(primary_ratio)
    inverse = 1.0 - ratio
    red = round(primary.red() * ratio + base.red() * inverse)
    green = round(primary.green() * ratio + base.green() * inverse)
    blue = round(primary.blue() * ratio + base.blue() * inverse)
    alpha = round(primary.alpha() * ratio + base.alpha() * inverse)
    return QColor(red, green, blue, alpha)


@dataclass(frozen=True)
class ThemePalette:
    bg_primary: QColor
    bg_secondary: QColor
    bg_tertiary: QColor
    surface_1: QColor
    surface_2: QColor
    surface_3: QColor
    border: QColor
    border_strong: QColor
    glass_bg: QColor
    glass_border: QColor
    drop_area_bg: QColor


def build_theme_palette(accent: QColor) -> ThemePalette:
    base_primary = QColor("#f8fafc")
    base_secondary = QColor("#f1f5f9")
    base_tertiary = QColor("#e2e8f0")

    bg_primary = mix_srgb(accent, base_primary, 0.02)
    bg_secondary = mix_srgb(accent, base_secondary, 0.04)
    bg_tertiary = mix_srgb(accent, base_tertiary, 0.06)

    surface_1 = QColor("#ffffff")
    surface_2 = mix_srgb(accent, base_primary, 0.03)
    surface_3 = mix_srgb(accent, base_secondary, 0.06)

    border = mix_srgb(accent, _rgba(15, 23, 42, 0.08), 0.10)
    border_strong = mix_srgb(accent, _rgba(15, 23, 42, 0.15), 0.20)

    glass_bg = mix_srgb(accent, _rgba(255, 255, 255, 0.85), 0.05)
    glass_border = mix_srgb(accent, _rgba(15, 23, 42, 0.10), 0.15)
    drop_area_bg = mix_srgb(accent, _rgba(255, 255, 255, 0.35), 0.05)

    return ThemePalette(
        bg_primary=bg_primary,
        bg_secondary=bg_secondary,
        bg_tertiary=bg_tertiary,
        surface_1=surface_1,
        surface_2=surface_2,
        surface_3=surface_3,
        border=border,
        border_strong=border_strong,
        glass_bg=glass_bg,
        glass_border=glass_border,
        drop_area_bg=drop_area_bg,
    )
