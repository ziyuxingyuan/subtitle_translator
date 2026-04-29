from __future__ import annotations

import json
import re
from typing import Callable

import requests

from modules.api_manager import APIManager
from modules.segmentation_engine import SegmentationConfig


Requester = Callable[[list[dict[str, str]]], str]
THINK_BLOCK_RE = re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE)
REQUEST_MAX_TOKENS = 800


def _normalize_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get('text') if item.get('type') == 'text' else item.get('content')
                if text:
                    parts.append(str(text))
            elif isinstance(item, str):
                parts.append(item)
        return ''.join(parts)
    return str(content or '')


def _strip_think_blocks(text: str) -> str:
    return THINK_BLOCK_RE.sub('', text).strip()


def _extract_stream_text(response: requests.Response) -> str:
    parts: list[str] = []
    raw = response.content.decode('utf-8', errors='replace')
    for line in raw.splitlines():
        if not line.startswith('data: '):
            continue
        payload_line = line[6:].strip()
        if not payload_line or payload_line == '[DONE]':
            continue
        item = json.loads(payload_line)
        choices = item.get('choices') or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get('delta') or {}
        message = choice.get('message') or {}
        content = delta.get('content')
        if content is None:
            content = message.get('content')
        if content is not None:
            parts.append(_normalize_content(content))
    return _strip_think_blocks(''.join(parts))


def _extract_response_text(response: requests.Response) -> str:
    content_type = (response.headers.get('content-type') or '').lower()
    if 'text/event-stream' in content_type:
        return _extract_stream_text(response)
    payload = response.json()
    choices = payload.get('choices') or []
    if not choices:
        raise RuntimeError(f'\u8bed\u4e49 LLM \u54cd\u5e94\u7f3a\u5c11 choices: {payload}')
    message = choices[0].get('message') or {}
    return _strip_think_blocks(_normalize_content(message.get('content', '')))


def _build_proxies(config: SegmentationConfig) -> dict[str, str | None]:
    if config.proxy_enabled and config.proxy_address.strip():
        address = config.proxy_address.strip()
        return {'http': address, 'https': address}
    return {'http': None, 'https': None}


def build_semantic_requester(config: SegmentationConfig) -> Requester:
    api_manager = APIManager()
    headers = api_manager.get_auth_headers(config.provider, config.api_key)
    headers['Content-Type'] = 'application/json'
    endpoint = f"{config.endpoint.rstrip('/')}/chat/completions"
    proxies = _build_proxies(config)

    def requester(messages: list[dict[str, str]]) -> str:
        payload = {
            'model': config.model,
            'messages': messages,
            'temperature': float(config.temperature),
            'max_tokens': REQUEST_MAX_TOKENS,
            'stream': False,
        }
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=(15, max(30, int(config.timeout_seconds))),
            proxies=proxies,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f'\u8bed\u4e49 LLM \u8bf7\u6c42\u5931\u8d25 (HTTP {response.status_code}): {response.text[:400]}'
            )
        return _extract_response_text(response)

    return requester
