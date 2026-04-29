from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, List

from modules.segmentation_engine import SRTSegment
from modules.srt_parser import SRTParser


@dataclass
class PostProcessOptions:
    min_duration_ms: int = 1200
    max_duration_ms: int = 12000
    min_gap_ms: int = 100
    max_chars_per_line: int = 60
    merge_gap_ms: int = 1000


_SPLIT_REGEX = re.compile(r"([。！？\?]|\.{3,}|…)")


def post_process_segments(
    raw: List[SRTSegment],
    options: PostProcessOptions | None = None,
) -> tuple[List[SRTSegment], List[str]]:
    opts = options or PostProcessOptions()
    logs: List[str] = []

    def log(msg: str) -> None:
        logs.append(msg)

    log("--- Stage1: 时长/再分段/间距 ---")
    stage1 = _stage1_adjust(raw, opts, log)

    log("--- Stage2: 合并相邻短条 ---")
    stage2 = _stage2_merge(stage1, opts, log)

    log("--- Stage3: 最终格式化 ---")
    stage3 = _stage3_finalize(stage2, log)

    if not logs:
        logs.append("后处理完成（无变化）")
    return stage3, logs


def _stage1_adjust(
    raw: List[SRTSegment],
    opts: PostProcessOptions,
    log: Callable[[str], None],
) -> List[SRTSegment]:
    out: List[SRTSegment] = []
    for seg in raw:
        start = SRTParser.time_to_milliseconds(seg["time_start"])
        end = SRTParser.time_to_milliseconds(seg["time_end"])
        if end <= start:
            end = start + opts.min_duration_ms
        local = {
            **seg,
            "time_start": SRTParser.milliseconds_to_time(start),
            "time_end": SRTParser.milliseconds_to_time(end),
        }
        if _duration(local) > opts.max_duration_ms or len(local["text"]) > opts.max_chars_per_line * 2:
            splits = _split_by_punctuation(local, opts)
            if len(splits) > 1:
                log(f"Stage1: 超限再分段 -> {len(splits)} 段 | \"{local['text'][:30]}...\"")
            for split in splits:
                out.append(_clamp_duration(split, opts))
        else:
            out.append(_clamp_duration(local, opts))

    max_safe_shift_ms = 350
    following_buffer_ms = 100
    gapped: List[SRTSegment] = []
    for i, seg in enumerate(out):
        start = SRTParser.time_to_milliseconds(seg["time_start"])
        end = SRTParser.time_to_milliseconds(seg["time_end"])
        if i > 0:
            prev_end = SRTParser.time_to_milliseconds(gapped[i - 1]["time_end"])
            needed = prev_end + opts.min_gap_ms - start
            if needed > 0:
                shift = min(needed, max_safe_shift_ms)
                if i + 1 < len(out):
                    next_start = SRTParser.time_to_milliseconds(out[i + 1]["time_start"])
                    max_shift = max(0, next_start - following_buffer_ms - start)
                    shift = min(shift, max_shift)
                if shift > 0:
                    start += shift
                    end += shift
                    log(f"Stage1: 间距不足，后移 {shift} ms | \"{seg['text'][:20]}\"")
                else:
                    log(f"Stage1: 间距不足但跳过调整 | \"{seg['text'][:20]}\"")
        gapped.append(
            {
                **seg,
                "time_start": SRTParser.milliseconds_to_time(start),
                "time_end": SRTParser.milliseconds_to_time(end),
            }
        )
    return gapped


def _stage2_merge(
    segs: List[SRTSegment],
    opts: PostProcessOptions,
    log: Callable[[str], None],
) -> List[SRTSegment]:
    if not segs:
        return []
    out: List[SRTSegment] = [segs[0]]
    for curr in segs[1:]:
        prev = out[-1]
        gap = (
            SRTParser.time_to_milliseconds(curr["time_start"])
            - SRTParser.time_to_milliseconds(prev["time_end"])
        )
        can_merge = (
            gap >= 0
            and gap <= opts.merge_gap_ms
            and _duration(prev) < opts.min_duration_ms * 1.2
            and _duration(curr) < opts.min_duration_ms * 1.2
        )
        if can_merge:
            merged = {
                "index": 0,
                "time_start": prev["time_start"],
                "time_end": curr["time_end"],
                "text": f"{prev['text']} {curr['text']}".strip(),
            }
            log(f"Stage2: 合并相邻短条 | gap={gap}ms | \"{merged['text'][:30]}...\"")
            out[-1] = merged
        else:
            out.append(curr)
    return out


def _stage3_finalize(segs: List[SRTSegment], log: Callable[[str], None]) -> List[SRTSegment]:
    finalized = [
        {**seg, "index": idx + 1}
        for idx, seg in enumerate(segs)
    ]
    log(f"Stage3: 格式化完成，条目数 {len(finalized)}")
    return finalized


def _duration(seg: SRTSegment) -> int:
    return max(
        0,
        SRTParser.time_to_milliseconds(seg["time_end"])
        - SRTParser.time_to_milliseconds(seg["time_start"]),
    )


def _clamp_duration(seg: SRTSegment, opts: PostProcessOptions) -> SRTSegment:
    start = SRTParser.time_to_milliseconds(seg["time_start"])
    end = SRTParser.time_to_milliseconds(seg["time_end"])
    if end <= start:
        end = start + opts.min_duration_ms
    if end - start < opts.min_duration_ms:
        end = start + opts.min_duration_ms
    return {
        **seg,
        "time_start": SRTParser.milliseconds_to_time(start),
        "time_end": SRTParser.milliseconds_to_time(end),
    }


def _split_by_punctuation(seg: SRTSegment, opts: PostProcessOptions) -> List[SRTSegment]:
    raw_parts = [part for part in _SPLIT_REGEX.split(seg["text"]) if part]
    if len(raw_parts) <= 1:
        return [seg]

    merged_parts: List[str] = []
    for part in raw_parts:
        trimmed = part.strip()
        if not trimmed:
            continue
        is_punc = bool(re.fullmatch(r"[。！？\?…\.]+", trimmed))
        if is_punc and merged_parts:
            merged_parts[-1] += trimmed
        elif trimmed:
            merged_parts.append(trimmed)

    if not merged_parts:
        return [seg]

    start = SRTParser.time_to_milliseconds(seg["time_start"])
    end = SRTParser.time_to_milliseconds(seg["time_end"])
    total_duration = max(end - start, opts.min_duration_ms)
    total_len = sum(len(part) for part in merged_parts) or 1

    cursor = start
    results: List[SRTSegment] = []
    for part in merged_parts:
        frac = len(part) / total_len
        seg_duration = max(opts.min_duration_ms, min(opts.max_duration_ms, int(total_duration * frac)))
        seg_start = cursor
        seg_end = min(end, seg_start + seg_duration)
        results.append(
            {
                "index": 0,
                "time_start": SRTParser.milliseconds_to_time(seg_start),
                "time_end": SRTParser.milliseconds_to_time(seg_end),
                "text": part,
            }
        )
        cursor = seg_end

    return results
