from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from modules.semantic_llm_validator import strip_added_punctuation
from modules.semantic_models import SemanticDocument, SemanticSegment
from modules.srt_parser import SRTParser

PUNCTUATION_CHARS = set("。！？、，,.!?…；;：「」『』（）()【】[]〈〉《》 \t\r\n")


def _is_punctuation(ch: str) -> bool:
    return ch in PUNCTUATION_CHARS


def _distribute_segment_display_words(raw_words: Sequence[str], segment_text: str) -> list[str]:
    if not raw_words:
        return []
    buffers = ['' for _ in raw_words]
    word_index = 0
    char_index = 0
    last_word_index = 0
    for ch in str(segment_text or ''):
        if _is_punctuation(ch):
            target_index = last_word_index if buffers[last_word_index] else min(word_index, len(raw_words) - 1)
            buffers[target_index] += ch
            continue
        while word_index < len(raw_words) and char_index >= len(raw_words[word_index]):
            word_index += 1
            char_index = 0
        if word_index >= len(raw_words):
            break
        buffers[word_index] += ch
        last_word_index = word_index
        char_index += 1
    return [buffer or raw_words[index] for index, buffer in enumerate(buffers)]


def _build_top_level_display_words(
    document: SemanticDocument,
    segments: Sequence[SemanticSegment],
) -> list[str]:
    raw_words = [word.text for word in document.raw_words]
    display_words = list(raw_words)
    raw_index = 0
    for segment in segments:
        plain_text = strip_added_punctuation(segment.ja_text)
        if not plain_text:
            continue
        start_index = raw_index
        collected = ''
        while raw_index < len(raw_words) and len(collected) < len(plain_text):
            collected += raw_words[raw_index]
            raw_index += 1
            if collected == plain_text:
                break
        if collected != plain_text:
            continue
        segment_raw_words = raw_words[start_index:raw_index]
        segment_display_words = _distribute_segment_display_words(segment_raw_words, segment.ja_text)
        for offset, display_word in enumerate(segment_display_words):
            display_words[start_index + offset] = display_word
    return display_words


def _build_word_payload(
    document: SemanticDocument,
    segments: Sequence[SemanticSegment],
) -> list[dict[str, float | str]]:
    display_words = _build_top_level_display_words(document, segments)
    payload = []
    for index, word in enumerate(document.raw_words):
        payload.append(
            {
                'word': word.text,
                'display_word': display_words[index],
                'start': round(word.start, 3),
                'end': round(word.end, 3),
            }
        )
    return payload


def _segment_words(
    document: SemanticDocument,
    segment: SemanticSegment,
) -> list[dict[str, float | str]]:
    if not segment.source_word_range:
        return []
    start_idx, end_idx = segment.source_word_range
    words = document.raw_words[start_idx : end_idx + 1]
    return [
        {
            'word': word.text,
            'start': round(word.start, 3),
            'end': round(word.end, 3),
        }
        for word in words
    ]


def build_semantic_srt_blocks(
    segments: Sequence[SemanticSegment],
) -> list[dict[str, str | int]]:
    return [
        {
            'index': index,
            'start_time': SRTParser.milliseconds_to_time(int(segment.start * 1000)),
            'end_time': SRTParser.milliseconds_to_time(int(segment.end * 1000)),
            'text': segment.ja_text,
        }
        for index, segment in enumerate(segments, start=1)
    ]


def build_semantic_whisper_payload(
    document: SemanticDocument,
    segments: Sequence[SemanticSegment],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extra_metadata = dict(metadata or {})
    source_metadata = dict(document.metadata or {})
    payload_segments: list[dict[str, Any]] = []
    for index, segment in enumerate(segments, start=1):
        payload_segments.append(
            {
                'id': index,
                'start': round(segment.start, 3),
                'end': round(segment.end, 3),
                'text': segment.ja_text,
                'words': _segment_words(document, segment),
                'source_word_range': list(segment.source_word_range) if segment.source_word_range else None,
                'segmentation_source': segment.segmentation_source,
                'risk_flags': list(segment.risk_flags),
            }
        )
    text_value = ''.join(segment.ja_text for segment in segments).strip()
    if not text_value:
        text_value = document.metadata.get('source_text') or document.full_text
    return {
        'language': document.language or 'ja',
        'text': text_value,
        'words': _build_word_payload(document, segments),
        'segments': payload_segments,
        'semantic_metadata': {
            'source_metadata': source_metadata,
            **extra_metadata,
        },
    }


def export_semantic_artifacts(
    document: SemanticDocument,
    segments: Sequence[SemanticSegment],
    srt_path: Path,
    json_path: Path,
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    parser = SRTParser()
    srt_blocks = build_semantic_srt_blocks(segments)
    srt_content = parser.rebuild(srt_blocks)
    payload = build_semantic_whisper_payload(document, segments, metadata)
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(srt_content, encoding='utf-8')
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return srt_path, json_path
