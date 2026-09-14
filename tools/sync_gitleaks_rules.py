#!/usr/bin/env python3
"""Refresh the vendored gitleaks ruleset in memriver-core.

Build-time tooling: this is the only part of the project allowed to touch the
network. Run it by hand when the upstream ruleset should be refreshed:

    uv run python tools/sync_gitleaks_rules.py [--ref master]

It overwrites
packages/memriver-core/src/memriver_core/content_policy/rules/gitleaks.toml
with the upstream file, verbatim and uncommented, so the copy can be diffed
against upstream. The file is committed; the runtime stays offline and loads it
with tomllib at import. Compiling the patterns is the secret scanner's job --
rules that Python's `re` rejects are skipped there, per interpreter -- so this
script only reports the counts as a sanity check on the download.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import stat
import tempfile
import urllib.request
from collections.abc import Callable

SOURCE_URL = ("https://raw.githubusercontent.com/gitleaks/gitleaks/"
              "{ref}/config/gitleaks.toml")
OUTPUT = (pathlib.Path(__file__).resolve().parent.parent / "packages"
          / "memriver-core" / "src" / "memriver_core" / "content_policy" / "rules"
          / "gitleaks.toml")


def _atomic_write_bytes(path: pathlib.Path, data: bytes, *,
                        validate: Callable[[pathlib.Path], object] | None = None
                        ) -> None:
    """Write `data` to `path` without ever exposing a truncated or bad file.

    Same pattern as the repository's own `_atomic_write`: write to a temp
    sibling in the same directory, then `os.replace`. An interruption or
    ENOSPC mid-write leaves the temp file damaged and `path` untouched,
    instead of truncating the live rules file the scanner imports at startup.

    `validate` is handed the finished temp sibling *before* the replace, so a
    file that parses but the runtime cannot use never reaches `path`: it
    raises, the temp file is unlinked, and the previous good file stands.

    The temp sibling inherits the target's mode when there is one to inherit.
    `os.replace` gives the target's name to the temp file's inode, so without
    this a 0644 rules file a checkout ships would come back 0600 and stop
    being readable by a second user or a packaging job. A path that does not
    exist yet keeps mkstemp's private 0600.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            try:
                os.fchmod(f.fileno(), stat.S_IMODE(path.stat().st_mode))
            except FileNotFoundError:
                pass
        if validate is not None:
            validate(pathlib.Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default="master",
                    help="gitleaks git ref to fetch the config from")
    url = SOURCE_URL.format(ref=ap.parse_args().ref)

    import sys
    import tomllib

    # a fixed https URL with only the git ref interpolated
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()

    # Validate BEFORE overwriting, never after: the secret scanner's
    # import-time load is deliberately unguarded, so a truncated 200, a
    # non-TOML one, or -- legal TOML the loader still chokes on -- a rule
    # missing its `id` would break `import memriver_core.content_policy
    # .secret_scanner` outright, and with it every write. Parsing the payload
    # here catches the first two; the scanner's own loader, run against the
    # temp sibling, is the only thing that catches the third, because it is
    # the check the runtime itself performs. A failed sync must leave the
    # previous good ruleset in place.
    from memriver_core.content_policy.secret_scanner import _load_rules

    raw = tomllib.loads(data.decode("utf-8"))["rules"]
    _atomic_write_bytes(OUTPUT, data, validate=_load_rules)

    loaded = {rule_id for rule_id, *_ in _load_rules(OUTPUT)}
    print(f"wrote {OUTPUT}: {len(raw)} upstream rules, "
          f"{sum(r['id'] in loaded for r in raw)} usable on Python "
          f"{sys.version_info.major}.{sys.version_info.minor}")
    print(f"remember to update the Fetched date in {OUTPUT.with_name('NOTICE.md')}")


if __name__ == "__main__":
    main()
