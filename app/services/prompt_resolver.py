from __future__ import annotations

from typing import Any, Dict, List, Tuple


def normalize_prompt_map(system_prompts: Dict[str, Any] | List[Any]) -> Dict[str, Dict[str, Any]]:
    prompt_map: Dict[str, Dict[str, Any]] = {}
    if isinstance(system_prompts, dict):
        prompt_container = system_prompts.get("prompts", {})
        if isinstance(prompt_container, dict):
            for key, value in prompt_container.items():
                if isinstance(value, dict):
                    prompt_map[str(key)] = value
        elif isinstance(prompt_container, list):
            for item in prompt_container:
                if isinstance(item, dict):
                    key = str(item.get("id") or item.get("name") or "").strip()
                    if key:
                        prompt_map[key] = item
    elif isinstance(system_prompts, list):
        for item in system_prompts:
            if isinstance(item, dict):
                key = str(item.get("id") or item.get("name") or "").strip()
                if key:
                    prompt_map[key] = item
    return prompt_map


def resolve_selected_prompt_id(
    settings: Dict[str, Any],
    system_prompts: Dict[str, Any] | List[Any],
) -> str:
    prompt_map = normalize_prompt_map(system_prompts)
    prompt_id = str(settings.get("system_prompt_id") or "").strip()

    if prompt_id:
        if prompt_id in prompt_map:
            return prompt_id
        for key, item in prompt_map.items():
            if str(item.get("name", "")).strip() == prompt_id:
                return key

    current_prompt = ""
    if isinstance(system_prompts, dict):
        current_prompt = str(system_prompts.get("current_prompt") or "").strip()
        if current_prompt:
            if current_prompt in prompt_map:
                return current_prompt
            for key, item in prompt_map.items():
                if str(item.get("name", "")).strip() == current_prompt:
                    return key

    for key, item in prompt_map.items():
        if bool(item.get("is_default")):
            return key

    return next(iter(prompt_map), "")


def resolve_system_prompt(
    settings: Dict[str, Any],
    system_prompts: Dict[str, Any] | List[Any],
) -> Tuple[str, str]:
    custom_prompt = str(settings.get("custom_prompt") or "").strip()
    if custom_prompt:
        return "", custom_prompt

    prompt_map = normalize_prompt_map(system_prompts)
    selected_prompt_id = resolve_selected_prompt_id(settings, system_prompts)
    if selected_prompt_id:
        prompt = prompt_map.get(selected_prompt_id, {})
        if isinstance(prompt, dict):
            content = str(prompt.get("content") or "").strip()
            if content:
                return selected_prompt_id, content

    return "", str(settings.get("system_prompt") or "").strip()
