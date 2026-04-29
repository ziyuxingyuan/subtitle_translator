from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from modules.semantic_models import SemanticSegment


@dataclass(frozen=True)
class RiskDetectionResult:
    segment: SemanticSegment
    risk_score: float
    risk_flags: Tuple[str, ...]
    needs_llm_refine: bool


class SemanticRiskDetector:
    def __init__(
        self,
        long_chars_threshold: int = 20,
        long_duration_threshold: float = 5.0,
        uncertain_gap_threshold: float = 0.4,
    ) -> None:
        self._long_chars_threshold = long_chars_threshold
        self._long_duration_threshold = long_duration_threshold
        self._uncertain_gap_threshold = uncertain_gap_threshold

    def detect(self, segments: Sequence[SemanticSegment]) -> List[RiskDetectionResult]:
        results: List[RiskDetectionResult] = []
        for index, segment in enumerate(segments):
            flags = self._collect_flags(segment, index, segments)
            score = self._score(flags)
            results.append(
                RiskDetectionResult(
                    segment=SemanticSegment(
                        start=segment.start,
                        end=segment.end,
                        ja_text=segment.ja_text,
                        source_word_range=segment.source_word_range,
                        segmentation_source=segment.segmentation_source,
                        risk_flags=tuple(flags),
                    ),
                    risk_score=score,
                    risk_flags=tuple(flags),
                    needs_llm_refine=score >= 2.0,
                )
            )
        return results

    def _collect_flags(
        self,
        segment: SemanticSegment,
        index: int,
        segments: Sequence[SemanticSegment],
    ) -> List[str]:
        flags: List[str] = []
        text = segment.ja_text.strip()
        duration = max(0.0, segment.end - segment.start)
        if len(text) >= self._long_chars_threshold:
            flags.append("long_sentence")
        if duration >= self._long_duration_threshold:
            flags.append("long_duration")
        if "。か？" in text or "です。か？" in text or "ます。か？" in text:
            flags.append("question_conflict")
        if "、も" in text or "ので。" in text:
            flags.append("boundary_conflict")
        if self._tiny_fragment(text, duration):
            flags.append("tiny_fragment")
        if self._repetition(text):
            flags.append("repetition")
        if self._is_uncertain(index, segments):
            flags.append("boundary_uncertain")
        return flags

    def _tiny_fragment(self, text: str, duration: float) -> bool:
        return len(text) <= 2 and duration <= 0.35

    def _repetition(self, text: str) -> bool:
        if len(text) < 4:
            return False
        for size in (1, 2, 3):
            if len(text) >= size * 3 and text[:size] * 3 in text:
                return True
        return False

    def _is_uncertain(self, index: int, segments: Sequence[SemanticSegment]) -> bool:
        if index <= 0:
            return False
        prev = segments[index - 1]
        curr = segments[index]
        gap = max(0.0, curr.start - prev.end)
        return gap <= self._uncertain_gap_threshold and len(curr.ja_text.strip()) <= 4

    def _score(self, flags: Sequence[str]) -> float:
        score = 0.0
        weights = {
            "long_sentence": 1.0,
            "long_duration": 1.0,
            "question_conflict": 2.0,
            "boundary_conflict": 1.5,
            "tiny_fragment": 1.0,
            "repetition": 1.2,
            "boundary_uncertain": 1.0,
        }
        for flag in flags:
            score += weights.get(flag, 0.5)
        return score
