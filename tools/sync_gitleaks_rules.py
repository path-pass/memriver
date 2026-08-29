#!/usr/bin/env python3
"""Vendor the community gitleaks ruleset into memriver-core as generated data.

Build-time tooling: this is the only part of the project allowed to touch the
network. Run it by hand when the upstream ruleset should be refreshed:

    uv run python tools/sync_gitleaks_rules.py [--ref master]

It rewrites packages/memriver-core/src/memriver_core/gate_rules.py, which is
committed so the runtime stays offline and dependency-free.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import tomllib
import urllib.request
import warnings

SOURCE_URL = ("https://raw.githubusercontent.com/gitleaks/gitleaks/"
              "{ref}/config/gitleaks.toml")
OUTPUT = (pathlib.Path(__file__).resolve().parent.parent / "packages"
          / "memriver-core" / "src" / "memriver_core" / "gate_rules.py")

# Rules dropped because they misfire on prose memory content. Each entry must
# name the false positive that forced it; the false-positive tests in
# packages/memriver-core/tests/test_gate_vendored.py are the source of truth.
EXCLUDED_RULE_IDS: set[str] = set()


def compilable(pattern: str) -> bool:
    """True when Python's `re` accepts the Go/RE2 pattern with its RE2 meaning.

    Warnings are promoted to errors so that constructs Python merely tolerates
    with a different meaning (POSIX classes such as `[[:alnum:]]`, which Python
    reads as a nested set) are skipped rather than silently mis-matching.
    """
    # `\z` (Go's end-of-text anchor) only became valid in Python 3.14; rejecting
    # it explicitly keeps the generated file identical on every interpreter the
    # project supports (requires-python >= 3.12) instead of varying by runtime.
    if r"\z" in pattern:
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            re.compile(pattern)
    except (re.error, Warning, RecursionError):
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default="master",
                    help="gitleaks git ref to fetch the config from")
    ref = ap.parse_args().ref

    url = SOURCE_URL.format(ref=ref)
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https URL
        config = tomllib.loads(resp.read().decode("utf-8"))

    kept: list[tuple[str, str, float | None, int, tuple[str, ...]]] = []
    no_regex: list[str] = []
    skipped: list[str] = []
    excluded: list[str] = []
    for rule in config.get("rules", ()):
        rid = rule["id"]
        pattern = rule.get("regex")
        if not pattern:
            no_regex.append(rid)
        elif rid in EXCLUDED_RULE_IDS:
            excluded.append(rid)
        elif not compilable(pattern):
            skipped.append(rid)
        else:
            entropy = rule.get("entropy")
            kept.append((
                rid,
                pattern,
                float(entropy) if entropy is not None else None,
                int(rule.get("secretGroup", 0)),
                tuple(k.lower() for k in rule.get("keywords", ())),
            ))
    kept.sort(key=lambda r: r[0])

    def block(title: str, ids: list[str]) -> str:
        if not ids:
            return f"# {title}: none\n"
        body = "".join(f"#   {i}\n" for i in sorted(ids))
        return f"# {title} ({len(ids)}):\n{body}"

    header = (
        '"""Secret-detection rules vendored from the gitleaks project.\n\n'
        "GENERATED FILE -- DO NOT EDIT.\n"
        "Regenerate with: uv run python tools/sync_gitleaks_rules.py\n\n"
        f"Source: {url}\n"
        f"Upstream minVersion: {config.get('minVersion', 'unknown')}\n"
        f"Fetched: {datetime.date.today().isoformat()}\n\n"
        "Divergences from upstream gitleaks:\n"
        "  - allowlists/stopwords are not vendored; these rules fire more\n"
        "    often than upstream gitleaks would.\n"
        "  - entropy rejects at `>= threshold`; upstream skips at\n"
        "    `<= threshold`, so this gate is stricter by the boundary case.\n\n"
        "gitleaks is distributed under the MIT License,\n"
        "Copyright (c) 2019 Zachary Rice. The rule patterns below are\n"
        "reproduced from its default configuration under that licence;\n"
        "see https://github.com/gitleaks/gitleaks/blob/master/LICENSE.\n"
        '"""\n'
        "from __future__ import annotations\n\n"
        f"# Rules kept: {len(kept)}\n"
        + block("Skipped: no regex (path-only rules)", no_regex)
        + block("Skipped: pattern is not Python-`re` compatible", skipped)
        + block("Excluded: too noisy for prose memory content", excluded)
        + "\n# (rule_id, pattern, entropy_threshold, secret_group, keywords)\n"
        "VENDORED_RULES: tuple[\n"
        "    tuple[str, str, float | None, int, tuple[str, ...]], ...\n"
        "] = (\n"
    )
    rows = "".join(
        f"    ({rid!r}, {pattern!r}, {entropy!r}, {group!r}, {keywords!r}),\n"
        for rid, pattern, entropy, group, keywords in kept
    )
    OUTPUT.write_text(header + rows + ")\n", encoding="utf-8")
    print(f"wrote {OUTPUT}: {len(kept)} rules, {len(skipped)} incompatible, "
          f"{len(excluded)} excluded, {len(no_regex)} without a regex")


if __name__ == "__main__":
    main()
