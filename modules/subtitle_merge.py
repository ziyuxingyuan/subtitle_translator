from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from modules.srt_parser import SRTParser


@dataclass
class Subtitle:
    index: int
    start_ms: int
    end_ms: int
    text: str

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


class SubtitleMergeEngine:
    OVERLAP_THRESHOLD = 0.30

    def merge(
        self,
        pass1_path: Path,
        pass2_path: Path,
        output_path: Path,
        strategy: str,
    ) -> Dict[str, int | str]:
        strategies = {
            "pass1_primary": self._merge_pass1_primary,
            "pass1_overlap": self._merge_pass1_overlap,
        }
        if strategy not in strategies:
            raise ValueError(f"未知合并策略: {strategy}")

        subs1 = self._parse_srt(pass1_path)
        subs2 = self._parse_srt(pass2_path)

        merged = strategies[strategy](subs1, subs2)
        for idx, sub in enumerate(merged, start=1):
            sub.index = idx

        self._write_srt(merged, output_path)
        return {
            "pass1_count": len(subs1),
            "pass2_count": len(subs2),
            "merged_count": len(merged),
            "strategy": strategy,
        }

    def _parse_srt(self, path: Path) -> List[Subtitle]:
        if not path.exists():
            raise FileNotFoundError(f"SRT 文件不存在: {path}")
        content = path.read_text(encoding="utf-8-sig")
        parser = SRTParser()
        blocks = parser.parse(content)
        subs: List[Subtitle] = []
        for block in blocks:
            start_ms = SRTParser.time_to_milliseconds(block["start_time"])
            end_ms = SRTParser.time_to_milliseconds(block["end_time"])
            subs.append(
                Subtitle(
                    index=int(block["index"]),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=str(block.get("text", "")).strip(),
                )
            )
        return subs

    def _write_srt(self, subtitles: List[Subtitle], path: Path) -> None:
        parser = SRTParser()
        blocks = [
            {
                "index": idx + 1,
                "start_time": SRTParser.milliseconds_to_time(sub.start_ms),
                "end_time": SRTParser.milliseconds_to_time(sub.end_ms),
                "text": sub.text,
            }
            for idx, sub in enumerate(subtitles)
        ]
        content = parser.rebuild(blocks)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _has_overlap(self, base: Subtitle, other: Subtitle, allow_threshold: bool) -> bool:
        overlap_start = max(base.start_ms, other.start_ms)
        overlap_end = min(base.end_ms, other.end_ms)
        if overlap_end <= overlap_start:
            return False

        overlap_duration = overlap_end - overlap_start
        if allow_threshold:
            allowed = int(base.duration_ms * self.OVERLAP_THRESHOLD)
            return overlap_duration > allowed
        return True

    def _merge_primary_fill(
        self,
        primary: List[Subtitle],
        secondary: List[Subtitle],
        allow_threshold: bool,
    ) -> List[Subtitle]:
        merged: List[Subtitle] = [
            Subtitle(0, sub.start_ms, sub.end_ms, sub.text) for sub in primary
        ]

        for sec_sub in secondary:
            has_conflict = False
            for pri_sub in primary:
                if self._has_overlap(pri_sub, sec_sub, allow_threshold):
                    has_conflict = True
                    break
            if not has_conflict:
                merged.append(Subtitle(0, sec_sub.start_ms, sec_sub.end_ms, sec_sub.text))

        merged.sort(key=lambda s: s.start_ms)
        return merged

    def _merge_pass1_primary(self, subs1: List[Subtitle], subs2: List[Subtitle]) -> List[Subtitle]:
        return self._merge_primary_fill(subs1, subs2, allow_threshold=False)

    def _merge_pass1_overlap(self, subs1: List[Subtitle], subs2: List[Subtitle]) -> List[Subtitle]:
        return self._merge_primary_fill(subs1, subs2, allow_threshold=True)
