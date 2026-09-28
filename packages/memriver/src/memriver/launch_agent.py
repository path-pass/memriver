"""The macOS LaunchAgent that starts `memriver dream run` every day (spec §9.3).

A per-user agent under ~/Library/LaunchAgents, loaded into the user's GUI
domain: no sudo, no system daemon, and nothing secret in the plist. Whether
the job is loaded is asked of launchd itself (`launchctl print`), so a failed
unload -- or a launchd that cannot answer -- is never mistaken for "not
installed", and a failed replacement puts back what was there before.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from collections.abc import Callable
from pathlib import Path

from memriver_dream.settings import DREAM_LAUNCH_AGENT_LABEL

from .install import replace_atomically

Launchctl = Callable[[list[str]], int]
_ABSENT = 113                               # `launchctl print`: could not find service


class LaunchctlFailed(Exception):
    """launchd did not do what was asked, or could not say; the plist on disk says what
    is left."""


class RestoreFailed(LaunchctlFailed):
    """A replacement failed and putting the previous agent back failed too."""


def plist_path(home: Path, label: str = DREAM_LAUNCH_AGENT_LABEL) -> Path:
    return Path(home) / "Library" / "LaunchAgents" / f"{label}.plist"


def render(*, program: list[str], schedule_at: str, env: dict[str, str], log_path: Path,
           label: str = DREAM_LAUNCH_AGENT_LABEL) -> bytes:
    hour, minute = (int(part) for part in schedule_at.split(":"))
    return plistlib.dumps({
        "Label": label, "ProgramArguments": program,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": env, "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path), "ProcessType": "Background"})


def run_launchctl(args: list[str]) -> int:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True,
                          check=False).returncode


def _loaded(label: str, uid: int, launchctl: Launchctl) -> bool:
    """Loaded or absent; LaunchctlFailed when launchd cannot say -- never "absent"."""
    code = launchctl(["print", f"gui/{uid}/{label}"])
    if code not in (0, _ABSENT):
        raise LaunchctlFailed
    return code == 0


def _unload(label: str, uid: int, launchctl: Launchctl) -> None:
    """Boot a loaded job out; LaunchctlFailed when launchd still has it afterwards. The
    service target names the job by label, so it works with the plist already gone."""
    launchctl(["bootout", f"gui/{uid}/{label}"])
    if _loaded(label, uid, launchctl):
        raise LaunchctlFailed


def _exists(path: Path) -> bool:
    """Whether `path` is there, checked with `lstat` directly rather than
    `Path.exists()`: on Python 3.14 that method swallows every `OSError` (not
    just "nothing here") and reports False, which would read a stat fault
    (EIO, a permission error) as "no plist" -- and a failed replacement's
    rollback would then delete the real file instead of restoring it. Only
    a missing path is absent here; any other OSError propagates, before
    anything is unloaded or written.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _previous_plist(path: Path) -> bytes | None:
    """The plist at `path` before any change is made, or None when there truly
    is none; see `_exists` for why this is not `Path.exists()`."""
    return path.read_bytes() if _exists(path) else None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_atomically(path, data, 0o644, os.replace)


def _restore(path: Path, previous: bytes | None, was_loaded: bool, uid: int,
             launchctl: Launchctl) -> bool:
    """Put back the plist and the loaded state from before a replacement; whether that
    worked. A job that was not loaded is not started."""
    try:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _write(path, previous)
        return not was_loaded or launchctl(["bootstrap", f"gui/{uid}", str(path)]) == 0
    except OSError:
        return False


def install(*, home: Path, plist: bytes, uid: int, launchctl: Launchctl,
            label: str = DREAM_LAUNCH_AGENT_LABEL) -> None:
    path = plist_path(home, label)
    previous = _previous_plist(path)
    was_loaded = _loaded(label, uid, launchctl)     # launchd cannot say: nothing touched
    if was_loaded:
        _unload(label, uid, launchctl)        # still loaded: the old plist stays
    try:
        _write(path, plist)
        if launchctl(["bootstrap", f"gui/{uid}", str(path)]) != 0:
            raise LaunchctlFailed
    except (OSError, LaunchctlFailed) as err:
        if not _restore(path, previous, was_loaded, uid, launchctl):
            raise RestoreFailed from err
        raise


def uninstall(*, home: Path, uid: int, launchctl: Launchctl,
              label: str = DREAM_LAUNCH_AGENT_LABEL) -> bool:
    """True when something was removed; False when nothing was installed."""
    path = plist_path(home, label)
    loaded = _loaded(label, uid, launchctl)         # launchd cannot say: the plist stays
    if not _exists(path) and not loaded:
        return False
    if loaded:
        _unload(label, uid, launchctl)        # still loaded: fail and keep the plist
    path.unlink(missing_ok=True)
    return True
