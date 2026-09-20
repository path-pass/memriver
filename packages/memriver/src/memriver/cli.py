"""Command dispatch for the `memriver` executable.

The bare invocation is load-bearing: every MCP client config in the wild spells
the server as `memriver` or `memriver --root R --project-dir D`, with no
subcommand. Those forms are rewritten to `serve` before parsing, so the
explicit grammar can grow commands without breaking a single existing config.

Handlers import their dependencies lazily. `serve` must not pay for the hook
stack, and -- the expensive direction -- a hook fires on every session start
and every turn end, and must never pay for importing the MCP server.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__

# the only tokens that can legally open the bare compatibility form
_LEGACY_SERVE_FLAGS = ("--root", "--project-dir")


def _add_store_options(parser: argparse.ArgumentParser, *,
                       project_dir_default: Path | None,
                       project_dir_help: str) -> None:
    parser.add_argument("--root", type=Path, default=None,
                        help="storage root, which also holds the optional "
                             "config.toml (default: $MEMRIVER_ROOT or ~/agent-memory)")
    parser.add_argument("--project-dir", type=Path, default=project_dir_default,
                        help=project_dir_help)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memriver",
        description="Shared memory MCP server for coding agents")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser(
        "serve", help="run the MCP server over stdio (the default command)")
    # the bare form rewrites `memriver --root R --version` to `serve --root R
    # --version`, so serve has to answer for --version too
    serve.add_argument("--version", action="version", version=__version__)
    _add_store_options(serve, project_dir_default=Path.cwd(),
                       project_dir_help="project whose 'project' memory scope is "
                                        "used (default: the current working "
                                        "directory)")
    serve.set_defaults(handler=_serve)

    hook = commands.add_parser("hook", help="run a harness hook over stdin/stdout")
    hook.add_argument("event", choices=["session-start", "stop"])
    # spelled out here, like install's below: importing hooks.Harness at parse
    # time would pull memriver_core.models into every invocation, including
    # install/uninstall/--version. test_hook_harness_choices_match_the_literal
    # pins these names to hooks.Harness so the two cannot drift.
    hook.add_argument("--harness", choices=["claude-code", "codex"], required=True)
    _add_store_options(hook, project_dir_default=None,
                       project_dir_help="project whose 'project' memory scope is "
                                        "used (default: the directory the harness "
                                        "reports, else the current working directory)")
    hook.set_defaults(handler=_hook)

    install = commands.add_parser(
        "install", help="configure a harness to use memriver")
    selector = install.add_mutually_exclusive_group()
    selector.add_argument("--harness", choices=["claude-code", "codex", "cursor",
                                                "kiro"],
                          help="install one harness (default: all of them)")
    selector.add_argument("--all", action="store_true",
                          help="install every supported harness (the default)")
    install.add_argument("--yes", action="store_true",
                         help="accept every change shown, without prompting")
    install.add_argument("--dry-run", action="store_true",
                         help="show the plan and write nothing")
    install.set_defaults(handler=_install)

    uninstall = commands.add_parser(
        "uninstall", help="remove a harness's memriver configuration")
    un_selector = uninstall.add_mutually_exclusive_group()
    un_selector.add_argument("--harness", choices=["claude-code", "codex", "cursor",
                                                   "kiro"],
                             help="uninstall one harness (default: all of them)")
    un_selector.add_argument("--all", action="store_true",
                             help="uninstall every supported harness (the default)")
    uninstall.add_argument("--yes", action="store_true",
                           help="accept every change shown, without prompting")
    uninstall.add_argument("--dry-run", action="store_true",
                           help="show the plan and write nothing")
    uninstall.add_argument("--purge-data", action="store_true",
                           help="also delete the memory storage root (default: "
                                "$MEMRIVER_ROOT or ~/agent-memory)")
    uninstall.add_argument("--root", type=Path, default=None,
                           help="storage root to purge with --purge-data "
                                "(default: $MEMRIVER_ROOT or ~/agent-memory)")
    uninstall.add_argument("--clean-uv-cache", action="store_true",
                           help="also run 'uv cache clean' for memriver and "
                                "memriver-core")
    uninstall.set_defaults(handler=_uninstall)

    doctor = commands.add_parser(
        "doctor", help="check the memory store for problems")
    doctor.add_argument("--root", type=Path, default=None,
                        help="storage root to check (default: $MEMRIVER_ROOT or "
                             "~/agent-memory)")
    doctor.add_argument("--json", action="store_true",
                        help="emit a machine-readable JSON report")
    doctor.add_argument("--stale-days", type=_positive_int, default=90,
                        help="days since a memory was last updated before it is "
                             "flagged stale (default: 90)")
    doctor.set_defaults(handler=_doctor)
    return parser


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be a positive integer")
    return parsed


def _normalize_legacy_serve(argv: list[str]) -> list[str]:
    """Rewrite the pre-subcommand invocation forms to an explicit `serve`.

    `--version`, `-h`/`--help` and any explicit command are left alone, and an
    unknown first positional stays a parser error rather than being served.
    """
    if not argv:
        return ["serve"]
    if argv[0].partition("=")[0] in _LEGACY_SERVE_FLAGS:
        return ["serve", *argv]
    return argv


def _serve(args: argparse.Namespace) -> int:
    from memriver_core.config import load_settings
    from pydantic import ValidationError

    from .server import build_server

    try:
        settings = load_settings(root_override=args.root)
    except ValidationError as err:
        # an invalid config *file* is warned about and ignored; only a bad
        # MEMRIVER_* environment variable reaches here, and that is worth
        # failing on -- but as a readable message, not a bare traceback
        raise SystemExit(f"memriver: invalid MEMRIVER_* environment setting\n{err}")
    build_server(root=settings.root, project_dir=args.project_dir,
                 settings=settings).run()  # stdio
    return 0


def _hook(args: argparse.Namespace) -> int:
    from .hooks import run_hook

    try:
        payload_text = sys.stdin.read()
    except (UnicodeDecodeError, OSError):
        # run_hook never raises, but the read happens before it: bytes that are
        # not UTF-8 are malformed input, and a hook that tracebacks over them
        # fails the session it exists to help
        payload_text = ""
    result = run_hook(args.event, args.harness, payload_text,
                      root=args.root, project_dir=args.project_dir, cwd=Path.cwd())
    # only what the hook composed: anything else on stdout is read by the
    # harness as a malformed hook response
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def _install(args: argparse.Namespace) -> int:
    import os

    from .install import HARNESSES, run_install

    # no selector means every harness, so the documented optional grammar has
    # one deterministic meaning rather than a silent no-op
    harnesses = [args.harness] if args.harness else list(HARNESSES)
    return run_install(harnesses, yes=args.yes, dry_run=args.dry_run,
                       home=Path.home(), cwd=Path.cwd(), env=os.environ,
                       input_fn=input, stdout=sys.stdout, replace_file=os.replace)


def _uninstall(args: argparse.Namespace) -> int:
    import os

    from .install import HARNESSES
    from .uninstall import run_uninstall

    harnesses = [args.harness] if args.harness else list(HARNESSES)
    return run_uninstall(harnesses, yes=args.yes, dry_run=args.dry_run,
                         purge_data=args.purge_data,
                         clean_uv_cache=args.clean_uv_cache, root=args.root,
                         home=Path.home(), cwd=Path.cwd(), env=os.environ,
                         input_fn=input, stdout=sys.stdout, replace_file=os.replace)


def _doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    return run_doctor(root=args.root, json_output=args.json,
                      stale_days=args.stale_days,
                      stdout=sys.stdout, stderr=sys.stderr)


def _configure_logging() -> None:
    """Pin memriver's own loggers to stderr, wherever the root logger points.

    Under stdio transport, stdout is the JSON-RPC/hook channel and stderr is
    the only place a loader warning (an unreadable config.toml, an unknown
    key) can surface. `logging.basicConfig` cannot promise that: it is a no-op
    once the root logger has a handler, so a process that embeds `main()`
    after configuring logging to stdout would leak those warnings into the
    protocol stream. Configuring the two memriver loggers directly, and taking
    them off propagation, makes the destination independent of the root.

    Each logger's handlers are replaced outright, not added to: an embedding
    process may have already attached its own handler to `memriver` or
    `memriver_core` before `main()` runs -- a `StreamHandler(sys.stdout)`
    would otherwise still carry warnings into the protocol stream, and a
    `NullHandler` would otherwise still swallow them, since propagation is
    off. Replacing also rebinds to the current `sys.stderr` if it was swapped
    since an earlier call, e.g. under test capture.

    Idempotent: `main()` is an ordinary callable and may be invoked more than
    once in a process, which must not double every warning line.
    """
    for name in ("memriver", "memriver_core"):
        logger = logging.getLogger(name)
        logger.propagate = False
        logger.setLevel(logging.WARNING)
        logger.handlers[:] = [logging.StreamHandler(sys.stderr)]


def main(argv: Sequence[str] | None = None) -> int:
    # before any handler can reach load_settings
    _configure_logging()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(_normalize_legacy_serve(raw))
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
