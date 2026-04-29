from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from app.services.logging_setup import get_logger
from modules.segmentation_engine import SegmentationConfig, UserStoppedException
from modules.semantic_exporter import export_semantic_artifacts
from modules.semantic_pipeline import run_semantic_pipeline
from modules.whisper_json_loader import load_whisper_json_content


class SegmentationWorker(QThread):
    log = pyqtSignal(str)
    finished = pyqtSignal(str)
    stopped = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        input_path: str,
        pre_output_path: str,
        segmentation_config: SegmentationConfig,
    ) -> None:
        super().__init__()
        self._input_path = Path(input_path)
        self._pre_output_path = Path(pre_output_path)
        self._segmentation_config = segmentation_config
        if self._segmentation_config.debug_mode and not self._segmentation_config.debug_task_id:
            self._segmentation_config.debug_task_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        if self._segmentation_config.debug_mode and self._segmentation_config.debug_batch_index <= 0:
            self._segmentation_config.debug_batch_index = 1
        self._stop_event = threading.Event()
        self._logger = get_logger("segmentation")

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            if not self._input_path.exists():
                raise FileNotFoundError("Input file not found or inaccessible.")
            content = self._input_path.read_text(encoding="utf-8")
            document = load_whisper_json_content(content)
            self._segmentation_config.stop_event = self._stop_event
            if not str(getattr(self._segmentation_config, "batch_cache_dir", "") or "").strip():
                cache_dir = self._pre_output_path.parent / "_semantic_batch_cache" / self._pre_output_path.stem
                self._segmentation_config.batch_cache_dir = str(cache_dir)
            result = run_semantic_pipeline(
                document=document,
                segmentation_config=self._segmentation_config,
                log_func=self.log.emit,
            )
            if self._stop_event.is_set():
                raise UserStoppedException("Segmentation stopped by user.")
            output_json = self._pre_output_path.with_name(f"{self._pre_output_path.stem}.whisper.json")
            export_semantic_artifacts(
                document=document,
                segments=[item.segment for item in result.risk_results],
                srt_path=self._pre_output_path,
                json_path=output_json,
                metadata={
                    "llm_attempted": result.llm_attempted,
                    "llm_applied": result.llm_applied,
                    "source_path": str(self._input_path),
                },
            )
            self._logger.debug("segmentation exported: %s", self._pre_output_path)
            self.finished.emit(str(self._pre_output_path))
        except UserStoppedException as exc:
            message = str(exc) or "Segmentation stopped."
            self.stopped.emit(message)
            self._logger.info("segmentation stopped: %s", message)
        except Exception as exc:
            self.failed.emit(str(exc))
            self._logger.exception("segmentation failed: %s", exc)
