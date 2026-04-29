from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from modules.semantic_llm_validator import ValidationResult, punctuation_only_changed


Requester = Callable[[list[dict[str, str]]], str]


@dataclass(frozen=True)
class RefinementRequest:
    text: str
    prev_text: str = ''
    next_text: str = ''


@dataclass(frozen=True)
class RefinementResult:
    success: bool
    text: str
    validation: ValidationResult
    raw_response: str


class SemanticLLMRefiner:
    def __init__(self, requester: Requester, system_prompt: str | None = None) -> None:
        self._requester = requester
        self._system_prompt = system_prompt or (
            '你是一个受限的日文字幕标点修复器。'
            '你只能在原文中添加标点符号，不允许删除、改写、替换、重排任何字符。'
            '输出只能是修复后的单条文本，不要解释。'
        )

    def refine(self, request: RefinementRequest) -> RefinementResult:
        messages = self._build_messages(request)
        raw_response = str(self._requester(messages)).strip()
        validation = punctuation_only_changed(request.text, raw_response)
        if not validation.valid:
            return RefinementResult(
                success=False,
                text=request.text,
                validation=validation,
                raw_response=raw_response,
            )
        return RefinementResult(
            success=True,
            text=raw_response,
            validation=validation,
            raw_response=raw_response,
        )

    def _build_messages(self, request: RefinementRequest) -> list[dict[str, str]]:
        context_lines: list[str] = []
        if request.prev_text.strip():
            context_lines.append(f'上文：{request.prev_text.strip()}')
        if request.next_text.strip():
            context_lines.append(f'下文：{request.next_text.strip()}')
        context = '\n'.join(context_lines)
        user_content = (
            '任务：只给当前文本添加合适的日文标点。\n'
            '硬性要求：\n'
            '1. 不允许删除任何原字符。\n'
            '2. 不允许替换任何原字符。\n'
            '3. 不允许新增词语。\n'
            '4. 不允许调整顺序。\n'
            '5. 只输出修复后的单条文本。\n'
        )
        if context:
            user_content += context + '\n'
        user_content += f'当前文本：{request.text}'
        return [
            {'role': 'system', 'content': self._system_prompt},
            {'role': 'user', 'content': user_content},
        ]
