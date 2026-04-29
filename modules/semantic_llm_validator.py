from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

_ALLOWED_PUNCTUATION = set('。！？、，,.!?…；;：「」『』（）()【】[]〈〉《》　 \t\r\n')


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    original: str
    candidate: str
    stripped_original: str
    stripped_candidate: str
    reason: str


def strip_added_punctuation(text: str) -> str:
    return ''.join(ch for ch in text if ch not in _ALLOWED_PUNCTUATION)


def punctuation_only_changed(original: str, candidate: str) -> ValidationResult:
    original_text = str(original or '')
    candidate_text = str(candidate or '')
    stripped_original = strip_added_punctuation(original_text)
    stripped_candidate = strip_added_punctuation(candidate_text)
    valid = stripped_original == stripped_candidate
    reason = 'ok' if valid else 'candidate modified non-punctuation characters'
    return ValidationResult(
        valid=valid,
        original=original_text,
        candidate=candidate_text,
        stripped_original=stripped_original,
        stripped_candidate=stripped_candidate,
        reason=reason,
    )


def find_non_punctuation_diffs(original: str, candidate: str) -> list[tuple[int, str, str]]:
    left = strip_added_punctuation(original)
    right = strip_added_punctuation(candidate)
    diffs: list[tuple[int, str, str]] = []
    for idx in range(max(len(left), len(right))):
        lch = left[idx] if idx < len(left) else ''
        rch = right[idx] if idx < len(right) else ''
        if lch != rch:
            diffs.append((idx, lch, rch))
    return diffs
