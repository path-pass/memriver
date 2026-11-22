"""Record what the Python settings model answers for a matrix of settings files and variables.

    .venv/bin/python -I -B compat/record_settings_matrix.py OUT.json WORK_DIRECTORY

Writes the fixture `crates/memriver-core/tests/fixtures/settings_matrix.json`. Each case is a
settings.toml (text, or hex when it is not UTF-8) or none, and MEMRIVER_* variables; the answer is
the nine values, as text, or the error sentence. Stores are made under WORK_DIRECTORY, one per
case; nothing else is read or written, and no memory store is opened.

The interpreter is used as it is: nothing about it is changed for the recording, the limit on the
digits of an integer included -- it is part of what the Python implementation answers.

Where the Rust implementation is meant to answer differently, the case says so itself: "rust"
holds what it answers ("reads": the values that are not defaults, or "error": the sentence) and
"why" the reason. A value that is only too large for its field is not marked: there the field
holds its largest value.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from memriver_core.settings import SettingsError, load_settings

FIELDS = ["max_body_chars", "search_limit_max", "search_limit_default", "index_budget_lines",
          "memory_reads_retention_days", "standing_max_count", "standing_max_chars", "tags_max",
          "tags_per_memory_max"]
BIG = "123456789012345678901234567890"
TOML_1_1 = "TOML 1.1 syntax, which the Python parser (TOML 1.0) refuses"
DIGITS = "an integer of more than 4,300 decimal digits, which the Python parser cannot convert"
NO_DIGITS = "a prefix without digits, which only the Python parser refuses as TOML"
DATES = "a second of 60 or a year of 0000, which only the Python parser refuses as TOML"
DEPTH = "nesting deeper than 80, which only the Rust parser refuses"
SATURATED = "both values are the largest a field holds, so the default is not above the max"
KEYS = ["max_body_chars", "memory_reads_retention_days"]
BIG = "123456789012345678901234567890"
# TOML spellings of a value, as written after "key = "
FILE = ["10", "10.0", "10.5", '"10"', '" 10 "', '"10.0"', '"1e1"', '"0x10"', '"+10"', '"-1"', "0",
        "true", '"true"', '""', BIG, f'"{BIG}"',
        "false", "-1", "+10", "-0", "1_000", '"1_000"', "0x10", "0o17", "0b11", "1e1", "1E1", "1e30",
        "10.00", "0.0", "-0.0", "inf", "nan", "+inf", "9223372036854775807", "9223372036854775808",
        "18446744073709551616", "1979-05-27", "1979-05-27T07:32:00Z", "07:32:00", "[10]", "[]",
        "{ a = 1 }", '"0010"', '"-0"', '"+0"', '"10."', '".5"', '"10.00"', '"10.50"', '"1e2"',
        '"1E1"', '"10e0"', '"Infinity"', '"nan"', '"0b11"', '"0o17"', '"10L"', '" "', '"\\t10"',
        '"10\\n"', '"1 0"', '"\\uff11\\uff10"', '"\\u0661\\u0660"', '"1__0"', '"_10"', '"10_"',
        '"True"', '"1,000"', '"10 "', '"\\u00a010"', '"\\u300010"', '"0.0"', '"1.0e1"',
        '"9223372036854775807"', '"9223372036854775808"', '"x"', "'10'", '"""10"""',
        '"' + "9" * 4300 + '"', '"' + "9" * 4301 + '"', '"1' + "0" * 400 + '.0"', "1e400",
        "4294967295", "4294967296", "2147483648", "9007199254740993.0", "1.5e1", "1e-1",
        "9223372036854774784.0", "9223372036854775807.0", "9223372036854775808.0", "1e18", "1e19",
        "9.3e18", "1.0", "-1.0", "5e-324", "1_0.0", "0xFFFFFFFFFFFFFFFFFF", "+0", "00", "1e0",
        '"\u001f10"', '"\u001c10"', '"\u008510"', '"\u200b10"', '"\ufeff10"', '"\u202810"',
        '"\u000b10"', '"\u000c10"', '"\r10\r\n"', '"\u168010"', '"\u200010"', '"\u205f10"',
        '"1_000.0"', '"+10.0"', '" 10.0 "', '"10.0_0"', '"10.0e0"', '".0"', '"0.0"', '"1.0"',
        '"01.0"', '"1.000000000000000000000000000000000000000"', '"1_0_0"', '"+1_0"', '"+-1"',
        '"++1"', '"+ 1"', '"1+"', '"+"', '"-"', '"_"', '"0_1"', '"1.0.0"', '"1._0"', '"1_.0"',
        '"' + "0" * 4300 + '1"', '"+' + "9" * 4300 + '"', '"' + "9" * 4300 + '.0"',
        '"' + "9" * 4299 + '.0"', '"1_' + "9" * 4299 + '"', '"1_' + "9" * 4298 + '"',
        '" ' + "9" * 4300 + ' "', '"1.' + "0" * 5000 + '"', '"18446744073709551615"',
        '"18446744073709551616"', '"18446744073709551615.0"', '"18446744073709551616.0"',
        '"9223372036854775808.0"', '"123456789012345678901234567890.0"',
        '"' + "9" * 4301 + '.0"', '"1_' + "9" * 4300 + '"', '"+' + "9" * 4301 + '"',
        '"0' + "9" * 4301 + '"', '"' + "9" * 2150 + "_" + "9" * 2150 + '"',
        "1" + "0" * 4300, "1" + "0" * 5000, "0x" + "F" * 4000]
ENV = ["10", "10.0", "10.5", '"10"', " 10 ", "1e1", "0x10", "+10", "-1", "0", "true", "", BIG,
       "false", "-0", "+0", "1_000", "1__0", "_10", "10_", "0010", "10.", ".5", "10.00", "10.50",
       "1e2", "1E1", "Infinity", "nan", "0b11", "0o17", "10L", " ", "\t10", "10\n", "1 0",
       "\uff11\uff10", "\u0661\u0660", "True", "1,000", "\u00a010", "\u300010", "0.0", "null",
       "None", "x", "9223372036854775807", "9223372036854775808", "18446744073709551616",
       "9" * 4300, "9" * 4301, "[10]", "{}", "'10'", "4294967296", "1" + "0" * 400 + ".0",
       "\x1f10", "\x1c10", "\x8510", "\u200b10", "\ufeff10", "\u202810", "\x0b10", "\x0c10",
       "1_000.0", "+10.0", " 10.0 ", ".0", "1.0", "01.0", "+", "-", "1_0_0", "0_1", "1.0.0",
       "0" * 4300 + "1", "+" + "9" * 4300, "9" * 4300 + ".0", "1_" + "9" * 4299,
       "18446744073709551615.0", "18446744073709551616.0", "123456789012345678901234567890.0"]

# Spellings on which the two implementations differ when they are the value of a setting: what
# the Rust one answers ("reads" the value, or calls the field "invalid") and why.
FILE_DIFFERS = {
    "1" + "0" * 4300: ("reads", DIGITS), "1" + "0" * 5000: ("reads", DIGITS),
    "0x": ("invalid", NO_DIGITS), "0o": ("invalid", NO_DIGITS), "0b": ("invalid", NO_DIGITS),
    "23:59:60": ("invalid", DATES), "1979-05-27T07:32:60Z": ("invalid", DATES),
    "0000-01-01": ("invalid", DATES),
}
FILE += ["9" * 4300, "0x", "0o", "0b", "23:59:60", "1979-05-27T07:32:60Z", "0000-01-01"]
assert set(FILE_DIFFERS) <= set(FILE)

cases = []


def case(name, file=None, env=None, *, reads=None, error=None, why=None):
    """One case; `reads` or `error` is what the Rust implementation answers when it differs."""
    if isinstance(file, str):
        file = file.encode("utf-8")
    row = {"name": name, "file": file, "env": env or {}}
    if why is not None:
        row["rust"] = {"error": error} if error is not None else {"reads": reads or {}}
        row["why"] = why
    cases.append(row)


for key in KEYS:
    for spelling in FILE:
        answer, why = FILE_DIFFERS.get(spelling, (None, None))
        case(f"file {key} = {spelling[:40]}", f"{key} = {spelling}\n", why=why,
             reads={key: spelling} if answer == "reads" else None,
             error=f"settings.toml is invalid: field {key}" if answer == "invalid" else None)
    for value in ENV:
        case(f"env {key} = {value[:40]!r}", None, {f"MEMRIVER_{key.upper()}": value})

READS_42 = {"reads": {"max_body_chars": "42"}, "why": TOML_1_1}
# names: keys of the file and of the environment
case("file key in upper case", "MAX_BODY_CHARS = 42\n")
case("file key in mixed case", "Max_Body_Chars = 42\n")
case("file key twice in two cases, lower first", "max_body_chars = 1\nMAX_BODY_CHARS = 2\n")
case("file key twice in two cases, upper first", "MAX_BODY_CHARS = 2\nmax_body_chars = 1\n")
case("file key in two cases, the first one invalid", 'max_body_chars = "x"\nMAX_BODY_CHARS = 2\n')
case("file key in two cases, the second one invalid", 'max_body_chars = 1\nMAX_BODY_CHARS = "x"\n')
case("file key in upper case, invalid", 'MAX_BODY_CHARS = "x"\n')
case("file key in upper case, invalid, with the variable valid", 'MAX_BODY_CHARS = "x"\n',
     {"MEMRIVER_MAX_BODY_CHARS": "7"})
case("file key quoted", '"max_body_chars" = 42\n')
case("file key dotted", "max_body_chars.x = 1\n")
case("file key the same twice", "max_body_chars = 1\nmax_body_chars = 2\n")
case("file key with the prefix", "memriver_max_body_chars = 42\nMEMRIVER_MAX_BODY_CHARS = 43\n")
case("file key inside a table", "[core]\nmax_body_chars = 42\n")
case("file table named like a key", "[max_body_chars]\nnested = 1\n")
case("file array of tables named like a key", "[[max_body_chars]]\nnested = 1\n")
case("file root key", 'root = "/somewhere/else"\nmax_body_chars = 42\n')
case("file root key that is a number", "root = 5\nmax_body_chars = 42\n")
case("file unknown key and tables", 'not_a_setting = 1\nmax_body_chars = 42\n[plugin]\nexecutor = "codex"\n')
case("file constructor options", '_cli_parse_args = true\n_secrets_dir = "/nowhere"\n')
case("env name in lower case", None, {"memriver_max_body_chars": "42"})
case("env name in mixed case", None, {"Memriver_Max_Body_Chars": "42"})
case("env name in lower case, invalid", None, {"memriver_max_body_chars": "x"})
case("env unknown variable", None, {"MEMRIVER_PLUGIN": "notjson", "MEMRIVER_": "1", "MEMRIVER_MAX": "1"})
case("env without the prefix", None, {"MAX_BODY_CHARS": "42"})
case("env root in lower case does not move the root", None, {"memriver_root": "/nowhere/else"})
# the file as a whole
case("no file")
case("empty file", "")
case("file of comments", "# nothing\n\n")
case("file with a byte order mark", b"\xef\xbb\xbfmax_body_chars = 42\n")
case("file with CRLF", "max_body_chars = 42\r\nindex_budget_lines = 7\r\n")
case("file with a bare CR", "max_body_chars = 42\rindex_budget_lines = 7\n")
case("file without a final newline", "max_body_chars = 42")
case("file that is not TOML", "this is not = = valid toml\n")
case("file that is not UTF-8", b"a = '\xff'\n")
case("file that is not UTF-8 in a comment", b"# \xff\nmax_body_chars = 42\n")
case("file with a NUL", b"max_body_chars = 42\n\x00")
case("file with a control character in a comment", b"# \x01\nmax_body_chars = 42\n")
case("file with a foreign value that overflows", "other = 9223372036854775808\nmax_body_chars = 42\n")
case("file with a foreign float that overflows", "other = 1e400\nmax_body_chars = 42\n")
case("file with a foreign table redefined", "[a]\nx = 1\n[a]\ny = 2\n")
case("file with a foreign invalid date", "other = 1979-13-40\nmax_body_chars = 42\n")
case("file with a newline in an inline table", "other = { a = 1,\n b = 2 }\nmax_body_chars = 42\n", **READS_42)
case("file with a trailing comma in an inline table", "other = { a = 1, }\nmax_body_chars = 42\n", **READS_42)
case("file with an escape of TOML 1.1", 'other = "\\x41 \\e"\nmax_body_chars = 42\n', **READS_42)
case("file with a time without seconds", "other = 07:32\nmax_body_chars = 42\n", **READS_42)
case("file with a unicode bare key", "caf\u00e9 = 1\nmax_body_chars = 42\n")
# sources together
case("env beats file", "max_body_chars = 42\nindex_budget_lines = 7\n", {"MEMRIVER_MAX_BODY_CHARS": "99"})
case("valid env hides an invalid file value", 'max_body_chars = "x"\n', {"MEMRIVER_MAX_BODY_CHARS": "99"})
case("invalid env over a valid file value", "max_body_chars = 42\n", {"MEMRIVER_MAX_BODY_CHARS": "x"})
case("env and file both invalid, different fields", 'index_budget_lines = "x"\n', {"MEMRIVER_MAX_BODY_CHARS": "abc"})
case("every field invalid in the file", "".join(f"{f} = 0\n" for f in reversed(FIELDS)))
case("every field invalid in the environment", None, {f"MEMRIVER_{f.upper()}": "0" for f in FIELDS})
case("two fields invalid in the file", 'max_body_chars = 0\nindex_budget_lines = "x"\n')
case("default above the file's max", "search_limit_max = 3\n")
case("default above the environment's max", None, {"MEMRIVER_SEARCH_LIMIT_MAX": "3"})
case("default above the environment's max, the file holding other keys", "index_budget_lines = 7\n", {"MEMRIVER_SEARCH_LIMIT_MAX": "3"})
case("file default above the default max", "search_limit_default = 100\n")
case("file default above the file's max", "search_limit_default = 9\nsearch_limit_max = 8\n")
case("file default equal to the file's max", "search_limit_default = 8\nsearch_limit_max = 8\n")
case("env default above the file's max", "search_limit_max = 8\nindex_budget_lines = 7\n", {"MEMRIVER_SEARCH_LIMIT_DEFAULT": "9"})
case("file default above the environment's max", "search_limit_default = 9\n", {"MEMRIVER_SEARCH_LIMIT_MAX": "8"})
case("file default under the environment's lowered max", "search_limit_default = 2\n", {"MEMRIVER_SEARCH_LIMIT_MAX": "3"})
case("file default under the environment's raised max", "search_limit_default = 80\n", {"MEMRIVER_SEARCH_LIMIT_MAX": "90"})
case("invalid max and a default", 'search_limit_max = "x"\nsearch_limit_default = 100\n')
case("invalid max and no default", 'search_limit_max = "x"\n')
case("zero max and a default", "search_limit_max = 0\nsearch_limit_default = 100\n")
case("invalid default and a low max", 'search_limit_max = 3\nsearch_limit_default = "x"\n')
case("env max invalid, file default high", "search_limit_default = 100\n", {"MEMRIVER_SEARCH_LIMIT_MAX": "x"})
case("file max low, env default set under it", "search_limit_max = 3\n", {"MEMRIVER_SEARCH_LIMIT_DEFAULT": "2"})
case("file max low and upper case, default untouched", "SEARCH_LIMIT_MAX = 3\n")
case("file default high and upper case", "SEARCH_LIMIT_DEFAULT = 100\n")
case("precedence across layers", "max_body_chars = 3\nsearch_limit_max = 30\nindex_budget_lines = 7\n",
     {"MEMRIVER_MAX_BODY_CHARS": "2", "MEMRIVER_SEARCH_LIMIT_MAX": "20"})
case("retention from the file", "memory_reads_retention_days = 60\n")
# an integer the Python parser cannot convert, wherever it stands
case("file with a foreign integer of 4300 digits", f"other = {'9' * 4300}\nmax_body_chars = 42\n")
case("file with a foreign integer of 4301 digits", f"other = {'9' * 4301}\nmax_body_chars = 42\n",
     reads={"max_body_chars": "42"}, why=DIGITS)
case("file with a foreign integer of 4301 digits in a table",
     f"max_body_chars = 42\n[other]\nx = {'9' * 4301}\n", reads={"max_body_chars": "42"}, why=DIGITS)
# what only one of the two parsers takes for TOML, on a key that is not a setting
for spelling in ("0x", "0o", "0b"):
    case(f"file with a foreign {spelling} without digits", f"other = {spelling}\nmax_body_chars = 42\n",
         reads={"max_body_chars": "42"}, why=NO_DIGITS)
for spelling in ("23:59:60", "1979-05-27T07:32:60Z", "0000-01-01", "0000-01-01T00:00:00Z"):
    case(f"file with a foreign {spelling}", f"other = {spelling}\nmax_body_chars = 42\n",
         reads={"max_body_chars": "42"}, why=DATES)
case("file with a foreign second of 59", "other = 23:59:59\nmax_body_chars = 42\n")
case("file with a foreign year of 0001", "other = 0001-01-01\nmax_body_chars = 42\n")
UNREADABLE = "settings.toml could not be read"
for depth in (80, 81, 100):
    differs = {"error": UNREADABLE, "why": DEPTH} if depth > 80 else {}
    tail = "\nmax_body_chars = 42\n"
    case(f"file with arrays nested {depth} deep", "other = " + "[" * depth + "]" * depth + tail,
         **differs)
    case(f"file with inline tables nested {depth} deep",
         "other = " + "{a=" * (depth - 1) + "{}" + "}" * (depth - 1) + tail, **differs)
    case(f"file with a dotted key of {depth} parts", ".".join("a" * depth) + " = 1" + tail, **differs)
    case(f"file with a table header of {depth} parts",
         "max_body_chars = 42\n[" + ".".join("a" * depth) + "]\nx = 1\n", **differs)
# both too large for their fields: there the two are equal, whatever was written
TOP = 2 ** 64 - 1
case("default above the max, both beyond a field",
     f"search_limit_max = {TOP}\nsearch_limit_default = {TOP + 1}\n",
     reads={"search_limit_max": str(TOP), "search_limit_default": str(TOP + 1)}, why=SATURATED)
case("default above the max, both beyond a field, from the environment", None,
     {"MEMRIVER_SEARCH_LIMIT_MAX": str(TOP), "MEMRIVER_SEARCH_LIMIT_DEFAULT": str(TOP + 1)},
     reads={"search_limit_max": str(TOP), "search_limit_default": str(TOP + 1)}, why=SATURATED)
case("default equal to the max, both beyond a field",
     f"search_limit_max = {TOP + 1}\nsearch_limit_default = {TOP + 1}\n")
case("default above the max, the max the largest a field holds",
     f"search_limit_max = {TOP - 1}\nsearch_limit_default = {TOP}\n")


def text_of(value: int | None) -> str | None:
    """A value as text: decimal, or hexadecimal where the interpreter will not write it out."""
    if value is None:
        return None
    try:
        return str(value)
    except ValueError:
        return hex(value)


def record(work: Path) -> list[dict]:
    rows = []
    seen: dict[str, int] = {}
    for number, c in enumerate(cases):
        for name in [k for k in os.environ if k.upper().startswith("MEMRIVER_")]:
            del os.environ[name]
        root = work / f"case-{number}"
        root.mkdir()
        # names are cut for reading: keep them distinct
        seen[c["name"]] = seen.get(c["name"], 0) + 1
        row = {"name": c["name"] if seen[c["name"]] == 1 else f"{c['name']} #{seen[c['name']]}"}
        if c["file"] is not None:
            (root / "settings.toml").write_bytes(c["file"])
            try:
                row["file"] = c["file"].decode("utf-8")
            except UnicodeDecodeError:
                row["file_hex"] = c["file"].hex()
        row["env"] = c["env"]
        os.environ.update(c["env"])
        try:
            settings = load_settings(root_override=root)
            assert settings.root == root
            row["values"] = {f: text_of(getattr(settings, f)) for f in FIELDS}
        except SettingsError as err:
            row["error"] = str(err)
        for name in c["env"]:
            os.environ.pop(name, None)
        for extra in ("rust", "why"):
            if extra in c:
                row[extra] = c[extra]
        rows.append(row)
    return rows


def main() -> None:
    out, work = Path(sys.argv[1]), Path(sys.argv[2])
    rows = record(work)
    out.write_text("[\n" + ",\n".join(json.dumps(row) for row in rows) + "\n]\n", encoding="ascii")
    print(len(rows), "cases;", sum("error" in row for row in rows), "refused by Python;",
          sum("rust" in row for row in rows), "answered differently by Rust")


if __name__ == "__main__":
    main()
