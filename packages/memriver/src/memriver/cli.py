from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(prog="memriver",
                                     description="Shared memory MCP server for coding agents")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--root", type=Path, default=None,
                        help="storage root (default: $MEMRIVER_ROOT or ~/agent-memory)")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(),
                        help="project whose 'project' memory scope is used "
                             "(default: the current working directory)")
    args = parser.parse_args()

    if args.version:
        print(__version__)
        return

    from memriver_core.scope import storage_root

    from .server import build_server

    root = args.root or storage_root()
    build_server(root=root, project_dir=args.project_dir).run()  # stdio


if __name__ == "__main__":
    main()
