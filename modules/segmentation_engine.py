from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Dict, List, TypedDict

import requests

from modules.api_manager import APIManager
from modules.config_paths import get_target_config_dir
from modules.rate_limiter import RateLimiter
from modules.srt_parser import SRTParser


class WordToken(TypedDict):
    text: str
    start: float
    end: float


class SRTSegment(TypedDict):
    index: int
    time_start: str
    time_end: str
    text: str


@dataclass
class SegmentationInput:
    original_segments: List[SRTSegment]
    word_tokens: List[WordToken] | None = None
    full_text: str | None = None


@dataclass
class SegmentationConfig:
    provider: str
    model: str
    api_key: str
    endpoint: str
    max_chars_per_chunk: int = 4000
    enable_summary: bool = True
    temperature: float = 0.2
    timeout_seconds: int = 180
    max_retries: int = 1
    fallback_on_failure: bool = False
    batch_concurrency: int = 2
    batch_cache_dir: str = ""
    provider_limits: Dict[str, int] = field(default_factory=dict)
    proxy_enabled: bool = False
    proxy_address: str = ""
    debug_mode: bool = False
    debug_task_id: str = ""
    debug_batch_index: int = 0
    resume_enabled: bool = True
    stop_event: threading.Event | None = None


class UserStoppedException(Exception):
    """Raised when segmentation stops because the user cancelled."""


_DEFAULT_SEGMENTATION_PROMPTS: Dict[str, str] = {
    "universal": (
        "Important: Your primary task is to accurately segment the "
        "\"Current Text Block\". You may also receive a \"Full Text Summary\" "
        "for context. Only output the segmented fragments, one per line. "
        "Do NOT add or delete any characters; keep the original content and order.\n\n"
        "- Remove accidental spaces caused by formatting; keep intentional spaces.\n"
        "- Parentheses/brackets as standalone segments; split consecutive brackets.\n"
        "- Quoted content stays as one segment.\n"
        "- Sentence starters/fillers may stand alone only at sentence start.\n"
        "- Main split points after strong punctuation (. ! ? … ;), keep punctuation.\n"
        "- Integrity: concatenating outputs must exactly match the original block."
    )
}

_DEFAULT_SUMMARY_PROMPTS: Dict[str, str] = {
    "universal": (
        "Please analyze the content of the text below (which may be in any language) "
        "and generate a concise summary of around 100-150 words in the SAME LANGUAGE "
        "as the input text (or English if the language is obscure). This summary is "
        "only for context in segmentation tasks. Do not include verbatim details."
    )
}


def _load_prompt_config() -> tuple[Dict[str, str], Dict[str, str]]:
    prompt_path = Path(__file__).with_name("segmentation_prompts.json")
    if not prompt_path.exists():
        return _DEFAULT_SEGMENTATION_PROMPTS, _DEFAULT_SUMMARY_PROMPTS
    try:
        data = json.loads(prompt_path.read_text(encoding="utf-8"))
        segmentation_prompts = data.get("segmentation_prompts") or {}
        summary_prompts = data.get("summary_prompts") or {}
        if isinstance(segmentation_prompts, dict) and isinstance(summary_prompts, dict):
            return segmentation_prompts, summary_prompts
    except Exception:
        pass
    return _DEFAULT_SEGMENTATION_PROMPTS, _DEFAULT_SUMMARY_PROMPTS


SEGMENTATION_PROMPTS, SUMMARY_PROMPTS = _load_prompt_config()


def detect_language(text: str | None) -> str:
    if not text:
        return "universal"
    if re.search(r"[\u3040-\u30ff\u31f0-\u31ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    if re.search(r"[\u4e00-\u9fa5]", text):
        return "zh"
    return "en"


def normalize_for_comparison(text: str) -> str:
    normalized = re.sub(r"\s+", "", text)
    normalized = (normalized.replace("！", "!")
                  .replace("？", "?")
                  .replace("，", ",")
                  .replace("。", ".")
                  .replace("、", ","))
    normalized = re.sub(r"[…。]{2,}|\.{3,}", "…", normalized)
    normalized = re.sub(r"(.)\1{2,}", r"\1\1", normalized)
    return normalized


def sequence_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    len_a = len(a)
    len_b = len(b)
    if len_a == 0 or len_b == 0:
        return 0.0

    prev = [0] * (len_b + 1)
    curr = [0] * (len_b + 1)
    for i in range(1, len_a + 1):
        char_a = a[i - 1]
        for j in range(1, len_b + 1):
            if char_a == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = prev[j] if prev[j] > curr[j - 1] else curr[j - 1]
        prev, curr = curr, [0] * (len_b + 1)

    lcs = prev[len_b]
    return (2 * lcs) / (len_a + len_b)


class SemanticSegmentationEngine:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("segmentation")
        self._api_manager = APIManager()
        self._rate_limiter = RateLimiter()
        self._session = requests.Session()

    def _hash_text(self, text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _get_resume_cache_path(self, resume_key: str) -> Path:
        cache_root = get_target_config_dir().parent / "segmentation_resume"
        cache_root.mkdir(parents=True, exist_ok=True)
        return cache_root / f"segmentation_{resume_key}.json"

    def _load_resume_state(self, cache_path: Path) -> Dict[str, object]:
        if not cache_path.exists():
            return {}
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_resume_state(self, cache_path: Path, state: Dict[str, object]) -> None:
        cache_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def _clear_resume_state(self, cache_path: Path) -> None:
        try:
            cache_path.unlink(missing_ok=True)
        except Exception:
            return

    def segment(self, input_data: SegmentationInput, config: SegmentationConfig) -> List[SRTSegment]:
        if not input_data.original_segments:
            return []

        base_text = (input_data.full_text or "").strip()
        self._logger.debug(
            "📊 segment() 开始: original_segments=%s, full_text_len=%s",
            len(input_data.original_segments),
            len(input_data.full_text or ""),
        )
        if not base_text:
            base_text = "".join(seg.get("text", "") for seg in input_data.original_segments)
        self._logger.debug("📊 base_text_len=%s", len(base_text))

        if not base_text.strip():
            self._logger.debug("⚠️ base_text 为空，直接回退原始分段")
            return [
                {
                    "index": idx + 1,
                    "time_start": seg.get("time_start", "00:00:00,000"),
                    "time_end": seg.get("time_end", "00:00:00,000"),
                    "text": seg.get("text", ""),
                }
                for idx, seg in enumerate(input_data.original_segments)
            ]

        lang = detect_language(base_text)
        seg_prompt = SEGMENTATION_PROMPTS.get(lang, SEGMENTATION_PROMPTS.get("universal", ""))
        summary_prompt = SUMMARY_PROMPTS.get(lang, SUMMARY_PROMPTS.get("universal", ""))
        self._logger.debug("📊 语言检测=%s, enable_summary=%s", lang, config.enable_summary)

        max_chars = max(500, min(6000, int(config.max_chars_per_chunk or 4000)))
        chunks = self._split_text_prefer_two(base_text, max_chars)
        self._logger.debug("📊 文本切分完成，chunks=%s, max_chars=%s", len(chunks), max_chars)

        resume_state: Dict[str, object] = {}
        resume_cache_path: Path | None = None
        resume_lock = threading.Lock()
        summary_text = ""

        if config.resume_enabled:
            base_hash = self._hash_text(base_text)
            prompt_hash = self._hash_text(seg_prompt + "|" + summary_prompt)
            resume_key = self._hash_text(f"{base_hash}|{prompt_hash}|{config.model}|{max_chars}")
            resume_cache_path = self._get_resume_cache_path(resume_key)
            resume_state = self._load_resume_state(resume_cache_path)
            if (
                resume_state.get("base_hash") != base_hash
                or resume_state.get("prompt_hash") != prompt_hash
                or resume_state.get("chunk_count") != len(chunks)
                or resume_state.get("max_chars") != max_chars
            ):
                resume_state = {}
            else:
                cached_summary = resume_state.get("summary_text")
                if isinstance(cached_summary, str):
                    summary_text = cached_summary
                    self._logger.debug("⏩ 断点续跑: 使用缓存摘要")

        if config.enable_summary and not summary_text:
            self._logger.debug("📊 摘要生成开始")
            summary_text = self.get_summary(base_text, config, summary_prompt)
            self._logger.debug("📊 摘要生成完成，长度=%s", len(summary_text))
        if resume_cache_path and not resume_state:
            resume_state = {
                "base_hash": self._hash_text(base_text),
                "prompt_hash": self._hash_text(seg_prompt + "|" + summary_prompt),
                "model": config.model,
                "max_chars": max_chars,
                "chunk_count": len(chunks),
                "summary_text": summary_text,
                "chunks": {},
            }
            self._save_resume_state(resume_cache_path, resume_state)

        segmented_lines: List[str] = []

        def get_cached_chunk(chunk_index: int) -> Dict[str, object] | None:
            if not resume_state:
                return None
            chunks_state = resume_state.get("chunks")
            if not isinstance(chunks_state, dict):
                return None
            cached = chunks_state.get(str(chunk_index))
            if not isinstance(cached, dict):
                return None
            if not isinstance(cached.get("lines"), list):
                return None
            if not isinstance(cached.get("similarity"), (int, float)):
                return None
            return {
                "ok": True,
                "lines": cached.get("lines"),
                "similarity": float(cached.get("similarity")),
                "cached": True,
            }

        def save_chunk_result(chunk_index: int, result: Dict[str, object]) -> None:
            if not resume_cache_path or not resume_state:
                return
            chunks_state = resume_state.get("chunks")
            if not isinstance(chunks_state, dict):
                return
            with resume_lock:
                chunks_state[str(chunk_index)] = {
                    "lines": result.get("lines", []),
                    "similarity": result.get("similarity", 0.0),
                }
                resume_state["chunks"] = chunks_state
                self._save_resume_state(resume_cache_path, resume_state)

        def process_chunk(chunk_index: int, chunk_text: str) -> Dict[str, object]:
            self._check_stop(config)
            cached = get_cached_chunk(chunk_index)
            if cached:
                self._logger.debug(
                    "⏩ 断点续跑命中 chunk=%s/%s lines=%s similarity=%.3f",
                    chunk_index + 1,
                    len(chunks),
                    len(cached["lines"]),
                    float(cached["similarity"]),
                )
                return cached

            self._logger.debug(
                "LLM 分段请求开始 chunk=%s/%s len=%s model=%s temp=%.2f summary=%s",
                chunk_index + 1,
                len(chunks),
                len(chunk_text),
                config.model,
                float(config.temperature),
                bool(config.enable_summary),
            )
            similarity_retries = max(0, int(config.max_retries or 0))
            attempt = 0
            result = self._segment_chunk(chunk_text, summary_text, seg_prompt, config)
            self._logger.debug(
                "LLM 分段完成 chunk=%s/%s lines=%s similarity=%.3f",
                chunk_index + 1,
                len(chunks),
                len(result["lines"]),
                result["similarity"],
            )
            while result["similarity"] < 0.90 and attempt < similarity_retries:
                attempt += 1
                self._logger.warning(
                    "相似度过低(%.3f)，将重试(%s/%s)",
                    result["similarity"],
                    attempt,
                    similarity_retries,
                )
                retry_config = SegmentationConfig(**{**config.__dict__, "temperature": 0.0})
                result = self._segment_chunk(
                    chunk_text,
                    summary_text,
                    seg_prompt,
                    retry_config,
                    is_retry=True,
                )
                self._logger.debug(
                    "🔄 重试完成 chunk=%s/%s similarity=%.3f lines=%s",
                    chunk_index + 1,
                    len(chunks),
                    result["similarity"],
                    len(result["lines"]),
                )

            if result["similarity"] < 0.90:
                raise RuntimeError(
                    f"语义分段相似度过低({result['similarity']:.3f})，已达到最大重试({similarity_retries})"
                )

            chunk_result = {"ok": True, "lines": result["lines"], "similarity": result["similarity"]}
            save_chunk_result(chunk_index, chunk_result)
            return chunk_result

        max_workers = min(3, len(chunks))
        if max_workers <= 1:
            for idx, chunk in enumerate(chunks):
                self._check_stop(config)
                result = process_chunk(idx, chunk)
                segmented_lines.extend(result["lines"])  # type: ignore[arg-type]
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            self._logger.debug("并发分段启用: workers=%s chunks=%s", max_workers, len(chunks))
            results: Dict[int, Dict[str, object]] = {}
            try:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {}
                    for idx, chunk in enumerate(chunks):
                        cached = get_cached_chunk(idx)
                        if cached:
                            results[idx] = cached
                            continue
                        future_map[executor.submit(process_chunk, idx, chunk)] = idx
                    for future in as_completed(future_map):
                        idx = future_map[future]
                        try:
                            results[idx] = future.result()
                        except UserStoppedException:
                            raise
                        except Exception:
                            raise
            finally:
                if config.stop_event and config.stop_event.is_set():
                    raise UserStoppedException("用户已停止语义分段。")

            for idx in range(len(chunks)):
                result = results.get(idx)
                if not result or not result.get("ok"):
                    raise RuntimeError(f"语义分段 chunk={idx + 1}/{len(chunks)} 失败")
                segmented_lines.extend(result["lines"])  # type: ignore[arg-type]

        if not segmented_lines:
            raise RuntimeError("语义分段失败：未生成任何分段结果")

        deduped: List[str] = []
        for line in segmented_lines:
            if not deduped or deduped[-1] != line:
                deduped.append(line)
        self._logger.debug("📊 去重完成，deduped_lines=%s", len(deduped))

        result_segments = [
            {
                "index": idx + 1,
                "time_start": "00:00:00,000",
                "time_end": "00:00:00,000",
                "text": text,
            }
            for idx, text in enumerate(deduped)
            if text
        ]
        self._logger.debug("📊 segment() 完成，返回=%s", len(result_segments))
        if resume_cache_path and resume_state:
            self._clear_resume_state(resume_cache_path)
        return result_segments

    def align_with_tokens_strict(
        self,
        lines: List[str],
        tokens: List[WordToken],
        fallback_times: List[tuple[str, str]] | None = None,
    ) -> List[SRTSegment]:
        backtrack = 50
        similarity_threshold = 0.75
        self._logger.debug("📊 对齐开始: lines=%s, tokens=%s", len(lines), len(tokens))

        align_results: List[Dict[str, int | str | bool]] = []
        token_idx = 0

        for line_idx, line in enumerate(lines):
            compact = normalize_for_comparison(re.sub(r"\s+", "", line))
            if not compact:
                continue

            best_match: Dict[str, int | float] | None = None
            search_start = max(0, token_idx - backtrack)

            for start_try in range(search_start, min(token_idx + 1, len(tokens))):
                built = ""
                for i in range(start_try, len(tokens)):
                    built += tokens[i]["text"]
                    if len(built) >= len(compact) * 0.7:
                        normalized_built = normalize_for_comparison(built)
                        ratio = sequence_similarity(compact, normalized_built)
                        is_match = (
                            ratio >= similarity_threshold
                            or normalized_built in compact
                            or compact in normalized_built
                        )
                        if is_match:
                            if not best_match or ratio > float(best_match["ratio"]):
                                best_match = {"start": start_try, "end": i, "ratio": ratio}
                            if ratio >= 0.95:
                                break
                        if len(built) > len(compact) * 1.5:
                            break
                if best_match and float(best_match["ratio"]) >= 0.95:
                    break

            if best_match:
                align_results.append(
                    {
                        "line_idx": line_idx,
                        "text": line,
                        "start": int(best_match["start"]),
                        "end": int(best_match["end"]),
                        "aligned": True,
                    }
                )
                self._logger.debug(
                    "✅ 对齐成功 #%s ratio=%.1f%% tokens[%s-%s]",
                    line_idx + 1,
                    float(best_match["ratio"]) * 100,
                    best_match["start"],
                    best_match["end"],
                )
                token_idx = int(best_match["end"]) + 1
            else:
                align_results.append(
                    {
                        "line_idx": line_idx,
                        "text": line,
                        "start": -1,
                        "end": -1,
                        "aligned": False,
                    }
                )
                self._logger.debug("⚠️ 对齐失败 #%s: %s", line_idx + 1, line[:30])

        final_results: List[SRTSegment] = []
        skipped = 0
        fallback_used = 0
        fallback_len = len(fallback_times) if fallback_times else 0
        min_interval_ms = 100
        fallback_schedule: Dict[int, List[int]] = {}
        fallback_times_by_line: Dict[int, tuple[int, int]] = {}

        if fallback_times:
            denom = max(1, len(lines) - 1)
            for align in align_results:
                if align.get("aligned"):
                    continue
                mapped = int(round(int(align["line_idx"]) * (fallback_len - 1) / denom))
                mapped = max(0, min(mapped, fallback_len - 1))
                fallback_schedule.setdefault(mapped, []).append(int(align["line_idx"]))

            for mapped, line_indices in fallback_schedule.items():
                start_str, end_str = fallback_times[mapped]
                start_ms = SRTParser.time_to_milliseconds(start_str) if start_str else 0
                end_ms = SRTParser.time_to_milliseconds(end_str) if end_str else start_ms
                if end_ms <= start_ms:
                    end_ms = start_ms + min_interval_ms
                duration = max(1, end_ms - start_ms)
                count = max(1, len(line_indices))
                if duration >= count * min_interval_ms:
                    interval = duration / count
                else:
                    interval = duration / count

                prev_end = start_ms
                for i, line_idx in enumerate(line_indices):
                    if count == 1:
                        line_start = start_ms
                        line_end = end_ms
                    else:
                        line_start = start_ms + int(round(i * interval))
                        if line_start < prev_end:
                            line_start = prev_end
                        if i == count - 1:
                            line_end = end_ms
                        else:
                            line_end = start_ms + int(round((i + 1) * interval))
                        if line_end <= line_start:
                            line_end = min(end_ms, line_start + max(1, int(round(interval))))
                        if line_end < line_start:
                            line_end = line_start
                    fallback_times_by_line[line_idx] = (line_start, line_end)
                    prev_end = line_end

        for align in align_results:
            text = str(align["text"])
            if not align["aligned"]:
                if fallback_times_by_line:
                    line_idx = int(align["line_idx"])
                    mapped_range = fallback_times_by_line.get(line_idx)
                    if mapped_range:
                        start_ms, end_ms = mapped_range
                        final_results.append(
                            {
                                "index": len(final_results) + 1,
                                "time_start": SRTParser.milliseconds_to_time(start_ms),
                                "time_end": SRTParser.milliseconds_to_time(end_ms),
                                "text": text,
                            }
                        )
                        fallback_used += 1
                        continue
                skipped += 1
                continue
            start_ms = int(tokens[int(align["start"])]["start"] * 1000)
            end_ms = int(tokens[int(align["end"])]["end"] * 1000)
            final_results.append(
                {
                    "index": len(final_results) + 1,
                    "time_start": SRTParser.milliseconds_to_time(start_ms),
                    "time_end": SRTParser.milliseconds_to_time(end_ms),
                    "text": text,
                }
            )

        if fallback_used:
            self._logger.debug(
                "⚠️ 对齐未匹配片段已按原始段均分回填: groups=%s lines=%s min_gap=%sms",
                len(fallback_schedule),
                fallback_used,
                min_interval_ms,
            )
        if skipped:
            self._logger.debug("⚠️ 对齐未匹配片段已跳过: count=%s", skipped)

        aligned_count = sum(1 for item in align_results if item.get("aligned"))
        self._logger.debug(
            "📊 对齐完成: 成功=%s, 失败=%s, 最终片段=%s",
            aligned_count,
            len(align_results) - aligned_count,
            len(final_results),
        )
        return final_results

    def get_summary(self, full_text: str, config: SegmentationConfig, prompt: str) -> str:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": full_text},
        ]
        return self._call_with_retry(messages, config)

    def _segment_chunk(
        self,
        chunk: str,
        summary: str,
        system_prompt: str,
        config: SegmentationConfig,
        is_retry: bool = False,
    ) -> Dict[str, object]:
        effective_prompt = system_prompt
        if is_retry:
            effective_prompt = (
                system_prompt
                + "\n\n[Strict Rules] Do not omit or add any characters. "
                + "Only output the segmented lines."
            )

        if summary:
            user_prompt = f"【全文摘要】：\n{summary}\n\n【当前文本块】：\n{chunk}"
        else:
            user_prompt = f"【当前文本块】：\n{chunk}"

        messages = [
            {"role": "system", "content": effective_prompt},
            {"role": "user", "content": user_prompt},
        ]

        raw = self._call_with_retry(messages, config)
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        cleaned = self._sanitize_lines(chunk, lines)
        if cleaned:
            lines = cleaned

        normalized_joined = normalize_for_comparison("".join(lines))
        normalized_chunk = normalize_for_comparison(chunk)
        similarity = sequence_similarity(normalized_joined, normalized_chunk)
        return {"lines": lines, "similarity": similarity}

    def _sanitize_lines(self, chunk: str, lines: List[str]) -> List[str]:
        if not lines:
            return []
        normalized_chunk = normalize_for_comparison(chunk)
        cursor = 0
        cleaned: List[str] = []
        for line in lines:
            normalized_line = normalize_for_comparison(line)
            if not normalized_line:
                continue
            idx = normalized_chunk.find(normalized_line, cursor)
            if idx == -1:
                continue
            cleaned.append(line)
            cursor = idx + len(normalized_line)
        return cleaned

    def _call_with_retry(self, messages: List[Dict[str, str]], config: SegmentationConfig) -> str:
        retries = max(0, int(config.max_retries or 0))
        last_error: Exception | None = None
        self._logger.debug("📊 语义分段重试配置: max_retries=%s", retries)
        for attempt in range(retries + 1):
            self._check_stop(config)
            try:
                return self._call_with_timeout(messages, config)
            except UserStoppedException:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    self._logger.warning(
                        "语义分段请求失败，将重试(%s/%s): %s",
                        attempt + 1,
                        retries,
                        exc,
                    )
                    time.sleep(1 + attempt)
                else:
                    self._logger.error(
                        "语义分段请求失败，已达到最大重试(%s/%s): %s",
                        attempt,
                        retries,
                        exc,
                    )
        raise last_error or RuntimeError("Segmentation call failed")

    def _call_with_timeout(self, messages: List[Dict[str, str]], config: SegmentationConfig) -> str:
        return self._call_api(messages, config)

    def _call_api(self, messages: List[Dict[str, str]], config: SegmentationConfig) -> str:
        provider = config.provider
        api_key = config.api_key
        endpoint = config.endpoint
        model = config.model
        if not all([provider, api_key, endpoint, model]):
            raise ValueError("缺少语义分段所需的接口配置")

        provider_max_tokens = int(self._api_manager.get_max_tokens(provider) or 4096)
        max_tokens = max(256, int(float(config.max_chars_per_chunk or 4000) * 1.5))
        max_tokens = min(provider_max_tokens, max_tokens)
        request_data = {
            "model": model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        headers = self._api_manager.get_auth_headers(provider, api_key)
        headers["Content-Type"] = "application/json"
        headers["User-Agent"] = "SubtitleTranslator/1.0"

        input_tokens = self._estimate_tokens(json.dumps(request_data, ensure_ascii=False))
        rate_limits = dict(config.provider_limits or {})
        rate_limits["requests_per_minute"] = 3
        if not self._rate_limiter.reserve_request(
            provider,
            rate_limits,
            input_tokens,
            stop_event=config.stop_event,
        ):
            raise RuntimeError("速率限制检查失败")
        if config.stop_event and config.stop_event.is_set():
            raise UserStoppedException("用户已停止语义分段。")

        proxies = self._build_proxies(config)
        timeout_seconds = max(5, int(config.timeout_seconds or 180))
        chat_endpoint = f"{endpoint.rstrip('/')}/chat/completions"
        response = self._session.post(
            chat_endpoint,
            json=request_data,
            headers=headers,
            proxies=proxies,
            timeout=(30, timeout_seconds),
        )

        if response.status_code != 200:
            error_info = self._parse_api_error(response)
            raise RuntimeError(f"语义分段请求失败: {error_info}")

        response_data = response.json()
        choices = response_data.get("choices") or []
        if not choices:
            raise RuntimeError("语义分段响应为空")

        content = choices[0].get("message", {}).get("content")
        if not content:
            raise RuntimeError("语义分段响应缺少内容")

        self._rate_limiter.record_tokens(provider, input_tokens, 0)
        return str(content)

    def _build_proxies(self, config: SegmentationConfig) -> Dict[str, str | None]:
        if config.proxy_enabled and config.proxy_address:
            return {"http": config.proxy_address, "https": config.proxy_address}
        return {"http": None, "https": None}

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 3)

    def _parse_api_error(self, response: requests.Response) -> str:
        try:
            error_data = response.json()
            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    message = error_info.get("message", "未知错误")
                    error_type = error_info.get("type", "")
                    return f"{error_type}: {message}" if error_type else message
                return str(error_info)
            return f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception:
            return f"HTTP {response.status_code}: {response.text[:200]}"

    def _split_text_prefer_two(self, text: str, max_chars: int) -> List[str]:
        length = len(text)
        if length <= max_chars:
            return [text.strip()] if text.strip() else []

        if length <= max_chars * 2:
            mid = length // 2
            split = self._find_best_split(text, mid, min(length, mid + 200))
            return [text[:split].strip(), text[split:].strip()]

        chunks: List[str] = []
        cursor = 0
        while cursor < length:
            end = min(cursor + max_chars, length)
            split = self._find_best_split(text, end - 200, end)
            next_cursor = split if split > cursor else end
            chunks.append(text[cursor:next_cursor].strip())
            cursor = next_cursor
            if len(chunks) > 10000:
                break
        return [chunk for chunk in chunks if chunk]

    def split_text_prefer_two(self, text: str, max_chars: int) -> List[str]:
        return self._split_text_prefer_two(text, max_chars)

    def _find_best_split(self, text: str, start: int, end: int) -> int:
        clamp_start = max(0, start)
        clamp_end = min(len(text), end)
        window = text[clamp_start:clamp_end]
        candidates = [
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("，"),
            window.rfind("、"),
            window.rfind("."),
            window.rfind("!"),
            window.rfind("！"),
            window.rfind("?"),
            window.rfind("？"),
            window.rfind("…"),
            window.rfind(" "),
        ]
        best = max(candidates) if candidates else -1
        if best > 0:
            return clamp_start + best + 1
        return clamp_end

    def _check_stop(self, config: SegmentationConfig) -> None:
        if config.stop_event and config.stop_event.is_set():
            raise UserStoppedException("用户已停止语义分段。")


def clear_segmentation_resume_cache() -> None:
    cache_root = get_target_config_dir().parent / "segmentation_resume"
    if not cache_root.exists():
        return
    for path in cache_root.glob("segmentation_*.json"):
        try:
            path.unlink()
        except Exception:
            continue
