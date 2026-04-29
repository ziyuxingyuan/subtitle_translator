from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence


@dataclass(frozen=True)
class SemanticWord:
    text: str
    start: float
    end: float

    def to_engine_token(self) -> Dict[str, float | str]:
        return {"text": self.text, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class RawSegment:
    index: int
    time_start: str
    time_end: str
    text: str

    def to_engine_segment(self) -> Dict[str, str | int]:
        return {
            "index": self.index,
            "time_start": self.time_start,
            "time_end": self.time_end,
            "text": self.text,
        }


@dataclass(frozen=True)
class SemanticSegment:
    start: float
    end: float
    ja_text: str
    source_word_range: tuple[int, int] | None = None
    segmentation_source: str = "rule"
    risk_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticDocument:
    raw_words: tuple[SemanticWord, ...]
    raw_segments: tuple[RawSegment, ...]
    full_text: str
    language: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_engine_tokens(self) -> List[Dict[str, float | str]]:
        return [word.to_engine_token() for word in self.raw_words]

    def to_engine_segments(self) -> List[Dict[str, str | int]]:
        return [segment.to_engine_segment() for segment in self.raw_segments]


def build_full_text(words: Sequence[SemanticWord], segments: Sequence[RawSegment]) -> str:
    if words:
        return " ".join(word.text for word in words)
    return "\n".join(segment.text for segment in segments if segment.text)
