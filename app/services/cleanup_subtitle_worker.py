from __future__ import annotations

from pathlib import Path
import threading
from typing import Iterable

from PyQt6.QtCore import QThread, pyqtSignal

from modules.subtitle_cleaner import SubtitleProcessor


class CleanupSubtitleWorker(QThread):
    log = pyqtSignal(str)
    finished = pyqtSignal()
    failed = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(
        self,
        mode: str,
        files: Iterable[Path],
        *,
        do_cleaning: bool = False,
        do_formatting: bool = False,
        punc_split: bool = False,
        ellipses_mode: str = "replace",
        merge_short: bool = False,
        align_json: Path | None = None,
        align_srt: Path | None = None,
    ) -> None:
        super().__init__()
        self._mode = mode
        self._files = list(files)
        self._do_cleaning = do_cleaning
        self._do_formatting = do_formatting
        self._punc_split = punc_split
        self._ellipses_mode = ellipses_mode
        self._merge_short = merge_short
        self._align_json = align_json
        self._align_srt = align_srt
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        processor = SubtitleProcessor()
        try:
            if self._mode == "align":
                if self._stop_event.is_set():
                    self.stopped.emit()
                    return
                if not self._align_json or not self._align_srt:
                    raise ValueError("对齐修段缺少 JSON 或 SRT 文件。")
                self.log.emit("处理中：对齐修段（生成 *_fixed.srt）...")
                result = processor.process_align_split_group(
                    str(self._align_json),
                    str(self._align_srt),
                )
                self.log.emit(result)
                self.finished.emit()
                return
            if self._mode == "optimize":
                if not self._files:
                    raise ValueError("未找到可处理的文件。")
                json_files = [path for path in self._files if path.suffix.lower() == ".json"]
                if not json_files:
                    raise ValueError("优化字幕仅支持 JSON 输入（*.json）。")
                total = len(json_files)
                for idx, path in enumerate(json_files, start=1):
                    if self._stop_event.is_set():
                        self.log.emit("已停止处理。")
                        self.stopped.emit()
                        return
                    self.log.emit(f"({idx}/{total}) 处理中：优化字幕 {path.name} ...")
                    result = processor.process_json_split_oneclick(str(path))
                    self.log.emit(result)
                self.finished.emit()
                return
            if self._mode == "post_clean":
                if not self._files:
                    raise ValueError("未找到可处理的文件。")
                srt_files = [path for path in self._files if path.suffix.lower() == ".srt"]
                if not srt_files:
                    raise ValueError("译后清理仅支持 SRT 输入（*.srt）。")
                total = len(srt_files)
                for idx, path in enumerate(srt_files, start=1):
                    if self._stop_event.is_set():
                        self.log.emit("已停止处理。")
                        self.stopped.emit()
                        return
                    self.log.emit(f"({idx}/{total}) 处理中：译后清理 {path.name} ...")
                    result = processor.process_post_translation_cleanup(str(path))
                    self.log.emit(result)
                self.finished.emit()
                return

            if not self._files:
                raise ValueError("未找到可处理的文件。")

            total = len(self._files)
            for idx, path in enumerate(self._files, start=1):
                if self._stop_event.is_set():
                    self.log.emit("已停止处理。")
                    self.stopped.emit()
                    return
                self.log.emit(f"({idx}/{total}) 处理中：{path.name} ...")
                result = processor.process_file(
                    str(path),
                    self._do_cleaning,
                    self._do_formatting,
                    self._punc_split,
                    self._ellipses_mode,
                    self._merge_short,
                )
                self.log.emit(result)

            self.finished.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
