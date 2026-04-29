#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Config migration from Electron/legacy Python into data/config."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from modules.config_paths import ensure_config_dir, get_target_config_dir


SCHEMA_VERSION = 1
META_FILE = "migration_meta.json"


def _read_json(path: Path) -> Dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _read_json_files(paths: Dict[str, Path]) -> Dict[str, Dict[str, Any] | None]:
    """Read multiple JSON files concurrently (I/O bound)."""
    results: Dict[str, Dict[str, Any] | None] = {}
    if not paths:
        return results

    max_workers = min(4, len(paths))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_read_json, path): key for key, path in paths.items()}
        for future in as_completed(futures):
            key = futures[future]
            results[key] = future.result()
    return results


def _merge_primary_over_fallback(primary: Dict[str, Any] | None,
                                 fallback: Dict[str, Any] | None) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if isinstance(fallback, dict) and fallback:
        merged.update(fallback)
    if isinstance(primary, dict) and primary:
        merged.update(primary)
    return merged


def _normalize_system_prompts(data: Any) -> Dict[str, Any]:
    if not data:
        return {}
    if isinstance(data, dict):
        prompts = data.get("prompts")
        if isinstance(prompts, dict):
            return data
        if isinstance(prompts, list):
            return _system_prompts_from_list(prompts, data.get("current_prompt"), data.get("version"))
        if all(isinstance(value, dict) for value in data.values()):
            return _system_prompts_from_list(
                [{"id": key, **value} for key, value in data.items()],
                None,
                data.get("version"),
            )
        return {}
    if isinstance(data, list):
        return _system_prompts_from_list(data, None, None)
    return {}


def _system_prompts_from_list(items: list[Any],
                              current_prompt: str | None,
                              version: str | None) -> Dict[str, Any]:
    prompts: Dict[str, Any] = {}
    default_key = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        key = (item.get("id") or item.get("name") or "").strip()
        if not key:
            continue
        prompt = {
            "name": item.get("name", key),
            "description": item.get("description", ""),
            "content": item.get("content", ""),
            "tags": item.get("tags", []),
            "is_default": bool(item.get("is_default", False)),
        }
        prompts[key] = prompt
        if prompt["is_default"] and not default_key:
            default_key = key
    if not prompts:
        return {}
    chosen = current_prompt if current_prompt in prompts else (default_key or next(iter(prompts)))
    return {
        "prompts": prompts,
        "current_prompt": chosen,
        "version": version or "1.0",
    }


def _merge_system_prompts(primary: Any, fallback: Any) -> Dict[str, Any]:
    primary_norm = _normalize_system_prompts(primary)
    fallback_norm = _normalize_system_prompts(fallback)
    if not primary_norm:
        return fallback_norm
    if not fallback_norm:
        return primary_norm

    merged_prompts = dict(fallback_norm.get("prompts", {}))
    merged_prompts.update(primary_norm.get("prompts", {}))
    current_prompt = primary_norm.get("current_prompt") or fallback_norm.get("current_prompt")
    if current_prompt and current_prompt not in merged_prompts:
        current_prompt = ""
    result = {
        "prompts": merged_prompts,
        "version": primary_norm.get("version") or fallback_norm.get("version") or "1.0",
    }
    if current_prompt:
        result["current_prompt"] = current_prompt
    return result


def _extract_custom_providers(data: Dict[str, Any] | None) -> Dict[str, Any]:
    if not data:
        return {}
    if "custom_providers" in data:
        return data.get("custom_providers", {}) or {}
    return data


def _wrap_custom_providers(providers: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "custom_providers": providers,
        "metadata": {
            "version": "1.0",
            "updated_at": datetime.now().isoformat(),
            "total_providers": len(providers),
        },
    }


def _filter_api_providers(data: Dict[str, Any] | None,
                          allowed: set[str]) -> Dict[str, Any]:
    providers = (data or {}).get("providers", {}) if isinstance(data, dict) else {}
    filtered = {name: value for name, value in providers.items() if name in allowed}
    return {"providers": filtered, "templates": {}}


def _ensure_custom_metadata(name: str, config: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    merged = dict(config)
    merged.setdefault("name", name)
    merged.setdefault("is_custom", True)
    merged.setdefault("created_at", datetime.now().isoformat())
    merged.setdefault("updated_at", datetime.now().isoformat())
    return merged


def _detect_electron_root() -> Path | None:
    candidates = [
        Path("frontend") / "dist-electron" / "win-unpacked" / "data",
        Path("frontend") / "data",
    ]
    for root in candidates:
        if (root / "user_settings.json").exists() or (root / "config" / "user_settings.json").exists():
            return root
    return None


def _build_paths(root: Path) -> Dict[str, Path]:
    config_dir = root / "config"
    if (root / "api_providers.json").exists() or (root / "custom_providers.json").exists():
        config_dir = root
    user_settings = root / "user_settings.json"
    if not user_settings.exists():
        user_settings = config_dir / "user_settings.json"

    return {
        "user_settings": user_settings,
        "api_providers": config_dir / "api_providers.json",
        "custom_providers": config_dir / "custom_providers.json",
        "system_prompts": config_dir / "system_prompts.json",
        "theme": config_dir / "theme.json",
    }


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def migrate_to_data_config() -> Dict[str, Any]:
    """Run auto migration and return a summary (no secrets)."""
    target_dir = ensure_config_dir(get_target_config_dir())
    meta_path = target_dir / META_FILE
    if meta_path.exists():
        existing_meta = _read_json(meta_path) or {}
        if existing_meta.get("schema_version", 0) >= SCHEMA_VERSION:
            return {"status": "skipped", "reason": "already_migrated"}

    electron_root = _detect_electron_root()
    electron_data = _read_json_files(_build_paths(electron_root)) if electron_root else {}
    legacy_data = _read_json_files(_build_paths(Path("config")))

    user_settings = _merge_primary_over_fallback(
        electron_data.get("user_settings"),
        legacy_data.get("user_settings"),
    )

    allowed_builtins = {"openai", "deepseek"}
    if electron_data.get("api_providers"):
        api_providers = _filter_api_providers(electron_data.get("api_providers"), allowed_builtins)
    else:
        api_providers = _filter_api_providers(legacy_data.get("api_providers"), allowed_builtins)

    custom_primary = _extract_custom_providers(electron_data.get("custom_providers"))
    custom_fallback = _extract_custom_providers(legacy_data.get("custom_providers"))
    custom_providers = _merge_primary_over_fallback(custom_primary, custom_fallback)

    current_provider = user_settings.get("current_provider") if isinstance(user_settings, dict) else None
    segmentation_provider = user_settings.get("segmentation_provider") if isinstance(user_settings, dict) else None
    legacy_providers = (legacy_data.get("api_providers") or {}).get("providers", {})
    for provider_name in [current_provider, segmentation_provider]:
        if not provider_name:
            continue
        if provider_name in custom_providers:
            continue
        if provider_name in allowed_builtins:
            continue
        if provider_name in legacy_providers:
            custom_providers[provider_name] = _ensure_custom_metadata(
                provider_name,
                legacy_providers[provider_name],
            )

    system_prompts = _merge_system_prompts(
        electron_data.get("system_prompts"),
        legacy_data.get("system_prompts"),
    )
    theme = _merge_primary_over_fallback(
        electron_data.get("theme"),
        legacy_data.get("theme"),
    )

    _write_json(target_dir / "user_settings.json", user_settings)
    _write_json(target_dir / "api_providers.json", api_providers)
    _write_json(target_dir / "custom_providers.json", _wrap_custom_providers(custom_providers))
    if system_prompts:
        _write_json(target_dir / "system_prompts.json", system_prompts)
    if theme:
        _write_json(target_dir / "theme.json", theme)

    _write_json(meta_path, {
        "schema_version": SCHEMA_VERSION,
        "migrated_from": "electron_primary_legacy_fallback",
        "migrated_at": datetime.now().isoformat(),
    })

    return {
        "status": "migrated",
        "electron_root": str(electron_root) if electron_root else None,
        "target_dir": str(target_dir),
    }
