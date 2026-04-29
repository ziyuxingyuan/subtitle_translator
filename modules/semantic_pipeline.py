from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from modules.semantic_llm_requester import build_semantic_requester
from modules.semantic_llm_validator import punctuation_only_changed
from modules.semantic_models import SemanticDocument, SemanticSegment
from modules.semantic_risk_detector import RiskDetectionResult, SemanticRiskDetector
from modules.semantic_rule_engine import SemanticRuleEngine

try:
    from modules.segmentation_engine import SegmentationConfig
except Exception:
    SegmentationConfig = object  # type: ignore[misc,assignment]


BATCH_SIZE = 10
BATCH_SYSTEM_PROMPT = (
    '\u4f60\u662f\u4e00\u4e2a\u53d7\u9650\u7684\u65e5\u6587\u5b57\u5e55\u6807\u70b9\u6279\u5904\u7406\u5668\u3002'
    '\u4f60\u53ea\u80fd\u5728\u539f\u6587\u4e2d\u6dfb\u52a0\u6807\u70b9\u7b26\u53f7\uff0c\u4e0d\u5141\u8bb8\u5220\u9664\u3001\u6539\u5199\u3001\u66ff\u6362\u3001\u91cd\u6392\u4efb\u4f55\u5b57\u7b26\u3002'
    '\u4f60\u5fc5\u987b\u4e25\u683c\u8fd4\u56de JSON \u6570\u7ec4\uff0c\u6bcf\u4e2a\u5143\u7d20\u683c\u5f0f\u4e3a {"id":\u6570\u5b57,"text":"\u6587\u672c"}\uff0c\u4e0d\u8981\u8f93\u51fa\u89e3\u91ca\u3002'
)
CACHE_FILE_PREFIX = 'semantic_batch_'


@dataclass(frozen=True)
class SemanticPipelineResult:
    segments: tuple[SemanticSegment, ...]
    risk_results: tuple[RiskDetectionResult, ...]
    llm_attempted: int = 0
    llm_applied: int = 0


@dataclass(frozen=True)
class BatchItem:
    segment_index: int
    segment: SemanticSegment
    prev_text: str
    next_text: str


@dataclass(frozen=True)
class BatchAttemptResult:
    success_map: dict[int, str]
    failed_ids: tuple[int, ...]
    last_reason: str


@dataclass(frozen=True)
class BatchRunResult:
    batch_number: int
    success_map: dict[int, str]
    failed_ids: tuple[int, ...]
    last_reason: str


def run_semantic_pipeline(
    document: SemanticDocument,
    segmentation_config: SegmentationConfig | None,
    log_func: Callable[[str], None] | None = None,
) -> SemanticPipelineResult:
    log = _build_logger(log_func)
    base_segments = _run_rule_segmentation(document, log)
    detector = SemanticRiskDetector()
    risk_results = tuple(detector.detect(base_segments))
    risky_count = sum(1 for item in risk_results if item.needs_llm_refine)
    log(f'\u8bed\u4e49\u98ce\u9669\u68c0\u6d4b\u5b8c\u6210\uff1a\u9ad8\u98ce\u9669 {risky_count} \u6bb5')
    if not _can_use_llm(segmentation_config):
        return SemanticPipelineResult(
            segments=tuple(item.segment for item in risk_results),
            risk_results=risk_results,
        )
    refined_segments, attempted, applied = _apply_llm_refinement(risk_results, segmentation_config, log)
    final_risks = tuple(detector.detect(refined_segments))
    return SemanticPipelineResult(
        segments=tuple(item.segment for item in final_risks),
        risk_results=final_risks,
        llm_attempted=attempted,
        llm_applied=applied,
    )


def _build_logger(
    log_func: Callable[[str], None] | None,
) -> Callable[[str], None]:
    def log(message: str) -> None:
        if log_func:
            log_func(message)
    return log


def _run_rule_segmentation(
    document: SemanticDocument,
    log: Callable[[str], None],
) -> Sequence[SemanticSegment]:
    engine = SemanticRuleEngine()
    segments = tuple(engine.segment(document.raw_words))
    log(f'\u89c4\u5219\u8bed\u4e49\u5206\u6bb5\u5b8c\u6210\uff1a{len(segments)} \u6bb5\uff0c\u8bcd\u6570 {len(document.raw_words)}')
    return segments


def _can_use_llm(segmentation_config: SegmentationConfig | None) -> bool:
    if segmentation_config is None:
        return False
    required_values = (
        getattr(segmentation_config, 'provider', ''),
        getattr(segmentation_config, 'model', ''),
        getattr(segmentation_config, 'api_key', ''),
        getattr(segmentation_config, 'endpoint', ''),
    )
    return all(str(value).strip() for value in required_values)


def _apply_llm_refinement(
    risk_results: Sequence[RiskDetectionResult],
    segmentation_config: SegmentationConfig,
    log: Callable[[str], None],
) -> tuple[tuple[SemanticSegment, ...], int, int]:
    requester = build_semantic_requester(segmentation_config)
    batch_items = _collect_batch_items(risk_results)
    attempted = len(batch_items)
    applied_text_map, failed_batches = _run_batches(batch_items, requester, segmentation_config, log)
    if failed_batches:
        if bool(getattr(segmentation_config, 'fallback_on_failure', False)):
            for batch_result in failed_batches:
                log(
                    f'LLM \u6279\u5904\u7406\u5931\u8d25\uff0c\u56de\u9000\u7eaf\u89c4\u5219\uff1abatch={batch_result.batch_number} '
                    f'failed_ids={list(batch_result.failed_ids)} reason={batch_result.last_reason}'
                )
        else:
            first_failure = failed_batches[0]
            raise RuntimeError(
                f'\u8bed\u4e49 LLM \u6279\u5904\u7406\u5931\u8d25\uff1abatch={first_failure.batch_number} '
                f'failed_ids={list(first_failure.failed_ids)} reason={first_failure.last_reason}'
            )
    refined_segments: list[SemanticSegment] = []
    applied = 0
    for index, item in enumerate(risk_results):
        segment = item.segment
        candidate = applied_text_map.get(index)
        if candidate is None:
            refined_segments.append(segment)
            continue
        if candidate != segment.ja_text:
            applied += 1
            log(f'LLM \u6807\u70b9\u4fee\u6b63\u5df2\u5e94\u7528\uff1aidx={index + 1}')
            refined_segments.append(_replace_segment_text(segment, candidate))
            continue
        refined_segments.append(segment)
    return tuple(refined_segments), attempted, applied


def _collect_batch_items(risk_results: Sequence[RiskDetectionResult]) -> list[BatchItem]:
    items: list[BatchItem] = []
    for index, item in enumerate(risk_results):
        if not item.needs_llm_refine:
            continue
        items.append(
            BatchItem(
                segment_index=index,
                segment=item.segment,
                prev_text=_neighbor_text(risk_results, index - 1),
                next_text=_neighbor_text(risk_results, index + 1),
            )
        )
    return items


def _run_batches(
    batch_items: Sequence[BatchItem],
    requester: Callable[[list[dict[str, str]]], str],
    segmentation_config: SegmentationConfig,
    log: Callable[[str], None],
) -> tuple[dict[int, str], list[BatchRunResult]]:
    cache_dir = _resolve_cache_dir(segmentation_config)
    applied_text_map: dict[int, str] = {}
    pending_batches: list[tuple[int, list[BatchItem], Path | None, dict[int, str]]] = []
    failed_batches: list[BatchRunResult] = []
    for batch_number, batch in enumerate(_split_batches(batch_items), start=1):
        cache_path = _batch_cache_path(cache_dir, batch_number)
        cached_success_map, pending_items = _load_batch_cache(cache_path, batch)
        applied_text_map.update(cached_success_map)
        if not pending_items:
            log(f'\u8bed\u4e49\u6279\u6b21\u547d\u4e2d\u7f13\u5b58\uff1abatch={batch_number}')
            continue
        pending_batches.append((batch_number, pending_items, cache_path, cached_success_map))
    if not pending_batches:
        return applied_text_map, failed_batches

    max_workers = max(1, int(getattr(segmentation_config, 'batch_concurrency', 1) or 1))
    max_workers = min(max_workers, len(pending_batches))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _run_single_batch,
                batch_number,
                pending_items,
                requester,
                segmentation_config,
            ): (batch_number, cache_path, cached_success_map)
            for batch_number, pending_items, cache_path, cached_success_map in pending_batches
        }
        for future in as_completed(future_map):
            batch_number, cache_path, cached_success_map = future_map[future]
            result = future.result()
            merged_success_map = dict(cached_success_map)
            merged_success_map.update(result.success_map)
            _write_batch_cache(cache_path, batch_number, merged_success_map, result.failed_ids, result.last_reason)
            applied_text_map.update(merged_success_map)
            if result.failed_ids:
                failed_batches.append(
                    BatchRunResult(
                        batch_number=batch_number,
                        success_map=merged_success_map,
                        failed_ids=result.failed_ids,
                        last_reason=result.last_reason,
                    )
                )
    failed_batches.sort(key=lambda item: item.batch_number)
    return applied_text_map, failed_batches


def _split_batches(batch_items: Sequence[BatchItem]) -> list[list[BatchItem]]:
    return [list(batch_items[index : index + BATCH_SIZE]) for index in range(0, len(batch_items), BATCH_SIZE)]


def _resolve_cache_dir(segmentation_config: SegmentationConfig) -> Path | None:
    raw_dir = str(getattr(segmentation_config, 'batch_cache_dir', '') or '').strip()
    if not raw_dir:
        return None
    cache_dir = Path(raw_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _batch_cache_path(cache_dir: Path | None, batch_number: int) -> Path | None:
    if cache_dir is None:
        return None
    return cache_dir / f'{CACHE_FILE_PREFIX}{batch_number:03d}.json'


def _load_batch_cache(
    cache_path: Path | None,
    batch: Sequence[BatchItem],
) -> tuple[dict[int, str], list[BatchItem]]:
    if cache_path is None or not cache_path.exists():
        return {}, list(batch)
    try:
        payload = json.loads(cache_path.read_text(encoding='utf-8'))
    except Exception:
        return {}, list(batch)
    success_outputs = payload.get('success_outputs') or []
    candidate_map: dict[int, str] = {}
    for item in success_outputs:
        if not isinstance(item, dict):
            continue
        item_id = _coerce_item_id(item.get('id'))
        text = item.get('text')
        if item_id is None or not isinstance(text, str):
            continue
        candidate_map[item_id] = text.strip()
    success_map: dict[int, str] = {}
    pending_items: list[BatchItem] = []
    for item in batch:
        item_id = item.segment_index + 1
        candidate = candidate_map.get(item_id)
        if candidate is None:
            pending_items.append(item)
            continue
        validation = punctuation_only_changed(item.segment.ja_text, candidate)
        if not validation.valid:
            pending_items.append(item)
            continue
        success_map[item.segment_index] = candidate
    return success_map, pending_items


def _write_batch_cache(
    cache_path: Path | None,
    batch_number: int,
    success_map: dict[int, str],
    failed_ids: Sequence[int],
    last_reason: str,
) -> None:
    if cache_path is None:
        return
    payload = {
        'batch_number': batch_number,
        'status': 'success' if not failed_ids else 'partial',
        'success_outputs': [
            {'id': segment_index + 1, 'text': text}
            for segment_index, text in sorted(success_map.items())
        ],
        'failed_ids': list(failed_ids),
        'last_reason': last_reason,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _run_single_batch(
    batch_number: int,
    batch: Sequence[BatchItem],
    requester: Callable[[list[dict[str, str]]], str],
    segmentation_config: SegmentationConfig,
) -> BatchRunResult:
    result = _refine_batch_with_retries(batch, requester, segmentation_config)
    return BatchRunResult(
        batch_number=batch_number,
        success_map=result.success_map,
        failed_ids=result.failed_ids,
        last_reason=result.last_reason,
    )


def _refine_batch_with_retries(
    batch: Sequence[BatchItem],
    requester: Callable[[list[dict[str, str]]], str],
    segmentation_config: SegmentationConfig,
) -> BatchAttemptResult:
    retry_count = max(0, int(getattr(segmentation_config, 'max_retries', 0) or 0))
    total_attempts = retry_count + 1
    latest_success_map: dict[int, str] = {}
    pending_items = list(batch)
    last_reason = ''
    failed_ids: tuple[int, ...] = tuple(item.segment_index + 1 for item in pending_items)
    for attempt_index in range(1, total_attempts + 1):
        try:
            candidate_map = _request_batch(pending_items, requester)
        except Exception as exc:
            last_reason = str(exc) or exc.__class__.__name__
            if attempt_index >= total_attempts:
                return BatchAttemptResult(success_map=latest_success_map, failed_ids=failed_ids, last_reason=last_reason)
            continue
        validation = _validate_batch_candidates(pending_items, candidate_map)
        latest_success_map.update(validation.success_map)
        if not validation.failed_ids:
            return BatchAttemptResult(success_map=latest_success_map, failed_ids=(), last_reason='')
        failed_ids = validation.failed_ids
        last_reason = validation.last_reason
        pending_items = [item for item in pending_items if (item.segment_index + 1) in failed_ids]
    return BatchAttemptResult(success_map=latest_success_map, failed_ids=failed_ids, last_reason=last_reason or 'unknown_error')


def _request_batch(
    batch: Sequence[BatchItem],
    requester: Callable[[list[dict[str, str]]], str],
) -> dict[int, str]:
    messages = _build_batch_messages(batch)
    raw_response = str(requester(messages)).strip()
    data = _parse_batch_json(raw_response)
    candidate_map: dict[int, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        item_id = _coerce_item_id(item.get('id'))
        if item_id is None:
            continue
        text = item.get('text')
        if isinstance(text, str):
            candidate_map[item_id] = text.strip()
    return candidate_map


def _build_batch_messages(batch: Sequence[BatchItem]) -> list[dict[str, str]]:
    lines = [
        '\u4efb\u52a1\uff1a\u53ea\u7ed9\u6bcf\u6761\u6587\u672c\u6dfb\u52a0\u5408\u9002\u7684\u65e5\u6587\u6807\u70b9\u3002',
        '\u786c\u6027\u8981\u6c42\uff1a',
        '1. \u4e0d\u5141\u8bb8\u5220\u9664\u4efb\u4f55\u539f\u5b57\u7b26\u3002',
        '2. \u4e0d\u5141\u8bb8\u66ff\u6362\u4efb\u4f55\u539f\u5b57\u7b26\u3002',
        '3. \u4e0d\u5141\u8bb8\u65b0\u589e\u8bcd\u8bed\u3002',
        '4. \u4e0d\u5141\u8bb8\u8c03\u6574\u987a\u5e8f\u3002',
        '5. \u8bf7\u4e25\u683c\u8fd4\u56de JSON \u6570\u7ec4\uff0c\u6bcf\u4e2a\u5143\u7d20\u53ea\u80fd\u5305\u542b id \u548c text \u4e24\u4e2a\u5b57\u6bb5\u3002',
        '6. \u6240\u6709 id \u5fc5\u987b\u5b8c\u6574\u8fd4\u56de\uff0c\u4e0d\u5f97\u9057\u6f0f\u3002',
        '',
        '\u8f93\u5165\uff1a',
    ]
    for item in batch:
        lines.extend(
            [
                f'id={item.segment_index + 1}',
                f'\u4e0a\u6587\uff1a{item.prev_text}',
                f'\u5f53\u524d\uff1a{item.segment.ja_text}',
                f'\u4e0b\u6587\uff1a{item.next_text}',
                '',
            ]
        )
    lines.append('\u53ea\u8f93\u51fa JSON \u6570\u7ec4\u3002')
    return [
        {'role': 'system', 'content': BATCH_SYSTEM_PROMPT},
        {'role': 'user', 'content': '\n'.join(lines).strip()},
    ]


def _parse_batch_json(raw_response: str) -> list[object]:
    clean = str(raw_response or '').strip()
    if clean.startswith('```'):
        clean = clean.strip('`')
        clean = clean.replace('json\n', '', 1).strip()
    start = clean.find('[')
    end = clean.rfind(']')
    if start < 0 or end < start:
        raise RuntimeError(f'\u6279\u5904\u7406\u54cd\u5e94\u4e0d\u662f JSON \u6570\u7ec4: {raw_response[:400]}')
    payload = clean[start : end + 1]
    data = json.loads(payload)
    if not isinstance(data, list):
        raise RuntimeError(f'\u6279\u5904\u7406\u54cd\u5e94\u4e0d\u662f\u5217\u8868: {raw_response[:400]}')
    return data


def _coerce_item_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _validate_batch_candidates(
    batch: Sequence[BatchItem],
    candidate_map: dict[int, str],
) -> BatchAttemptResult:
    success_map: dict[int, str] = {}
    failed_ids: list[int] = []
    reasons: list[str] = []
    for item in batch:
        item_id = item.segment_index + 1
        candidate = candidate_map.get(item_id)
        if candidate is None:
            failed_ids.append(item_id)
            reasons.append(f'id={item_id}:missing')
            continue
        validation = punctuation_only_changed(item.segment.ja_text, candidate)
        if not validation.valid:
            failed_ids.append(item_id)
            reasons.append(f'id={item_id}:{validation.reason}')
            continue
        success_map[item.segment_index] = candidate
    return BatchAttemptResult(
        success_map=success_map,
        failed_ids=tuple(failed_ids),
        last_reason='; '.join(reasons),
    )


def _neighbor_text(
    risk_results: Sequence[RiskDetectionResult],
    index: int,
) -> str:
    if 0 <= index < len(risk_results):
        return risk_results[index].segment.ja_text
    return ''


def _replace_segment_text(segment: SemanticSegment, text: str) -> SemanticSegment:
    return SemanticSegment(
        start=segment.start,
        end=segment.end,
        ja_text=text,
        source_word_range=segment.source_word_range,
        segmentation_source='llm_refined',
        risk_flags=segment.risk_flags,
    )
