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
}

MASKS: dict[str, re.Pattern[str]] = {
    "version-string": re.compile(r"\A[0-9]+\.[0-9]+\.[0-9]+\n\Z"),
}
