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

import shutil
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
    from memriver_core.config import storage_root

    if root_override is not None:
        return root_override
    return storage_root(env=env, home=home)


def _refuse_purge_target(given: Path, canonical: Path, *, home: Path,
                         cwd: Path) -> str | None:
    """The refusal text for a target too dangerous to delete, or ``None``.

    Both checks run against ``canonical`` -- the destination ``rmtree`` would
    actually walk -- because a symlinked component anywhere above the leaf
    redirects the deletion somewhere the given spelling never named, and a
    relative path or a ``..`` chain names it only after resolution.

    A leaf that is itself a symlink is still refused outright rather than
    followed: ``--root`` names the store, and a link standing in for it is an
    arrangement memriver will not delete through.
    """
    if given.is_symlink():
        return (
            f"memriver uninstall: {given} is a symlink; memriver will not purge "
            "through it. Replace it with a regular directory (or remove it) and "
            "run uninstall --purge-data again.\n"
        )
    # `home`/`cwd` being relative to the target covers the target *being* one of
    # them and the target being any ancestor of one -- the filesystem root
    # included, since every path is relative to it
    if any(base.resolve().is_relative_to(canonical) for base in (home, cwd)):
        return (
            f"memriver uninstall: {canonical} is too broad a target to delete -- "
            "it holds the home directory, the current directory, or the whole "
            "filesystem. Point --root (or MEMRIVER_ROOT) at the memriver store "
            "itself and run uninstall --purge-data again.\n"
        )
    return None


def _purge_data(*, yes: bool, dry_run: bool, input_fn: Callable[[str], str],
                stdout: TextIO, env: Mapping[str, str], home: Path, cwd: Path,
                root_override: Path | None) -> int:
    """Delete the memory storage root, resolved from the same env/home this run
    was invoked with -- never the real process environment/home, which would
    silently purge a different store than the one ``uninstall`` just edited
    the harness configs for. ``--root`` (``root_override``), when given,
    outranks both.

    The target is canonicalized before anything else looks at it, and it is the
    canonical path -- not the spelling that produced it -- that is shown, that
    every guard runs against, and that ``rmtree`` is handed. The guards run a
    second time immediately before the deletion, because the confirmation
    prompt is a window in which the target can be swapped.
    """
    given = _resolve_storage_root(root_override, env, home)
    if not given.is_absolute():
        given = cwd / given
    canonical = given.resolve()
    stdout.write(f"\nmemory storage root: {canonical}\n")
    refusal = _refuse_purge_target(given, canonical, home=home, cwd=cwd)
    if refusal is not None:
        stdout.write(refusal)
        return 1
    if not canonical.exists():
        stdout.write("no data to remove.\n")
        return 0
    if not canonical.is_dir():
        stdout.write(
            f"memriver uninstall: {canonical} is not a directory; nothing was "
            "removed.\n"
        )
        return 1
    if dry_run:
        stdout.write("dry run: the memory store was not removed.\n")
        return 0
    if not yes:
        try:
            answer = input_fn(
                f"remove the entire memory store at {canonical}? [y/N] ")
        except EOFError:
            stdout.write(
                "memriver uninstall: stdin is not interactive and no answer can be "
                "read; re-run with --yes to purge the memory store shown above.\n"
            )
            return 1
        if answer.strip().lower() not in ("y", "yes"):
            stdout.write("data purge declined; the memory store was left in place.\n")
            return 0
    refusal = _refuse_purge_target(given, canonical, home=home, cwd=cwd)
    if refusal is not None or given.resolve() != canonical:
        stdout.write(refusal or (
            f"memriver uninstall: {given} no longer resolves to {canonical}; "
            "nothing was removed. Check what the path points at and run "
            "uninstall --purge-data again.\n"
        ))
        return 1
    try:
        shutil.rmtree(canonical)
    except OSError as error:
        reason = error.strerror or str(error)
        stdout.write(
            f"memriver uninstall: {canonical} was only partly removed ({reason}); "
            "the harness configuration above was removed successfully. Delete the "
            "remaining directory by hand.\n"
        )
        return 1
    stdout.write(f"removed {canonical}\n")
    return 0


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
