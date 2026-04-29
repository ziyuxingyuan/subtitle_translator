from __future__ import annotations

from typing import List, Sequence

from modules.semantic_models import SemanticSegment, SemanticWord
from modules.semantic_patterns import (
    CLAUSE_ENDINGS,
    CLAUSE_PUNCTUATION,
    HARD_DURATION_SECONDS,
    HARD_MAX_CHARS,
    HARD_SPLIT_GAP,
    HIGH_VALUE_PATTERNS,
    INTERJECTION_TOKENS,
    MIN_DURATION_SECONDS,
    NO_SPLIT_PAIRS,
    PHRASE_MAX_INTERNAL_GAP,
    POLITE_ENDINGS,
    QUESTION_ENDINGS,
    SOFT_ENDINGS,
    SOFT_SPLIT_GAP,
    TARGET_CHARS,
    TARGET_DURATION_SECONDS,
    TERMINAL_PUNCTUATION,
    TINY_SUFFIXES,
)


class SemanticRuleEngine:
    def segment(self, words: Sequence[SemanticWord], source: str = "rule") -> List[SemanticSegment]:
        prepared = self._prepare_words(words)
        if not prepared:
            return []
        segments: List[SemanticSegment] = []
        start_idx = 0
        total = len(prepared)
        while start_idx < total:
            split_idx = self._pick_best_split(prepared, start_idx)
            text = self._segment_text(prepared, start_idx, split_idx)
            punctuation = self._choose_punctuation(text, split_idx, prepared)
            seg = SemanticSegment(
                start=prepared[start_idx].start,
                end=max(prepared[split_idx].end, prepared[start_idx].start + 0.1),
                ja_text=self._append_punctuation(text, punctuation if split_idx < total - 1 or len(text) >= 2 else ""),
                source_word_range=(start_idx, split_idx),
                segmentation_source=source,
            )
            segments.append(seg)
            start_idx = split_idx + 1
        return self._merge_tiny_suffix_segments(segments)

    def _prepare_words(self, words: Sequence[SemanticWord]) -> List[SemanticWord]:
        prepared = [SemanticWord(self._strip_punctuation(word.text), word.start, word.end) for word in words if word.text.strip()]
        changed = True
        while changed:
            prepared, changed = self._merge_high_value_patterns(prepared)
        return prepared

    def _merge_high_value_patterns(self, words: Sequence[SemanticWord]) -> tuple[List[SemanticWord], bool]:
        merged: List[SemanticWord] = []
        idx = 0
        changed = False
        while idx < len(words):
            matched = False
            for pattern, replacement in HIGH_VALUE_PATTERNS:
                size = len(pattern)
                window = words[idx : idx + size]
                if len(window) != size:
                    continue
                if tuple(word.text for word in window) != pattern:
                    continue
                if self._internal_gap_exceeds(window):
                    continue
                merged.append(SemanticWord(replacement, window[0].start, window[-1].end))
                idx += size
                matched = True
                changed = True
                break
            if matched:
                continue
            if idx + 1 < len(words) and self._can_merge_pair(words[idx], words[idx + 1]):
                merged.append(SemanticWord(words[idx].text + words[idx + 1].text, words[idx].start, words[idx + 1].end))
                idx += 2
                changed = True
                continue
            merged.append(words[idx])
            idx += 1
        return merged, changed

    def _internal_gap_exceeds(self, words: Sequence[SemanticWord]) -> bool:
        for idx in range(len(words) - 1):
            if max(0.0, words[idx + 1].start - words[idx].end) > PHRASE_MAX_INTERNAL_GAP:
                return True
        return False

    def _can_merge_pair(self, current: SemanticWord, nxt: SemanticWord) -> bool:
        if max(0.0, nxt.start - current.end) > 0.12:
            return False
        pair = (current.text, nxt.text)
        return pair in NO_SPLIT_PAIRS or (current.text + nxt.text) in QUESTION_ENDINGS or (current.text + nxt.text) in CLAUSE_ENDINGS

    def _pick_best_split(self, words: Sequence[SemanticWord], start_idx: int) -> int:
        if start_idx >= len(words) - 1:
            return len(words) - 1
        min_end_idx = self._min_end_idx(words, start_idx)
        hard_end_idx = self._hard_end_idx(words, start_idx, min_end_idx)
        for idx in range(min_end_idx, hard_end_idx + 1):
            text = self._segment_text(words, start_idx, idx)
            if self._is_strong_boundary(text, words, idx):
                return idx
        best_idx = hard_end_idx
        best_score = -10**9
        for idx in range(min_end_idx, hard_end_idx + 1):
            score = self._boundary_score(words, start_idx, idx)
            if score > best_score:
                best_score = score
                best_idx = idx
        return best_idx

    def _min_end_idx(self, words: Sequence[SemanticWord], start_idx: int) -> int:
        idx = start_idx
        while idx < len(words) - 1:
            if self._segment_duration(words, start_idx, idx) >= MIN_DURATION_SECONDS or self._segment_chars(words, start_idx, idx) >= 4:
                return idx
            idx += 1
        return idx

    def _hard_end_idx(self, words: Sequence[SemanticWord], start_idx: int, min_end_idx: int) -> int:
        last_safe = min_end_idx
        for idx in range(min_end_idx, len(words)):
            if self._segment_duration(words, start_idx, idx) <= HARD_DURATION_SECONDS and self._segment_chars(words, start_idx, idx) <= HARD_MAX_CHARS:
                last_safe = idx
                continue
            break
        return last_safe

    def _boundary_score(self, words: Sequence[SemanticWord], start_idx: int, split_idx: int) -> float:
        text = self._segment_text(words, start_idx, split_idx)
        duration = self._segment_duration(words, start_idx, split_idx)
        chars = self._segment_chars(words, start_idx, split_idx)
        gap = self._boundary_gap(words, split_idx)
        next_text = words[split_idx + 1].text if split_idx + 1 < len(words) else ""
        score = 0.0
        score -= abs(duration - TARGET_DURATION_SECONDS)
        score -= abs(chars - TARGET_CHARS) * 0.18
        if gap >= HARD_SPLIT_GAP:
            score += 9.0
        elif gap >= SOFT_SPLIT_GAP:
            score += 4.0
        elif gap >= 0.22:
            score += 1.0
        if self._endswith(text, QUESTION_ENDINGS):
            score += 8.0
        elif self._endswith(text, POLITE_ENDINGS):
            score += 4.5
        elif self._endswith(text, SOFT_ENDINGS):
            score += 1.0
        elif self._endswith(text, CLAUSE_ENDINGS):
            score += 2.0
        if next_text in INTERJECTION_TOKENS and gap >= 0.16:
            score += 2.0
        if duration < MIN_DURATION_SECONDS:
            score -= 4.0
        if chars < 3:
            score -= 3.0
        return score

    def _is_strong_boundary(self, text: str, words: Sequence[SemanticWord], split_idx: int) -> bool:
        gap = self._boundary_gap(words, split_idx)
        next_text = words[split_idx + 1].text if split_idx + 1 < len(words) else ""
        current_text = words[split_idx].text
        if self._endswith(text, QUESTION_ENDINGS):
            return True
        if self._endswith(text, POLITE_ENDINGS) and (gap >= 0.18 or next_text in INTERJECTION_TOKENS):
            return True
        if self._endswith(text, SOFT_ENDINGS) and gap >= 0.5:
            return True
        if current_text in INTERJECTION_TOKENS and gap >= 0.35:
            return True
        return False

    def _choose_punctuation(self, text: str, split_idx: int, words: Sequence[SemanticWord]) -> str:
        gap = self._boundary_gap(words, split_idx)
        current_text = words[split_idx].text
        next_text = words[split_idx + 1].text if split_idx + 1 < len(words) else ""
        if self._endswith(text, QUESTION_ENDINGS):
            return "？"
        if self._endswith(text, POLITE_ENDINGS):
            return "。"
        if self._endswith(text, CLAUSE_ENDINGS):
            return "、" if gap < HARD_SPLIT_GAP else "。"
        if self._endswith(text, SOFT_ENDINGS):
            return "。" if gap >= 0.5 or next_text in INTERJECTION_TOKENS else "、"
        if current_text in INTERJECTION_TOKENS and gap >= 0.35:
            return "。"
        return "。" if gap >= HARD_SPLIT_GAP else ("、" if gap >= SOFT_SPLIT_GAP else "。")

    def _merge_tiny_suffix_segments(self, segments: Sequence[SemanticSegment]) -> List[SemanticSegment]:
        merged: List[SemanticSegment] = []
        for segment in segments:
            duration = segment.end - segment.start
            if merged and (segment.ja_text in TINY_SUFFIXES or (len(segment.ja_text) <= 2 and duration <= 0.28)):
                previous = merged[-1]
                combined = self._strip_punctuation(previous.ja_text) + segment.ja_text
                merged[-1] = SemanticSegment(
                    start=previous.start,
                    end=max(previous.end, segment.end),
                    ja_text=self._append_punctuation(combined, "？" if self._endswith(combined, QUESTION_ENDINGS) else "。"),
                    source_word_range=(previous.source_word_range[0], segment.source_word_range[1]) if previous.source_word_range and segment.source_word_range else previous.source_word_range,
                    segmentation_source=previous.segmentation_source,
                    risk_flags=previous.risk_flags,
                )
                continue
            merged.append(segment)
        return merged

    def _segment_text(self, words: Sequence[SemanticWord], start_idx: int, end_idx: int) -> str:
        return "".join(word.text for word in words[start_idx : end_idx + 1]).strip()

    def _segment_duration(self, words: Sequence[SemanticWord], start_idx: int, end_idx: int) -> float:
        return max(0.0, words[end_idx].end - words[start_idx].start)

    def _segment_chars(self, words: Sequence[SemanticWord], start_idx: int, end_idx: int) -> int:
        return sum(len(word.text) for word in words[start_idx : end_idx + 1])

    def _boundary_gap(self, words: Sequence[SemanticWord], split_idx: int) -> float:
        if split_idx + 1 >= len(words):
            return 0.0
        return max(0.0, words[split_idx + 1].start - words[split_idx].end)

    def _endswith(self, text: str, endings: Sequence[str]) -> bool:
        return any(text.endswith(item) for item in endings)

    def _append_punctuation(self, text: str, punctuation: str) -> str:
        clean = text.rstrip()
        if clean.endswith(TERMINAL_PUNCTUATION + CLAUSE_PUNCTUATION):
            return clean
        return f"{clean}{punctuation}" if punctuation else clean

    def _strip_punctuation(self, text: str) -> str:
        clean = text.strip()
        while clean.endswith(TERMINAL_PUNCTUATION + CLAUSE_PUNCTUATION):
            clean = clean[:-1]
        return clean
