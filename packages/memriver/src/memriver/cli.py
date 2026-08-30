from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(prog="memriver",
                                     description="Shared memory MCP server for coding agents")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--root", type=Path, default=None,
                        help="storage root, which also holds the optional "
                             "config.toml (default: $MEMRIVER_ROOT or ~/agent-memory)")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(),
                        help="project whose 'project' memory scope is used "
                             "(default: the current working directory)")
    args = parser.parse_args()

    if args.version:
        print(__version__)
        return

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


if __name__ == "__main__":
    main()
