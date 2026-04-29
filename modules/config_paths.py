#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared config path helpers for legacy and new layouts."""

from __future__ import annotations

import os
from pathlib import Path


def _env_config_dir() -> Path | None:
    env_value = os.getenv("SUBTITLE_TRANSLATOR_CONFIG_DIR")
    if env_value:
        return Path(env_value)
    return None


def _detect_shared_config_dir() -> Path | None:
    """Detect external shared config directory when present."""
    repo_root = Path(__file__).resolve().parents[1]
    candidates = (
        repo_root / "配置",
        repo_root.parent / "配置",
        repo_root.parent.parent / "配置",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def get_config_dir() -> Path:
    """Return the active config dir based on env or existing layout."""
    env_dir = _env_config_dir()
    if env_dir:
        return env_dir

    shared_dir = _detect_shared_config_dir()
    if shared_dir:
        return shared_dir

    data_config = Path("data") / "config"
    if data_config.exists():
        return data_config

    return Path("config")


def get_target_config_dir() -> Path:
    """Return the preferred target config dir for new writes."""
    env_dir = _env_config_dir()
    if env_dir:
        return env_dir

    shared_dir = _detect_shared_config_dir()
    if shared_dir:
        return shared_dir

    return Path("data") / "config"


def ensure_config_dir(path: Path | None = None) -> Path:
    """Ensure config dir exists and return it."""
    config_dir = path or get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir
