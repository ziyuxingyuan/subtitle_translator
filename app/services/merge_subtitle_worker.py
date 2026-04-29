from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from modules.subtitle_merge import SubtitleMergeEngine


class MergeSubtitleWorker(QThread):
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(
        self,
        pass1_path: str,
        pass2_path: str,
        output_path: str,
        strategy: str,
    ) -> None:
        super().__init__()
        self._pass1_path = Path(pass1_path)
        self._pass2_path = Path(pass2_path)
        self._output_path = Path(output_path)
        self._strategy = strategy

    def run(self) -> None:
        try:
            engine = SubtitleMergeEngine()
            stats = engine.merge(
                self._pass1_path,
                self._pass2_path,
                self._output_path,
                self._strategy,
            )
            stats["output_path"] = str(self._output_path)
            self.finished.emit(stats)
        except Exception as exc:
            self.failed.emit(str(exc))
