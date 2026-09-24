"""``memriver uninstall``: undo install's config edits, then optionally the rest.

``memriver.install.run_config_uninstall`` (the config-removal pipeline) never imports
``memriver_core`` -- it edits harness config files, it does not know where the
memory store lives. Purging that store, and clearing memriver's own packages
from the ``uv`` cache, both need capabilities the install package deliberately
does not have, so this module -- a sibling of ``doctor.py``, not a part of the
install package -- composes them: run the config removal first, and touch
nothing else unless that exits clean.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from .install import run_config_uninstall

# the two packages memriver's own install ever populates in uv's cache; `uv
# cache clean` takes one package name at a time
_UV_CACHE_PACKAGES = ("memriver", "memriver-core")

# a wedged `uv` must not hang uninstall after config removal already committed
_UV_CACHE_CLEAN_TIMEOUT_SECONDS = 30


def run_uninstall(harnesses: Sequence[str], *, yes: bool, dry_run: bool,
                  purge_data: bool, clean_uv_cache: bool,
                  home: Path, cwd: Path, env: Mapping[str, str],
                  input_fn: Callable[[str], str], stdout: TextIO,
                  replace_file: Callable[[Path, Path], None],
                  root: Path | None = None) -> int:
    """Config removal, then -- only once that succeeds -- the data root and cache.

    ``root``, when given, is the ``--purge-data`` target outright -- it wins
    over both the injected ``env``'s ``MEMRIVER_ROOT`` and the injected
    ``home``'s default, the same precedence ``doctor --root`` already gives
    its own root override.
    """
    exit_code = run_config_uninstall(
        harnesses, yes=yes, dry_run=dry_run, home=home, cwd=cwd, env=env,
        input_fn=input_fn, stdout=stdout, replace_file=replace_file,
    )
    if exit_code != 0:
        return exit_code

    if purge_data:
        exit_code = _purge_data(yes=yes, dry_run=dry_run, input_fn=input_fn,
                                stdout=stdout, env=env, home=home, cwd=cwd,
                                root_override=root)
        if exit_code != 0:
            return exit_code

    if clean_uv_cache and not dry_run:
        _clean_uv_cache(stdout)

    return 0


def _resolve_storage_root(root_override: Path | None, env: Mapping[str, str],
                          home: Path) -> Path:
    # imported here, not at module scope, so a plain uninstall with neither
    # flag never pays for importing memriver_core -- same convention doctor.py
    # already uses for its own memriver_core imports
    from memriver_core.settings import storage_root

    if root_override is not None:
        return root_override
    return storage_root(env=env, home=home)


def _unresolvable(path: Path, detail: str) -> str:
    """The refusal for a path canonicalization cannot answer for at all.

    A symlink loop, a component this user may not traverse: every one of them
    reaches ``_purge_data`` *after* the harness configuration has already been
    removed, so none of them may leave the process by way of a traceback.
    ``Path.resolve`` raises ``OSError`` for most of these and a bare
    ``RuntimeError`` for a loop, which is why both are caught wherever a purge
    path is resolved.
    """
    return (
        f"memriver uninstall: cannot resolve {path} ({detail}); nothing was "
        "removed. Check what the path points at and run uninstall --purge-data "
        "again.\n"
    )


def _refusal_text(refusal) -> str:
    if refusal.kind == "unresolvable":
        return _unresolvable(refusal.path, refusal.detail)
    if refusal.kind == "symlink":
        return (f"memriver uninstall: {refusal.path} is a symlink; memriver will not purge "
                "through it. Replace it with a regular directory (or remove it) and "
                "run uninstall --purge-data again.\n")
    if refusal.kind == "too-broad":
        return (f"memriver uninstall: {refusal.path} is too broad a target to delete "
                "-- it holds the home directory, the current directory, or the "
                "whole filesystem. Point --root (or MEMRIVER_ROOT) at the memriver "
                "store itself and run uninstall --purge-data again.\n")
    if refusal.kind == "not-directory":
        return f"memriver uninstall: {refusal.path} is not a directory; nothing was removed.\n"
    return (f"memriver uninstall: {refusal.path} could not be opened as a directory "
            f"({refusal.detail}); nothing was removed.\n")


def _result_text(result) -> str:
    canonical = result.path
    if result.outcome == "removed":
        return f"removed {canonical}\n"
    if result.outcome == "unopenable-parent":
        return (f"memriver uninstall: {canonical} could not be opened as a "
                f"directory ({result.detail}); nothing was removed.\n")
    if result.outcome == "uncheckable":
        return (f"memriver uninstall: {canonical} could not be checked "
                f"({result.detail}); nothing was removed.\n")
    if result.outcome == "not-confirmed":
        return (f"memriver uninstall: {canonical} is no longer the directory "
                "shown above; nothing was removed. Check what the path points "
                "at and run uninstall --purge-data again.\n")
    if result.outcome == "refused":
        return _refusal_text(result.refusal)
    if result.outcome == "replaced-root":
        return (f"memriver uninstall: the memory store at {canonical} was "
                "emptied, but a different object now stands at that name "
                "and was left alone. Remove the empty directory by hand.\n")
    return _partly_removed(canonical, result.detail, result.replaced)


def _purge_data(*, yes: bool, dry_run: bool, input_fn: Callable[[str], str],
                stdout: TextIO, env: Mapping[str, str], home: Path, cwd: Path,
                root_override: Path | None) -> int:
    """Delete the memory storage root, resolved from the same env/home this run
    was invoked with -- never the real process environment/home, which would
    silently purge a different store than the one ``uninstall`` just edited
    the harness configs for. ``--root`` (``root_override``), when given,
    outranks both.

    The target is canonicalized before anything else looks at it, and it is the
    canonical path -- not the spelling that produced it -- that is shown and
    that every guard runs against. The deletion itself is the core's
    ``plan_purge``/``purge``: the plan holds the directory *object* the user
    was shown open across the prompt and ``purge`` deletes exactly that object,
    so the confirmation prompt is not a window in which the target can be
    swapped for another one. This function only words each outcome.
    """
    from memriver_core.bootstrap import PurgeRefusal, plan_purge, purge

    given = _resolve_storage_root(root_override, env, home)
    plan = plan_purge(given, home=home, cwd=cwd, dry_run=dry_run)
    if isinstance(plan, PurgeRefusal):
        if plan.canonical is not None:
            stdout.write(f"\nmemory storage root: {plan.canonical}\n")
            stdout.write(_refusal_text(plan))
        else:
            stdout.write("\n" + _refusal_text(plan))
        return 1
    with plan:
        stdout.write(f"\nmemory storage root: {plan.canonical}\n")
        if not plan.exists:
            stdout.write("no data to remove.\n")
            return 0
        if dry_run:
            stdout.write("dry run: the memory store was not removed.\n")
            return 0
        if not yes:
            try:
                answer = input_fn(f"remove the entire memory store at {plan.canonical}? [y/N] ")
            except EOFError:
                stdout.write(
                    "memriver uninstall: stdin is not interactive and no answer can "
                    "be read; re-run with --yes to purge the memory store shown "
                    "above.\n")
                return 1
            if answer.strip().lower() not in ("y", "yes"):
                stdout.write("data purge declined; the memory store was left in place.\n")
                return 0
        result = purge(plan)
    stdout.write(_result_text(result))
    return 0 if result.outcome == "removed" else 1


def _partly_removed(canonical: Path, reason: str,
                    replaced: Sequence[Path] = ()) -> str:
    return "".join(
        f"memriver uninstall: {path} was replaced while it was being removed "
        "and was left alone.\n" for path in replaced
    ) + (
        f"memriver uninstall: {canonical} was only partly removed ({reason}); "
        "the harness configuration above was removed successfully. Delete the "
        "remaining directory by hand.\n"
    )


def _clean_uv_cache(stdout: TextIO) -> None:
    """Best-effort ``uv cache clean`` for both memriver packages.

    Deleting the cache of the very interpreter this process runs under is
    safe on POSIX: unlink only removes the directory entry, and any file this
    process still has open (its own installed wheel included) stays valid
    until the last file descriptor referencing it closes. Neither a missing
    ``uv`` nor a nonzero exit is fatal here -- uninstall's own exit code
    already reflects whether the config removal it guards succeeded.
    """
    for package in _UV_CACHE_PACKAGES:
        try:
            result = subprocess.run(
                ["uv", "cache", "clean", package],
                capture_output=True, text=True, check=False,
                timeout=_UV_CACHE_CLEAN_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            stdout.write(
                "\nwarning: uv was not found on PATH; clean the cache manually:\n"
                f"  uv cache clean {package}\n"
            )
            continue
        except subprocess.TimeoutExpired:
            stdout.write(
                f"\nwarning: 'uv cache clean {package}' timed out; run it manually:\n"
                f"  uv cache clean {package}\n"
            )
            continue
        except OSError as error:
            # every other way starting the process can fail -- a `uv` this user
            # may not execute, a binary the kernel refuses -- lands here rather
            # than escaping past an uninstall that already removed the config.
            # `KeyboardInterrupt` and `SystemExit` are not `OSError`, so they
            # still travel.
            stdout.write(
                f"\nwarning: 'uv cache clean {package}' could not be started "
                f"({error.strerror or error}); run it manually:\n"
                f"  uv cache clean {package}\n"
            )
            continue
        if result.returncode != 0:
            stdout.write(
                f"\nwarning: 'uv cache clean {package}' failed; run it manually:\n"
                f"  uv cache clean {package}\n"
            )
