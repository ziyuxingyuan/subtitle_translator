from __future__ import annotations

import logging

from app.services.debug_log_buffer import debug_log_buffer


LOG_DIR_NAME = "logs"
_DEBUG_LOGGERS = ("app", "translation", "segmentation")
_debug_handler: logging.Handler | None = None


class DebugBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        debug_log_buffer.append(message)


def setup_logging() -> None:
    logging.basicConfig(level=logging.WARNING)
    for name in _DEBUG_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        logger.propagate = False


def set_debug_mode(enabled: bool) -> None:
    global _debug_handler
    debug_log_buffer.set_enabled(bool(enabled))
    if enabled:
        if _debug_handler is None:
            _debug_handler = DebugBufferHandler()
            _debug_handler.setFormatter(logging.Formatter("%(message)s"))
        for name in _DEBUG_LOGGERS:
            logger = logging.getLogger(name)
            logger.setLevel(logging.DEBUG)
            if _debug_handler not in logger.handlers:
                logger.addHandler(_debug_handler)
    else:
        for name in _DEBUG_LOGGERS:
            logger = logging.getLogger(name)
            if _debug_handler and _debug_handler in logger.handlers:
                logger.removeHandler(_debug_handler)
            logger.setLevel(logging.WARNING)
        debug_log_buffer.clear()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
