"""Silence memriver_core's own stdlib logging for one CLI-boundary call.

memriver_core logs diagnostic warnings (an unreadable config.toml, a skipped
entry) through stdlib logging, which -- unconfigured, as it is here -- writes
straight to the real process stderr via `logging.lastResort`, bypassing
doctor's/hooks' own explicit stderr text entirely. Both promise exactly one
fixed, path-free stderr line on failure; scoping suppression to one call (and
to the memriver_core logger only) keeps that promise without silencing
logging globally or touching `serve`'s own diagnostics.

stdlib only (logging + contextlib), so importing this stays free for hooks.
"""

from __future__ import annotations

import contextlib
import logging


@contextlib.contextmanager
def quiet_core_logging():
    core_logger = logging.getLogger("memriver_core")
    previous_level = core_logger.level
    core_logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        core_logger.setLevel(previous_level)
