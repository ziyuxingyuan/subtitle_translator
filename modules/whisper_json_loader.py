from __future__ import annotations

import json
from typing import Any, Iterable, List

from modules.semantic_models import RawSegment, SemanticDocument, SemanticWord, build_full_text
from modules.srt_parser import SRTParser


_SEGMENT_TEXT_KEYS = ("text", "sentence", "content")
_START_KEYS = ("start", "startTime", "start_time")
_END_KEYS = ("end", "endTime", "end_time")
_WORD_TEXT_KEYS = ("word", "text", "token")


def _first_value(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _parse_time_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        if ":" in value:
            return value
        try:
            value = float(value)
        except Exception:
            return None
    if isinstance(value, (int, float)):
        return SRTParser.milliseconds_to_time(int(float(value) * 1000))
    return None


def _coerce_word(word: dict[str, Any]) -> SemanticWord | None:
    text = _first_value(word, _WORD_TEXT_KEYS)
    start = _first_value(word, _START_KEYS)
    end = _first_value(word, _END_KEYS)
    if end is None and start is not None and word.get("duration") is not None:
        try:
            end = float(start) + float(word["duration"])
        except Exception:
            end = None
    if not isinstance(text, str):
        return None
    try:
        start_f = float(start)
        end_f = float(end)
    except Exception:
        return None
    if not text.strip() or end_f < start_f:
        return None
    return SemanticWord(text=text.strip(), start=start_f, end=end_f)


def _collect_words(payload: dict[str, Any]) -> List[SemanticWord]:
    collected: List[SemanticWord] = []
    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        has_segment_words = False
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            words = segment.get("words")
            if isinstance(words, list) and words:
                has_segment_words = True
                for word in words:
                    if isinstance(word, dict):
                        item = _coerce_word(word)
                        if item:
                            collected.append(item)
        if has_segment_words:
            return collected
    words = payload.get("words")
    if isinstance(words, list):
        for word in words:
            if isinstance(word, dict):
                item = _coerce_word(word)
                if item:
                    collected.append(item)
    return collected


def _coerce_segment(segment: dict[str, Any], index: int) -> RawSegment | None:
    text = _first_value(segment, _SEGMENT_TEXT_KEYS)
    if not isinstance(text, str) or not text.strip():
        return None
    start = _parse_time_string(_first_value(segment, _START_KEYS)) or "00:00:00,000"
    end = _parse_time_string(_first_value(segment, _END_KEYS)) or "00:00:00,000"
    return RawSegment(index=index, time_start=start, time_end=end, text=text.strip())


def _collect_segments(payload: dict[str, Any]) -> List[RawSegment]:
    segments = payload.get("segments")
    if not isinstance(segments, list):
        return []
    collected: List[RawSegment] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        item = _coerce_segment(segment, len(collected) + 1)
        if item:
            collected.append(item)
    return collected


def _build_base_segment(words: List[SemanticWord], full_text: str) -> RawSegment | None:
    if not words or not full_text.strip():
        return None
    start = SRTParser.milliseconds_to_time(int(words[0].start * 1000))
    end = SRTParser.milliseconds_to_time(int(words[-1].end * 1000))
    return RawSegment(index=1, time_start=start, time_end=end, text=full_text)


def load_whisper_json_content(content: str) -> SemanticDocument:
    try:
        payload = json.loads(content)
    except Exception as exc:
        raise ValueError(f"转录 JSON 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("转录 JSON 根对象必须是对象")

    raw_words = _collect_words(payload)
    raw_segments = _collect_segments(payload)
    full_text = build_full_text(raw_words, raw_segments)
    if not raw_words and not raw_segments:
        raise ValueError("JSON 中未找到可用的词级时间戳或分段数据")
    if not raw_segments:
        base = _build_base_segment(raw_words, full_text)
        raw_segments = [base] if base else []

    metadata = {}
    source_text = payload.get("text")
    if isinstance(source_text, str):
        metadata["source_text"] = source_text
    return SemanticDocument(
        raw_words=tuple(raw_words),
        raw_segments=tuple(raw_segments),
        full_text=full_text,
        language=str(payload.get("language") or ""),
        metadata=metadata,
    )
