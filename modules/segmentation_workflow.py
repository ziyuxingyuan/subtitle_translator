from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, List

from modules.segmentation_engine import (
    SemanticSegmentationEngine,
    SegmentationConfig,
    SegmentationInput,
    SRTSegment,
    WordToken,
)
from modules.segmentation_post_processor import PostProcessOptions, post_process_segments
from modules.whisper_json_loader import load_whisper_json_content
from modules.srt_parser import SRTParser
from modules.config_paths import get_target_config_dir


def _collect_word_tokens(data: dict) -> List[WordToken]:
    collected: List[WordToken] = []

    def push_word(word: dict) -> None:
        text = word.get("word") or word.get("text")
        start = word.get("start") if "start" in word else word.get("startTime")
        end = word.get("end") if "end" in word else word.get("endTime")
        if end is None and start is not None and word.get("duration") is not None:
            end = start + word["duration"]
        if isinstance(text, str) and isinstance(start, (int, float)) and isinstance(end, (int, float)):
            collected.append({"text": text, "start": float(start), "end": float(end)})

    segments = data.get("segments")
    if isinstance(segments, list) and segments:
        has_words = False
        for seg in segments:
            words = seg.get("words") if isinstance(seg, dict) else None
            if isinstance(words, list) and words:
                has_words = True
                for word in words:
                    if isinstance(word, dict):
                        push_word(word)
        if not has_words:
            words = data.get("words")
            if isinstance(words, list):
                for word in words:
                    if isinstance(word, dict):
                        push_word(word)
    else:
        words = data.get("words")
        if isinstance(words, list):
            for word in words:
                if isinstance(word, dict):
                    push_word(word)

    return collected


def _parse_time_value(value: object) -> str | None:
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


def _collect_segments(data: dict) -> List[SRTSegment]:
    segments = data.get("segments")
    if not isinstance(segments, list):
        return []

    parsed: List[SRTSegment] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = seg.get("text") or seg.get("sentence") or seg.get("content") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        start = seg.get("start") if "start" in seg else seg.get("startTime")
        end = seg.get("end") if "end" in seg else seg.get("endTime")
        if start is None:
            start = seg.get("start_time")
        if end is None:
            end = seg.get("end_time")
        time_start = _parse_time_value(start) or "00:00:00,000"
        time_end = _parse_time_value(end) or "00:00:00,000"
        parsed.append(
            {
                "index": len(parsed) + 1,
                "time_start": time_start,
                "time_end": time_end,
                "text": text.strip(),
            }
        )
    return parsed


def _build_full_text(word_tokens: List[WordToken], segments: List[SRTSegment]) -> str:
    if word_tokens:
        return " ".join(word["text"] for word in word_tokens)
    if segments:
        return "\n".join(seg["text"] for seg in segments if seg.get("text"))
    return ""


def _write_llm_segmentation_debug(
    segmentation: List[SRTSegment],
    original_segments: List[SRTSegment],
    config: SegmentationConfig,
) -> None:
    if not config.debug_mode:
        return
    try:
        task_id = config.debug_task_id or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        batch_index = int(config.debug_batch_index or 0)
        debug_dir = get_target_config_dir().parent / "debug_files"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_file = debug_dir / f"segmentation_llm_{task_id}_batch_{batch_index}.txt"
        lines: List[str] = [
            f"original_segments={len(original_segments)}",
            f"llm_segments={len(segmentation)}",
        ]
        for idx, seg in enumerate(segmentation, start=1):
            text = str(seg.get("text", "")).replace("\n", " ").strip()
            lines.append(f"{idx}: {text}")
        debug_file.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        return


def parse_transcript_json_with_segments(
    content: str,
) -> tuple[List[SRTSegment], List[WordToken], str]:
    document = load_whisper_json_content(content)
    word_tokens = [word.to_engine_token() for word in document.raw_words]
    segments = [segment.to_engine_segment() for segment in document.raw_segments]
    return segments, word_tokens, document.full_text


def parse_transcript_json(content: str) -> tuple[List[WordToken], str]:
    segments, word_tokens, full_text = parse_transcript_json_with_segments(content)
    if not word_tokens:
        raise ValueError("JSON 中未找到可用的词级时间戳")
    return word_tokens, full_text


def build_base_segments(word_tokens: List[WordToken], full_text: str) -> List[SRTSegment]:
    start_ms = int(word_tokens[0]["start"] * 1000)
    end_ms = int(word_tokens[-1]["end"] * 1000)
    return [
        {
            "index": 1,
            "time_start": SRTParser.milliseconds_to_time(start_ms),
            "time_end": SRTParser.milliseconds_to_time(end_ms),
            "text": full_text,
        }
    ]


def segment_with_alignment(
    segments: List[SRTSegment],
    word_tokens: List[WordToken] | None,
    full_text: str | None,
    segmentation_config: SegmentationConfig,
    engine: SemanticSegmentationEngine | None = None,
    log_func: Callable[[str], None] | None = None,
) -> tuple[List[SRTSegment], List[str]]:
    logs: List[str] = []

    def log(message: str) -> None:
        logs.append(message)
        if log_func:
            log_func(message)

    engine = engine or SemanticSegmentationEngine()
    input_data = SegmentationInput(
        original_segments=segments,
        word_tokens=word_tokens,
        full_text=full_text,
    )

    log(f"语义分段开始: segments={len(segments)}, tokens={len(word_tokens or [])}")
    segmentation = engine.segment(input_data, segmentation_config)
    log(f"语义分段完成，返回 {len(segmentation)} 段")

    effective = segmentation
    if not segmentation:
        _write_llm_segmentation_debug(segmentation, segments, segmentation_config)
        log("分段结果为空，回退使用原始分段文本")
        effective = [
            {
                "index": idx + 1,
                "time_start": seg.get("time_start", "00:00:00,000"),
                "time_end": seg.get("time_end", "00:00:00,000"),
                "text": seg.get("text", ""),
            }
            for idx, seg in enumerate(segments)
        ]

    segmentation_texts = [seg["text"] for seg in effective if seg.get("text")]

    if word_tokens:
        log("使用词级时间戳进行轻量对齐")
        fallback_times = [
            (
                seg.get("time_start", "00:00:00,000"),
                seg.get("time_end", "00:00:00,000"),
            )
            for seg in segments
        ]
        processed = engine.align_with_tokens_strict(
            segmentation_texts,
            word_tokens,
            fallback_times=fallback_times,
        )
    else:
        log("缺少词级时间戳，使用原始时间轴")
        processed = [
            {
                "index": idx + 1,
                "time_start": seg.get("time_start", "00:00:00,000"),
                "time_end": seg.get("time_end", "00:00:00,000"),
                "text": seg.get("text", ""),
            }
            for idx, seg in enumerate(effective)
        ]

    log(f"对齐完成，最终片段数 {len(processed)}")
    return processed, logs


def smart_segment_and_save(
    segments: List[SRTSegment],
    word_tokens: List[WordToken] | None,
    full_text: str | None,
    segmentation_config: SegmentationConfig,
    pre_output_path: Path,
    engine: SemanticSegmentationEngine | None = None,
    log_func: Callable[[str], None] | None = None,
) -> tuple[List[SRTSegment], List[str]]:
    processed, logs = segment_with_alignment(
        segments=segments,
        word_tokens=word_tokens,
        full_text=full_text,
        segmentation_config=segmentation_config,
        engine=engine,
        log_func=log_func,
    )

    parser = SRTParser()
    blocks = [
        {
            "index": seg["index"],
            "start_time": seg["time_start"],
            "end_time": seg["time_end"],
            "text": seg["text"],
        }
        for seg in processed
    ]
    srt_content = parser.rebuild(blocks)
    pre_output_path.parent.mkdir(parents=True, exist_ok=True)
    pre_output_path.write_text(srt_content, encoding="utf-8")
    logs.append(f"预处理文件已生成: {pre_output_path}")
    if log_func:
        log_func(f"预处理文件已生成: {pre_output_path}")
    return processed, logs


def segment_and_postprocess(
    segments: List[SRTSegment],
    word_tokens: List[WordToken] | None,
    full_text: str | None,
    segmentation_config: SegmentationConfig,
    post_options: PostProcessOptions | None = None,
    engine: SemanticSegmentationEngine | None = None,
    log_func: Callable[[str], None] | None = None,
) -> tuple[List[SRTSegment], List[str]]:
    processed, logs = segment_with_alignment(
        segments=segments,
        word_tokens=word_tokens,
        full_text=full_text,
        segmentation_config=segmentation_config,
        engine=engine,
        log_func=log_func,
    )

    post_segments, post_logs = post_process_segments(processed, post_options)
    logs.extend(post_logs)
    if log_func:
        for entry in post_logs:
            log_func(entry)
    return post_segments, logs
