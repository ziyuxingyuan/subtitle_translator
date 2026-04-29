from __future__ import annotations

from threading import Lock
from typing import List, Tuple


class DebugLogBuffer:
    def __init__(self, max_entries: int = 4000) -> None:
        self._max_entries = max_entries
        self._entries: List[Tuple[int, str]] = []
        self._next_index = 1
        self._enabled = False
        self._lock = Lock()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._next_index = 1

    def append(self, message: str) -> None:
        if not message:
            return
        with self._lock:
            if not self._enabled:
                return
            msg = message if message.endswith("\n") else f"{message}\n"
            entry = (self._next_index, msg)
            self._next_index += 1
            self._entries.append(entry)
            if len(self._entries) > self._max_entries:
                overflow = len(self._entries) - self._max_entries
                if overflow > 0:
                    self._entries = self._entries[overflow:]

    def get_since(self, last_index: int) -> Tuple[List[str], int, bool]:
        with self._lock:
            if not self._entries:
                return [], last_index, False
            oldest_index = self._entries[0][0]
            newest_index = self._entries[-1][0]
            if last_index < oldest_index - 1:
                messages = [entry[1] for entry in self._entries]
                return messages, newest_index, True
            messages = [entry[1] for entry in self._entries if entry[0] > last_index]
            return messages, newest_index, False


debug_log_buffer = DebugLogBuffer()
