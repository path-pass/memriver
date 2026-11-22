"""Every difference between the two implementations a compat case may allow.

A name maps to the item number of the list of differences in the design (0: the
version itself, which is not an item of that list). A case names the ones it
expects (`allow=`); anything else that differs fails. `MASKS` says what part of
an output a name covers: that part is compared by presence only.
"""

from __future__ import annotations

import re

DIFFERENCES: dict[str, int] = {
    "version-string": 0,   # Python prints 0.1.0, Rust 0.2.0
    # a store below schema 6: Python names a stale version, Rust says what can upgrade
    "needs-upgrade-line": 10,
}

MASKS: dict[str, re.Pattern[str]] = {
    "version-string": re.compile(r"\A[0-9]+\.[0-9]+\.[0-9]+\n\Z"),
    # the end of the one line, after the version found, which stays compared
    "needs-upgrade-line": re.compile(
        r"(?<=; this memriver )needs schema version "
        r"(?:4|6\. Upgrade the store to version 6 with an earlier memriver release first)\n\Z"),
}
