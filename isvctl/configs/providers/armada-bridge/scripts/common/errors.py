#!/usr/bin/env python3
"""Error handling decorator for Armada Bridge provider scripts."""
from __future__ import annotations

import functools
import json
import traceback
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., int])


def handle_bridge_errors(func: F) -> F:
    """Wrap main() to catch errors and emit structured JSON to stdout.

    - NotImplementedError  → {"success": false, "error": "Not implemented: ..."}, return 1
    - Any other exception  → {"success": false, "error": ..., "error_type": ...}, return 1
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> int:
        try:
            return func(*args, **kwargs)
        except NotImplementedError as exc:
            print(json.dumps({"success": False, "error": f"Not implemented: {exc}"}))
            return 1
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "traceback": traceback.format_exc(),
                    }
                )
            )
            return 1

    return wrapper  # type: ignore[return-value]
