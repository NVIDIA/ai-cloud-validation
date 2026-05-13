# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""File-based logging utilities for Armada Bridge provider scripts.

Scripts in this provider emit structured JSON to stdout for the isvctl framework to
capture. This module provides a FileLogger that writes human-readable diagnostic output
to a log file, keeping stdout clean for JSON.

Usage::

    from common.logging import get_file_logger

    log = get_file_logger(__name__, log_file="/tmp/armada-bridge.log")
    log.info("Starting network setup")
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_DEFAULT_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_DEFAULT_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
_DEFAULT_BACKUP_COUNT = 3


class FileLogger:
    """Logger that writes to a rotating file, leaving stdout free for JSON output.

    Wraps :class:`logging.Logger` with a :class:`~logging.handlers.RotatingFileHandler`
    so that script diagnostic messages go to a persistent log file rather than mixing
    with the structured JSON that isvctl reads from stdout.

    Args:
        name: Logger name — use ``__name__`` from the calling module.
        log_file: Destination log file path.  Parent directories are created
            automatically.
        level: Minimum log level (``logging.DEBUG``, ``logging.INFO``, …).
            Defaults to ``logging.DEBUG``.
        max_bytes: Maximum file size before rotation.  Defaults to 10 MiB.
        backup_count: Number of rotated files to retain.  Defaults to 3.
        fmt: ``logging.Formatter`` format string.
        date_fmt: ``logging.Formatter`` date format string.
        also_stderr: When ``True``, also emit WARNING+ messages to stderr.
    """

    def __init__(
        self,
        name: str,
        log_file: str | Path,
        level: int = logging.DEBUG,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backup_count: int = _DEFAULT_BACKUP_COUNT,
        fmt: str = _DEFAULT_FORMAT,
        date_fmt: str = _DEFAULT_DATE_FORMAT,
        also_stderr: bool = False,
    ) -> None:
        """Initialise the FileLogger and attach handlers to the underlying logger."""
        self._log_file = Path(log_file)
        self._log_file.parent.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)

        # Avoid duplicating handlers if the same logger name is reused.
        if not self._logger.handlers:
            formatter = logging.Formatter(fmt=fmt, datefmt=date_fmt)

            file_handler = RotatingFileHandler(
                self._log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            self._logger.addHandler(file_handler)

            if also_stderr:
                stderr_handler = logging.StreamHandler(sys.stderr)
                stderr_handler.setFormatter(formatter)
                stderr_handler.setLevel(logging.WARNING)
                self._logger.addHandler(stderr_handler)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a DEBUG-level message."""
        self._logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an INFO-level message."""
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a WARNING-level message."""
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an ERROR-level message."""
        self._logger.error(msg, *args, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a CRITICAL-level message."""
        self._logger.critical(msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an ERROR-level message with the current exception traceback."""
        self._logger.exception(msg, *args, **kwargs)

    def set_level(self, level: int) -> None:
        """Change the effective log level for both the logger and its handlers.

        Args:
            level: A :mod:`logging` level constant such as ``logging.WARNING``.
        """
        self._logger.setLevel(level)
        for handler in self._logger.handlers:
            handler.setLevel(level)

    @property
    def log_file(self) -> Path:
        """Absolute path to the active log file."""
        return self._log_file.resolve()

    @property
    def logger(self) -> logging.Logger:
        """The underlying :class:`logging.Logger` instance."""
        return self._logger


def get_file_logger(
    name: str,
    log_file: str | Path = "/tmp/armada-bridge.log",
    level: int = logging.DEBUG,
    also_stderr: bool = False,
) -> FileLogger:
    """Return a :class:`FileLogger` for ``name`` writing to ``log_file``.

    This is the preferred factory for provider scripts.  Call it once at
    module level and reuse the returned instance::

        log = get_file_logger(__name__)
        log.info("vpc_id=%s", vpc_id)

    Args:
        name: Logger name — pass ``__name__``.
        log_file: Destination path.  Defaults to ``/tmp/armada-bridge.log``.
        level: Minimum log level.  Defaults to ``logging.DEBUG``.
        also_stderr: Mirror WARNING+ to stderr.  Useful during local dev.

    Returns:
        A configured :class:`FileLogger` instance.
    """
    return FileLogger(name=name, log_file=log_file, level=level, also_stderr=also_stderr)
