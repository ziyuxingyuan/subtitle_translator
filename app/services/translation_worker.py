from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from PyQt6.QtCore import QThread, pyqtSignal

from modules.api_manager import APIManager
from modules.rate_limiter import RateLimiter
from modules.segmentation_engine import SegmentationConfig, UserStoppedException as SegmentationStoppedException
from modules.semantic_exporter import export_semantic_artifacts
from modules.semantic_pipeline import run_semantic_pipeline
from modules.whisper_json_loader import load_whisper_json_content
from modules.srt_parser import SRTParser
from modules.translation_state_manager import TranslationStateManager
from modules.translator import TranslationEngine, UserStoppedException as TranslationStoppedException
from app.services.logging_setup import get_logger
from modules.config_paths import get_target_config_dir


class TranslationWorker(QThread):
    progress = pyqtSignal(int)
    progress_detail = pyqtSignal(int, int)
    stats_updated = pyqtSignal(int, int)
    log = pyqtSignal(str)
    finished = pyqtSignal(str)
    partial = pyqtSignal(str)
    stopped = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        input_path: str,
        output_path: str,
        settings: Dict[str, Any],
        provider: str,
        api_key: str,
        model: str,
        resume: bool = False,
        source_type: str | None = None,
        segmentation_config: SegmentationConfig | None = None,
        preprocessed_path: str | None = None,
    ) -> None:
        super().__init__()
        self._input_path = Path(input_path)
        self._output_path = Path(output_path)
        self._settings = settings
        self._provider = provider
        self._api_key = api_key
        self._model = model
        self._resume = resume
        self._source_type = (source_type or "").lower()
        self._segmentation_config = segmentation_config
        self._preprocessed_path = Path(preprocessed_path) if preprocessed_path else None
        self._stop_event = threading.Event()
        self._logger = get_logger("translation")
        self._rate_limiter = RateLimiter()
        self._active_workers = 0
        self._debug_task_id = ""
        if self._settings.get("debug_mode", 0):
            self._debug_task_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            debug_root = get_target_config_dir().parent / "debug_files" / f"task_{self._debug_task_id}"
            debug_root.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        self._stop_event.set()
        TranslationEngine.abort_all()

    def _emit_log(
        self,
        message: str,
        level: str = "INFO",
        batch_index: int | None = None,
        total_batches: int | None = None,
        status: str | None = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": str(message),
        }
        if batch_index is not None:
            payload["batch_index"] = batch_index
        if total_batches is not None:
            payload["total_batches"] = total_batches
        if status:
            payload["status"] = status
        self.log.emit(json.dumps(payload, ensure_ascii=False))

    def _prepare_input_path(self) -> Path:
        if self._is_json_input():
            return self._prepare_json_input()
        return self._input_path

    def _is_json_input(self) -> bool:
        if self._source_type:
            return self._source_type == "json"
        return self._input_path.suffix.lower() == ".json"

    def _prepare_json_input(self) -> Path:
        if self._preprocessed_path and self._preprocessed_path.exists():
            self._emit_log(f"使用预处理文件: {self._preprocessed_path}")
            return self._preprocessed_path

        if not self._segmentation_config:
            raise ValueError("JSON 输入缺少语义分段配置")

        content = self._input_path.read_text(encoding="utf-8")
        document = load_whisper_json_content(content)

        self._emit_log("正在执行语义分段...")
        self._segmentation_config.stop_event = self._stop_event
        if self._settings.get("debug_mode", 0):
            self._segmentation_config.debug_mode = True
            if self._debug_task_id:
                self._segmentation_config.debug_task_id = self._debug_task_id
            self._segmentation_config.debug_batch_index = 1
        if not str(getattr(self._segmentation_config, "batch_cache_dir", "") or "").strip():
            pre_path_for_cache = self._preprocessed_path or self._build_preprocessed_path()
            cache_dir = pre_path_for_cache.parent / "_semantic_batch_cache" / pre_path_for_cache.stem
            self._segmentation_config.batch_cache_dir = str(cache_dir)
        result = run_semantic_pipeline(
            document=document,
            segmentation_config=self._segmentation_config,
            log_func=lambda msg: self._emit_log(msg),
        )

        if self._stop_event.is_set():
            raise SegmentationStoppedException("翻译已停止，语义分段未完成。")

        pre_path = self._preprocessed_path or self._build_preprocessed_path()
        pre_json_path = pre_path.with_name(f"{pre_path.stem}.whisper.json")
        export_semantic_artifacts(
            document=document,
            segments=[item.segment for item in result.risk_results],
            srt_path=pre_path,
            json_path=pre_json_path,
            metadata={
                "llm_attempted": result.llm_attempted,
                "llm_applied": result.llm_applied,
                "source_path": str(self._input_path),
            },
        )
        self._emit_log(f"预处理文件已生成: {pre_path}", status="成功")
        return pre_path

    def _build_preprocessed_path(self) -> Path:
        base_name = self._input_path.stem or "output"
        output_dir = self._output_path.parent if self._output_path else self._input_path.parent
        return output_dir / f"{base_name}_semantic.srt"

    def run(self) -> None:
        try:
            self.progress.emit(0)
            self._emit_log("准备输入...")
            self._input_path = self._prepare_input_path()
            self._emit_log(f"加载输入文件: {self._input_path}")
            self._logger.info("加载字幕文件: %s", self._input_path)

            parser = SRTParser()
            content = self._input_path.read_text(encoding="utf-8-sig")
            blocks = parser.parse(content)
            self._emit_log(f"字幕解析完成，共 {len(blocks)} 行。", status="成功")

            state_manager = TranslationStateManager(str(self._input_path))
            total_blocks, translated_count, blocks_to_translate = self._prepare_state(
                state_manager, blocks
            )
            self._emit_progress(translated_count, total_blocks)
            self._emit_log(
                f"待翻译行数：{len(blocks_to_translate)}/{total_blocks}。",
                status="准备",
            )

            if not blocks_to_translate:
                self._emit_log("无需翻译，直接输出结果。", status="成功")
                self._write_partial_output(state_manager, parser, blocks)
                state_manager.cleanup()
                self.progress.emit(100)
                self.finished.emit(str(self._output_path))
                return

            api_manager = APIManager()
            provider_info = api_manager.get_provider_info(self._provider) or {}
            provider_endpoint = (provider_info.get("base_url") or "").strip()
            legacy_endpoint = str(self._settings.get("endpoint", "") or "").strip()
            endpoint = provider_endpoint or legacy_endpoint
            if not endpoint:
                raise ValueError("Missing endpoint for provider")

            self._emit_log("开始翻译...")
            batch_timeout_seconds = self._resolve_batch_timeout_seconds()
            batch_timeout_text = f"{batch_timeout_seconds}s" if batch_timeout_seconds > 0 else "关闭"
            self._emit_log(
                "接口: %s | 模型: %s | 单请求超时: %ss | 接口重试: %s 次 | 批次重试: %s 次 | 批次硬超时: %s。"
                % (
                    self._provider,
                    self._model,
                    self._settings.get("timeout", 60),
                    self._settings.get("max_retries", 2),
                    self._settings.get("batch_retries", 2),
                    batch_timeout_text,
                ),
                status="准备",
            )
            self._logger.info("开始翻译，接口: %s", self._provider)
            translated_count = self._translate_batches(
                api_manager,
                state_manager,
                blocks_to_translate,
                total_blocks,
                translated_count,
                endpoint,
            )

            if self._stop_event.is_set():
                self._emit_log("翻译已停止，输出已丢弃。", level="WARN", status="失败")
                self.stopped.emit("翻译已停止，输出已丢弃。")
                return

            failed = translated_count < total_blocks
            self._write_partial_output(state_manager, parser, blocks)

            if failed:
                self._emit_log("翻译完成，但仅生成了部分结果。", level="WARN", status="未完成")
                self.partial.emit("翻译完成，但仅生成了部分结果。")
                return

            state_manager.cleanup()
            self.progress.emit(100)
            self.finished.emit(str(self._output_path))
        except (SegmentationStoppedException, TranslationStoppedException) as exc:
            message = str(exc) or "翻译已停止。"
            self._emit_log(message, level="WARN", status="失败")
            self.stopped.emit(message)
            self._logger.info("翻译已停止: %s", message)
        except Exception as exc:
            self._emit_log(f"翻译失败: {exc}", level="ERROR", status="失败")
            self.failed.emit(str(exc))
            self._logger.exception("翻译失败: %s", exc)

    def _prepare_state(
        self,
        state_manager: TranslationStateManager,
        all_blocks: List[Dict[str, Any]],
    ) -> tuple[int, int, List[Dict[str, Any]]]:
        has_state = state_manager.has_valid_state()

        if self._resume and has_state:
            valid, reason = state_manager.validate_source_file()
            if not valid:
                self._emit_log(f"断点续译不可用：{reason}", level="WARN", status="恢复")
                raise ValueError(reason)

            total_blocks, translated_count, _ = state_manager.get_total_blocks_info()
            if total_blocks == 0:
                total_blocks = len(all_blocks)
            blocks_to_translate = state_manager.get_untranslated_blocks(all_blocks)
            self._emit_log("继续上次翻译进度...", status="恢复")
            return total_blocks, translated_count, blocks_to_translate

        if self._resume and not has_state:
            self._emit_log("未找到可用断点状态，已重新开始翻译。", level="WARN", status="恢复")

        if has_state:
            state_manager.cleanup()

        if not state_manager.start_new_translation(all_blocks):
            raise RuntimeError("无法创建翻译进度状态文件，请检查配置目录权限后重试。")
        return len(all_blocks), 0, all_blocks

    def _translate_batches(
        self,
        api_manager: APIManager,
        state_manager: TranslationStateManager,
        blocks_to_translate: List[Dict[str, Any]],
        total_blocks: int,
        translated_count: int,
        endpoint: str,
    ) -> int:
        batch_size = max(1, int(self._settings.get("batch_size", 10)))
        concurrency = int(self._settings.get("concurrency", 3))
        batches = [
            blocks_to_translate[i:i + batch_size]
            for i in range(0, len(blocks_to_translate), batch_size)
        ]

        self._rate_limiter = RateLimiter()
        rate_limiter = self._rate_limiter
        engine_config = self._build_engine_config(api_manager, endpoint)
        batch_timeout_seconds = self._resolve_batch_timeout_seconds()
        batch_started_at: Dict[int, float] = {}

        max_workers = 1
        if concurrency > 1 and len(batches) > 1:
            max_workers = min(max(1, concurrency), 8)
        self._active_workers = min(max_workers, len(batches))
        failed_batches = 0
        batch_retry_limit = max(0, int(self._settings.get("batch_retries", 2)))
        batch_retry_counts: Dict[int, int] = {}
        progress_state_warned = False
        executor = ThreadPoolExecutor(max_workers=max_workers)
        pending = set()
        try:
            futures: Dict[Any, tuple[int, List[Dict[str, Any]]]] = {}
            for batch_index, batch in enumerate(batches):
                if self._stop_event.is_set():
                    break
                try:
                    batch_config = self._build_batch_engine_config_with_deadline(
                        engine_config,
                        batch_index,
                        batch_started_at,
                        batch_timeout_seconds,
                    )
                except TimeoutError as exc:
                    failed_batches += 1
                    self._emit_log(
                        f"失败：{exc}",
                        level="ERROR",
                        batch_index=batch_index + 1,
                        total_batches=len(batches),
                        status="超时",
                    )
                    self._logger.warning("Batch timed out before submit: %s", exc)
                    continue
                future = executor.submit(self._translate_batch, batch, batch_config, rate_limiter)
                futures[future] = (batch_index, batch)
                batch_retry_counts[batch_index] = 0
                self._emit_log(
                    f"已提交（{len(batch)} 行）。",
                    batch_index=batch_index + 1,
                    total_batches=len(batches),
                    status="开始",
                )

            pending = set(futures.keys())
            total_batches = len(batches)
            self._emit_log(
                f"分批翻译开始：共 {total_batches} 批，每批 {batch_size} 行，并发 {self._active_workers}。",
                status="开始",
            )
            while pending:
                if self._stop_event.is_set():
                    break
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    batch_index, batch = futures.pop(future, (-1, None))
                    try:
                        translated = future.result()
                        if self._stop_event.is_set():
                            break
                        if not state_manager.save_batch(translated) and not progress_state_warned:
                            progress_state_warned = True
                            self._emit_log(
                                "警告：翻译进度状态未成功保存，断点续译可能不可用。",
                                level="WARN",
                                status="警告",
                            )
                        translated_count += len(translated)
                        self._emit_progress(translated_count, total_blocks)
                        if batch_index >= 0:
                            self._emit_log(
                                f"完成，新增 {len(translated)} 行，进度 {translated_count}/{total_blocks}。",
                                batch_index=batch_index + 1,
                                total_batches=total_batches,
                                status="成功",
                            )
                    except TranslationStoppedException:
                        self._stop_event.set()
                        break
                    except Exception as exc:
                        if batch_index >= 0 and batch is not None:
                            retry_count = batch_retry_counts.get(batch_index, 0)
                            if retry_count < batch_retry_limit and not self._stop_event.is_set():
                                next_retry = retry_count + 1
                                batch_retry_counts[batch_index] = next_retry
                                self._emit_log(
                                    f"失败：{exc}，准备重试 ({next_retry}/{batch_retry_limit})",
                                    level="WARN",
                                    batch_index=batch_index + 1,
                                    total_batches=total_batches,
                                    status="重试",
                                )
                                try:
                                    retry_config = self._build_batch_engine_config_with_deadline(
                                        engine_config,
                                        batch_index,
                                        batch_started_at,
                                        batch_timeout_seconds,
                                    )
                                except TimeoutError as timeout_exc:
                                    failed_batches += 1
                                    self._emit_log(
                                        f"失败：{timeout_exc}",
                                        level="ERROR",
                                        batch_index=batch_index + 1,
                                        total_batches=total_batches,
                                        status="超时",
                                    )
                                    self._logger.warning("Batch retry skipped by timeout: %s", timeout_exc)
                                    continue
                                retry_future = executor.submit(
                                    self._translate_batch,
                                    batch,
                                    retry_config,
                                    rate_limiter,
                                )
                                futures[retry_future] = (batch_index, batch)
                                pending.add(retry_future)
                                continue
                        failed_batches += 1
                        self._emit_log(
                            f"失败：{exc}",
                            level="ERROR",
                            batch_index=(batch_index + 1 if batch_index >= 0 else None),
                            total_batches=total_batches,
                            status="失败",
                        )
                        self._logger.warning("Batch failed: %s", exc)
                if self._stop_event.is_set():
                    break
        finally:
            if self._stop_event.is_set():
                for future in pending:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                return translated_count
            executor.shutdown(wait=True)

        if failed_batches:
            self._emit_log(
                f"{failed_batches} 批次在重试后失败。",
                level="WARN",
                status="未完成",
            )
        return translated_count

    def _translate_batch(
        self,
        batch: List[Dict[str, Any]],
        engine_config: Dict[str, Any],
        rate_limiter: RateLimiter,
    ) -> List[Dict[str, Any]]:
        thread_engine = TranslationEngine(shared_rate_limiter=rate_limiter)
        thread_engine.configure(engine_config.copy())
        translated = thread_engine.translate_batch(batch)
        self._validate_translated_batch(translated)
        return translated

    def _validate_translated_batch(self, translated_blocks: List[Dict[str, Any]]) -> None:
        if not self._should_check_untranslated():
            return
        unchanged_indices: List[str] = []
        sample_details: List[str] = []
        for block in translated_blocks:
            original = str(block.get("original_text", "")).strip()
            translated = str(block.get("text", "")).strip()
            if original and translated == original and self._contains_kana(translated):
                index_value = block.get("index")
                index_str = str(index_value) if index_value else "?"
                unchanged_indices.append(index_str)
                if len(sample_details) < 3:
                    safe_original = original.replace("\n", " ").strip()
                    safe_translated = translated.replace("\n", " ").strip()
                    sample_details.append(f"{index_str}: 原文={safe_original} / 译文={safe_translated}")
                if len(unchanged_indices) >= 8:
                    break
        if unchanged_indices:
            detail_text = ""
            if sample_details:
                detail_text = f"；示例 {len(sample_details)}/{len(unchanged_indices)} => " + " || ".join(sample_details)
            raise ValueError(f"检测到未翻译行(含假名)：{','.join(unchanged_indices)}{detail_text}")

    def _should_check_untranslated(self) -> bool:
        source = str(self._settings.get("source_language", "")).strip()
        target = str(self._settings.get("target_language", "")).strip()
        if not source or source.lower() == "auto":
            return False
        return self._is_japanese_language(source) and not self._is_japanese_language(target)

    @staticmethod
    def _is_japanese_language(value: str) -> bool:
        lower = value.lower()
        return lower in ("ja", "japanese") or "日" in value

    @staticmethod
    def _contains_kana(text: str) -> bool:
        for char in text:
            if "\u3040" <= char <= "\u309f" or "\u30a0" <= char <= "\u30ff":
                return True
        return False

    def _emit_progress(self, translated_count: int, total_blocks: int) -> None:
        total = max(1, total_blocks)
        percent = int((translated_count / total) * 100)
        self.progress.emit(min(100, percent))
        self.progress_detail.emit(translated_count, total_blocks)
        total_tokens = self._rate_limiter.get_total_tokens(self._provider) if self._rate_limiter else 0
        self.stats_updated.emit(total_tokens, self._active_workers)

    def _write_partial_output(
        self,
        state_manager: TranslationStateManager,
        parser: SRTParser,
        all_blocks: List[Dict[str, Any]],
    ) -> None:
        combined_blocks = state_manager.get_all_blocks_for_rebuild(all_blocks)
        output_content = parser.rebuild(combined_blocks)
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path.write_text(output_content, encoding="utf-8")
        self._emit_log(f"输出文件已写入: {self._output_path}", status="成功")
        self._logger.info("输出文件已写入: %s", self._output_path)

    def _log_rate_limit(self, message: str) -> None:
        text = str(message).strip()
        if text:
            self._emit_log(text, level="WARN", status="等待")

    def _build_engine_config(self, api_manager: APIManager, endpoint: str) -> Dict[str, Any]:
        custom_prompt = self._settings.get("custom_prompt") or ""
        if not custom_prompt:
            custom_prompt = self._settings.get("system_prompt", "")

        return {
            "provider": self._provider,
            "api_key": self._api_key,
            "model": self._model,
            "endpoint": endpoint,
            "timeout": self._settings.get("timeout", 60),
            "batch_size": self._settings.get("batch_size", 10),
            "max_retries": self._settings.get("max_retries", 2),
            "concurrency": self._settings.get("concurrency", 0),
            "debug_mode": self._settings.get("debug_mode", 0),
            "proxy_enabled": self._settings.get("proxy_enabled", False),
            "proxy_address": self._settings.get("proxy_address", ""),
            "source_language": self._settings.get("source_language", "ja"),
            "target_language": self._settings.get("target_language", "zh-CN"),
            "system_prompt": custom_prompt,
            "provider_limits": api_manager.get_provider_limits(self._provider),
            "stop_event": self._stop_event,
            "rate_limit_log": self._log_rate_limit,
        }

    def _build_batch_engine_config(self, base_config: Dict[str, Any], batch_index: int) -> Dict[str, Any]:
        config = base_config.copy()
        if self._debug_task_id:
            config["debug_task_id"] = self._debug_task_id
            config["debug_batch_index"] = batch_index
        return config

    def _resolve_batch_timeout_seconds(self) -> int:
        raw_value = self._settings.get("batch_timeout", 0)
        try:
            timeout_seconds = int(raw_value)
        except (TypeError, ValueError):
            timeout_seconds = 0
        return max(0, timeout_seconds)

    def _build_batch_engine_config_with_deadline(
        self,
        base_config: Dict[str, Any],
        batch_index: int,
        batch_started_at: Dict[int, float],
        batch_timeout_seconds: int,
    ) -> Dict[str, Any]:
        config = self._build_batch_engine_config(base_config, batch_index)
        if batch_timeout_seconds <= 0:
            config.pop("request_deadline", None)
            return config

        started_at = batch_started_at.setdefault(batch_index, time.monotonic())
        request_deadline = started_at + batch_timeout_seconds
        remaining = request_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"批次硬超时（{batch_timeout_seconds}秒）已触发，停止该批次。")

        config["request_deadline"] = request_deadline
        return config
