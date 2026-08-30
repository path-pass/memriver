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
import pathlib
import urllib.request

SOURCE_URL = ("https://raw.githubusercontent.com/gitleaks/gitleaks/"
              "{ref}/config/gitleaks.toml")
OUTPUT = (pathlib.Path(__file__).resolve().parent.parent / "packages"
          / "memriver-core" / "src" / "memriver_core" / "content_policy" / "rules"
          / "gitleaks.toml")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default="master",
                    help="gitleaks git ref to fetch the config from")
    url = SOURCE_URL.format(ref=ap.parse_args().ref)

    import sys  # noqa: PLC0415
    import tomllib  # noqa: PLC0415

    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https URL
        data = resp.read()

    # Parse BEFORE overwriting, never after: the secret scanner's import-time
    # tomllib.loads is deliberately unguarded, so a truncated or non-TOML 200
    # written to disk would break `import memriver_core.content_policy
    # .secret_scanner` outright -- and with it every write. A failed sync must
    # leave the previous good ruleset in place.
    raw = tomllib.loads(data.decode("utf-8"))["rules"]
    OUTPUT.write_bytes(data)

    # imported after the write so the counts describe what was just vendored
    from memriver_core.content_policy.secret_scanner import _RULES  # noqa: PLC0415

    loaded = {rule_id for rule_id, *_ in _RULES}
    print(f"wrote {OUTPUT}: {len(raw)} upstream rules, "
          f"{sum(r['id'] in loaded for r in raw)} usable on Python "
          f"{sys.version_info.major}.{sys.version_info.minor}")
    print(f"remember to update the Fetched date in {OUTPUT.with_name('NOTICE.md')}")


if __name__ == "__main__":
    main()
