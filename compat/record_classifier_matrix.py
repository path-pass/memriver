"""Record what the Python implementation answers for a matrix of [classifier] tables.

    .venv/bin/python -I -B compat/record_classifier_matrix.py OUT.json WORK_DIRECTORY

Writes the fixture `crates/memriver/tests/fixtures/classifier_matrix.json`. Each case is a
settings.toml (text, or hex when it is not UTF-8) or none; the answer is `null` (no table), the
values read (numbers as text, the threshold as its `repr`), or the error: whether the file is
unreadable, the fields named, and the sentence. A case that is about the Codex overrides also
holds `overrides_line`: the sentence `check_codex_overrides` itself refuses the table's
`codex_overrides` with, or `null` when it accepts it. Stores are made under WORK_DIRECTORY, one
per case; nothing else is read or written, and no memory store is opened.

The interpreter is used as it is: nothing about it is changed for the recording. The spellings of
a number are those of `record_settings_matrix.py`, and more for a fraction.

Where the Rust implementation is meant to answer differently, the case says so itself: "rust"
holds what it answers ("reads": the values that are not those of a bare jev table, "fields": the
fields named, or "unreadable") and "why" the reason. These are the differences between the two
TOML parsers that the core matrix already holds. A value that is only too large for its field is
not marked: there the field holds its largest value.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
import unicodedata
from pathlib import Path

from memriver.settings import check_codex_overrides, load_classifier_settings
from memriver_core.settings import SettingsError

_spec = importlib.util.spec_from_file_location(
    "record_settings_matrix", Path(__file__).resolve().parent / "record_settings_matrix.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

PASTED = "sk-" + "q" * 24
JEV = '[classifier]\nexecutor = "jev"\n'
CLAUDE = '[classifier]\nexecutor = "claude"\nexecutor_path = "/opt/bin/claude"\n'
CODEX = '[classifier]\nexecutor = "codex"\nexecutor_path = "/opt/bin/codex"\n'
OVERRIDES = JEV + "[classifier.codex_overrides]\n"
# a value that is no string, no number and no boolean, and the ones that are another type
OTHER = ["5", "0", "1.5", "true", "false", '["x"]', "[]", "{ a = 1 }", "{}", "1979-05-27",
         "1979-05-27T07:32:00Z", "07:32:00"]

cases = []


def case(name, file=None, *, rust=None, why=None, overrides=False):
    """One case; `rust` is what the Rust implementation answers when it differs, `why` why."""
    if isinstance(file, str):
        file = file.encode("utf-8")
    assert (rust is None) == (why is None)
    cases.append({"name": name, "file": file, "rust": rust, "why": why, "overrides": overrides})


def quoted(text: str) -> str:
    """`text` as a TOML basic string (a JSON string is one)."""
    return json.dumps(text)


# --- the two numbers: every spelling of the core matrix, and more for a fraction
FRACTIONS = [
    "0.5", "1", "0.7", "0.999", "1.0000000000000001", "1.0000000000000002", "1e-400", "1e-1",
    "0x1", "0o1", "0b1", "+1", "0x01", "0x0_1", "0b0001", "-0.5", "+0.5", "+1.0", "1.0e0", "2",
    "0.1_5", "1e-0", "0e0", "-inf", "-nan", "+nan", "0.5e0", "5e-1", "5E-1", "10e-1", "0x2",
    '"0.5"', '" 0.5 "', '"0.5 "', '"\\t0.5\\n"', '"\\u001f0.5"', '"\\u001c0.5"', '"\\u00a00.5"',
    '"\\u30000.5"', '"\\u200b0.5"', '"\\ufeff0.5"', '"\\u00850.5"', '"\\u20280.5"', '"\\u000b0.5"',
    '"\\u000c0.5"', '"0.5\\u0000"', '"5e-1"', '"5E-1"', '"+0.5"', '"-0.5"', '"+.5"', '"0_5e-1"',
    '"0.5_0"', '"0._5"', '"0_.5"', '"_0.5"', '"0.5_"', '"0__5"', '"0.5__0"', '" 0_1.0 "',
    '" 0.5_0 "', '" _0.5 "', '"1_0e-1"', '"1e-1_0"', '"1e_1"', '"1_e-1"', '"0_0.5"', '"1"',
    '"1."', '"01"', '"1e0"', '"1e+0"', '"1e-0"', '"0x1"', '"0x1p0"', '"inf"', '"-inf"', '"+inf"',
    '"infinity"', '"INF"', '"iNf"', '"NaN"', '"+nan"', '"-nan"', '"1e-400"', '"5e-324"',
    '"1.0000000000000001"', '"1.0000000000000002"', '"0.99999999999999999"', '"\\uff11"',
    '"\\u0661"', '"0\\uff0e5"', '"0,5"', '"1e"', '"e1"', '"."', '"1 "', '"0.5f"', '"0.5d"',
    '"1_"', '"1__"', '"0.5e"', '"0.5E-"', '"1/2"', '"50%"', '"0.5 0"', '"0. 5"', '"- 0.5"',
    '"--0.5"', '"+-0.5"', '"0.5\\n\\n"', '"\\n\\n0.5"', '"1e-1000000000000000000000"',
    '"1e1000000000000000000000"', '"0e1000000000000000000000"',
    '"0.' + "5" * 5000 + '"', '"1' + "0" * 400 + 'e-400"', '"0.' + "0" * 400 + "1e401" + '"',
    '"' + "0" * 5000 + '.5"', '"0_' + "0" * 4400 + '.5"',
]
SPELLINGS = list(dict.fromkeys(core.FILE + FRACTIONS))
for key in ("timeout_s", "block_threshold"):
    for spelling in SPELLINGS:
        answer, why = core.FILE_DIFFERS.get(spelling, (None, None))
        rust = None
        if answer == "reads" and key == "timeout_s":
            rust = {"reads": {"timeout_s": spelling, "timeout": spelling}}
        elif answer is not None:        # a whole number that is not 1 is no threshold
            rust = {"fields": [f"classifier.{key}"]}
        case(f"{key} = {spelling[:40]}", JEV + f"{key} = {spelling}\n", rust=rust, why=why)
    case(f"{key} in upper case", JEV + f"{key.upper()} = 1\n")
    case(f"{key} twice in two cases, the first one invalid",
         JEV + f'{key} = "x"\n{key.upper()} = 1\n')
    case(f"{key} twice in two cases, the second one invalid",
         JEV + f'{key} = 1\n{key.upper()} = "x"\n')
case("timeout_s on a headless executor", CLAUDE + "timeout_s = 5\n")
case("no timeout_s on claude", CLAUDE)
case("no timeout_s on codex", CODEX)

# --- the three switches
for key in ("enabled", "agent_writes", "dream_writes"):
    for spelling in ["true", "false", "1", "0", "1.0", "0.0", '"true"', '"false"', '"no"', '"1"',
                     '""', "[]", "[true]", "{}", "1979-05-27"]:
        case(f"{key} = {spelling}", JEV + f"{key} = {spelling}\n")
    case(f"{key} in upper case", JEV + f"{key.upper()} = false\n")

# --- the executor and its path
for spelling in ['"jev"', '"claude"', '"codex"', '"JEV"', '"Jev"', '" jev"', '"jev "', '"gpt"',
                 '""', '"jev\\u0000"', "'jev'", '"""jev"""', '"""\njev"""', '"""jev\n"""', *OTHER]:
    case(f"executor = {spelling}", f"[classifier]\nexecutor = {spelling}\n")
    case(f"executor = {spelling}, with a path",
         f'[classifier]\nexecutor = {spelling}\nexecutor_path = "/opt/bin/x"\n')
PATHS = ['"/opt/bin/x"', '"/"', '"//x"', '"/a b"', '"/a\\u0000"', '"/a\\nb"', '"/\\u00e9"',
         '"bin/x"', '""', '" "', '"./x"', '"../x"', '"~/x"', '" /x"', '"\\\\/x"', '"x/"',
         '"C:/x"', '"\\u2215x"', '"\\uff0fx"', "'/x'", *OTHER]
for executor in ("jev", "claude", "codex", "gpt", None):
    head = "[classifier]\n" + (f'executor = "{executor}"\n' if executor else "")
    for spelling in PATHS:
        case(f"executor {executor}, executor_path = {spelling}",
             head + f"executor_path = {spelling}\n")
case("executor_path before an invalid executor",
     '[classifier]\nexecutor_path = "bin/x"\nexecutor = "gpt"\n')
case("executor_path before the executor", '[classifier]\nexecutor_path = "/x"\nexecutor = "claude"\n')
case("executor in upper case", '[classifier]\nEXECUTOR = "jev"\n')
case("executor_path in upper case", '[classifier]\nexecutor = "claude"\nEXECUTOR_PATH = "/x"\n')
case("executor twice in two cases, the first one wins",
     '[classifier]\nExecutor = "jev"\nexecutor = "claude"\nunknown = 1\n')
case("executor twice in two cases, the first one invalid",
     '[classifier]\nExecutor = "gpt"\nexecutor = "jev"\n')
case("executor_path twice in two cases, the first one relative",
     '[classifier]\nexecutor = "claude"\nExecutor_Path = "x"\nexecutor_path = "/x"\n')
case("executor twice in one case", '[classifier]\nexecutor = "jev"\nexecutor = "jev"\n')
case("a key that lowers to executor only by a letter outside ASCII",
     '[classifier]\n"e\\u212aecutor" = "jev"\n"bloc\\u212a_threshold" = 2\nexecutor = "jev"\n')
case("the kelvin sign in a key", JEV + '"bloc\\u212a_threshold" = 2\n')
case("a dotless i in a key", JEV + '"t\\u0130meout_s" = 0\n"T\\u0049MEOUT_S" = 0\n')

# --- the three texts
TEXTS = ['"haiku"', '""', '" "', '"\\t"', '"a b"', '" a "', '"two\\nlines"', '"a\\tb"',
         '"a\\u0000"', '"a\\u007f"', '"\\u00a0"', '"a\\u00a0b"', '"\\u200b"', '"a\\u200bb"',
         '"\\u3000"', '"a\\u3000b"', '"\\u00e9"', '"\\u65e5\\u672c"', '"\\U0001F600"', '"\\ue000"',
         '"\\u0378"', '"a\\u2028b"', '"\\u001f"', '"a\\u001fb"', '"\\u00ad"', '"\\u0085"',
         '"\\u1680"', '"\\ufeff"', '"\\u202e"', '"\\U000E0001"', '"\\U0010FFFF"', '"\\u0300"',
         '"/abs"', '"rel"', "'lit'", '"""multi"""', '"""\nfirst"""', '"""a\nb"""', '"""a\\\n  b"""',
         '"' + "m" * 5000 + '"', *OTHER]
for key in ("model", "claude_settings"):
    for spelling in TEXTS:
        case(f"{key} = {spelling[:40]}", JEV + f"{key} = {spelling}\n")
    case(f"{key} in upper case", JEV + f'{key.upper()} = "/abs"\n')
NAMES = ['"A"', '"_"', '"a1"', '"TYPESAFE_API_KEY"', '"1A"', '""', '" "', '"A-B"', '"A B"', '"A.B"',
         '" A"', '"A "', '"A\\n"', '"\\nA"', '"A\\u0000"', '"\\u00c9"', '"A\\u00e9"', '"\\uff21"',
         '"A\\uff11"', '"A\\u0661"', '"' + "A" * 128 + '"', '"' + "A" * 129 + '"',
         '"_' + "9" * 127 + '"', '"$A"', '"A=B"', f'"{PASTED} x"', f'"{PASTED}"', *OTHER]
for spelling in NAMES:
    case(f"api_key_env = {spelling[:40]}", JEV + f"api_key_env = {spelling}\n")
case("api_key_env in upper case", JEV + 'API_KEY_ENV = "A"\n')

# --- the Codex overrides
A = '"model_provider" = "a"\n'


def over(name, body, head=OVERRIDES):
    case(f"overrides: {name}", head + body, overrides=True)


over("an empty table", "")
over("a whole provider",
     '"model_provider" = "foundry"\n"model" = "deployment-a"\n'
     '"model_providers.foundry.name" = "Foundry"\n'
     '"model_providers.foundry.base_url" = "https://example.invalid/openai/v1"\n'
     '"model_providers.foundry.env_key" = "FOUNDRY_API_KEY"\n'
     '"model_providers.foundry.wire_api" = "responses"\n'
     '"model_providers.foundry.requires_openai_auth" = false\n')
over("a whole provider, the selection last",
     '"model_providers.z-9_.requires_openai_auth" = true\n"model_providers.z-9_.name" = "Z"\n'
     '"model" = "m"\n"model_provider" = "z-9_"\n')
over("the selection and one key", A + '"model_providers.a.env_key" = "AZURE_KEY"\n',
     head=CODEX + "[classifier.codex_overrides]\n")
over("only a model", 'model = "m"\n')
over("only a selection", A)
# the cases of the test file
over("features.hooks", '"features.hooks" = false\n')
over("mcp_servers", '"mcp_servers.sentinel.command" = "sh"\n')
over("web_search", 'web_search = "live"\n')
over("model_instructions_file", 'model_instructions_file = "/tmp/other.md"\n')
over("project_doc_max_bytes", 'project_doc_max_bytes = "1000"\n')
over("a whole table of providers", "[classifier.codex_overrides.model_providers.foundry]\nname = \"Foundry\"\n")
over("a whole provider table", 'model_provider = "foundry"\n"model_providers.foundry" = { name = "Foundry" }\n')
over("a longer key", 'model_provider = "foundry"\n"model_providers.foundry.name.extra" = "x"\n')
over("a bearer token", f'model_provider = "foundry"\n"model_providers.foundry.experimental_bearer_token" = "{PASTED}"\n')
over("a header", f'model_provider = "foundry"\n"model_providers.foundry.http_headers.api-key" = "{PASTED}"\n')
over("a query parameter", f'model_provider = "foundry"\n"model_providers.foundry.query_params.key" = "{PASTED}"\n')
over("a pasted key as env_key", f'model_provider = "foundry"\n"model_providers.foundry.env_key" = "{PASTED}"\n')
over("a pasted key in a key", f'"{PASTED}" = "x"\n')
over("a key with a space", '"model_providers.a b.name" = "x"\n')
over("a provider nothing selects", '"model_providers.a.name" = "A"\n')
over("a provider that is not the selected one", 'model_provider = "a"\n"model_providers.b.name" = "B"\n')
over("two providers", 'model_provider = "a"\n"model_providers.a.name" = "A"\n"model_providers.b.name" = "B"\n')
over("two providers, the selected one last", 'model_provider = "a"\n"model_providers.b.name" = "B"\n"model_providers.a.name" = "A"\n')
over("a provider selected by other letters", 'model_provider = "A"\n"model_providers.a.name" = "A"\n')
over("a provider selected with a space", 'model_provider = "a "\n"model_providers.a.name" = "A"\n')
over("a provider selected before a refused selection", '"model_providers.a.name" = "A"\nmodel_provider = 5\n')
over("a refused key after providers that do not agree", '"model_providers.b.name" = "B"\nmodel_provider = "a"\nother = 1\n')
over("two refused keys", 'zzz = 1\naaa = 2\n')
over("a refused value before a refused key", 'model = ""\nzzz = 1\n')
# keys
for key in ["Model", "MODEL", "Model_Provider", "model ", " model", "", " ", "models", "model.x",
            "model_providers", "model_providers.", "model_providers.a", "model_providers.a.",
            "model_providers..name", "model_providers.a.Name", "Model_Providers.a.name",
            "model_providers.a.name ", "model_providers.a.b.name", "model_providers.a/b.name",
            "model_providers.\u00e9.name", "model_providers.a.name\n", "\u00e9", "a\tb", "a\u0000b",
            "x" * 128, "x" * 129, "model_providers." + "i" * 64 + ".name",
            "model_providers." + "i" * 65 + ".name", "model_providers.A-Z_09.name",
            "model_providers.a.temperature", "model_providers.a.http_headers",
            "model_providers.a.env_http_headers", "model_providers.a.query_params",
            "model_providers.a.experimental_bearer_token", "model_providers.a.auth",
            "model_providers.a.requires_openai_auth.x", "model_reasoning_effort", "sandbox_mode",
            "approval_policy", "profile", "notify", "shell_environment_policy.set.X"]:
    over(f"the key {key[:50]!r}", A + f'{quoted(key)} = "x"\n')
    over(f"the key {key[:50]!r}, true", A + f"{quoted(key)} = true\n")
# values of each key
for key in ("model", "model_provider", "model_providers.a.name", "model_providers.a.base_url",
            "model_providers.a.env_key", "model_providers.a.wire_api",
            "model_providers.a.requires_openai_auth"):
    head = "" if key == "model_provider" else A
    for spelling in ['"a"', '"x"', '""', '" "', '" a "', '"two\\nlines"', '"a\\tb"', '"\\u00a0"',
                     '"a\\u200bb"', '"\\u00e9"', '"responses"', '"Responses"', '" responses"',
                     '"chat"', '"https://example.invalid/v1"', '"A_1"', '"1A"', '"true"',
                     '"false"', "true", "false", "1", "0", "1.0", "[]", "{}", "1979-05-27"]:
        over(f"{key} = {spelling}", head + f'"{key}" = {spelling}\n')
for spelling in NAMES:
    over(f"env_key = {spelling[:40]}", A + f'"model_providers.a.env_key" = {spelling}\n')
# what is no table
for spelling in ["5", '"x"', "[]", '["model", "x"]', '[["model", "x"]]', "true", "1979-05-27",
                 "{}", '{ model = "m" }', "{ model = 5 }"]:
    case(f"overrides: codex_overrides = {spelling}", JEV + f"codex_overrides = {spelling}\n",
         overrides=True)
case("overrides: an array of tables", JEV + '[[classifier.codex_overrides]]\nmodel = "m"\n',
     overrides=True)
case("overrides: the table's name in upper case", JEV + '[classifier.CODEX_OVERRIDES]\nmodel = "m"\n',
     overrides=True)
case("overrides: the table's name in upper case, refused",
     JEV + "[classifier.Codex_Overrides]\nmodel = 5\n", overrides=True)
case("overrides: dotted keys", JEV + 'codex_overrides.model = "m"\ncodex_overrides."model_provider" = "p"\n',
     overrides=True)
case("overrides: bare dotted keys make tables",
     JEV + 'codex_overrides.model_providers.a.name = "A"\ncodex_overrides.model_provider = "a"\n',
     overrides=True)
case("overrides: refused, with other fields",
     '[classifier]\nexecutor = "gpt"\ntimeout_s = 0\n[classifier.codex_overrides]\n"features.hooks" = false\n',
     overrides=True)

# --- the base URL: what urlsplit makes of it
# a character whose compatibility form holds one of the characters a URL is cut at
CUT = [chr(point) for point in range(0x80, 0x110000)
       if chr(point).isprintable() and set("/?#@:") & set(unicodedata.normalize("NFKC", chr(point)))]
URLS = [
    "https://example.invalid/openai/v1", "http://example.invalid", "HTTPS://EXAMPLE.invalid/",
    "hTtP://x", " https://x", "  http://x", "https://x ", "https:// x", "https://x y", "https://",
    "https:///path", "https:/x", "https:x", "https:", "https", "//x", "x", "/x", "://x",
    "ftp://x", "file:///etc/hosts", "ws://x", "httpss://x", "htt://x", "http+x://h", "ht tp://x",
    "1http://x", "http ://x", ":http://x", "\u00e9http://x", "http\u00e9://x", "http\u212a://x",
    "\uff48ttp://x", "http://http://x", "http:http://x", "https://u@x", "https://u:p@x",
    f"https://u:{PASTED}@example.invalid/v1", "https://@x", "https://x@", "https://:@x",
    "https://x/@", "https://x/a@b", "https://x/a:b@c", "https://x:1/@", "https://x?",
    "https://x#", "https://x/?a", "https://x/#a", "https://?x", "https://#x",
    f"https://example.invalid/v1?key={PASTED}", "https://example.invalid/#x", "https://x/%3F",
    "https://x:80", "https://x:abc", "https://x:", "https://x:99999999", "https://x:-1",
    "https://:80", "https://:", "https://:/x", "https://.", "https://-", "https://%41",
    "https://x/%zz", "http://x//y", "https://x;y", "https://x\\y", "https://x\\@y", "https://x/ y",
    "https://x/[", "https://x/]", "https://x/[::1]", "https://x:1/[", "https://ex[ample",
    "https://ex]ample", "https://[::1]", "https://[::1]:80", "https://[::1]:", "https://[::1]:x",
    "https://[::1]x", "https://[::1].x", "https://[::1]/x", "https://[::1", "https://::1]",
    "https://x[::1]", "https://[::1][::1]", "https://[[::1]]", "https://[::1]]", "https://[]",
    "https://[]:80", "https://[ ]", "https://[x]", "https://[1.2.3.4]", "https://[localhost]",
    "https://[v1.x]", "https://[v1.]", "https://[v1]", "https://[vg.x]", "https://[V1.x]",
    "https://[v.x]", "https://[vF0a.:]", "https://[v1.x.y]", "https://[v1. ]", "https://[v1.\u00e9]",
    "https://[v\uff11.x]", "https://[fe80::1%eth0]", "https://[fe80::1%]", "https://[fe80::1%a%b]",
    "https://[fe80::1%25eth0]", "https://[%eth0]", "https://[::%1]", "https://[::ffff:1.2.3.4]",
    "https://[::1.2.3.4]", "https://[1.2.3.4::]", "https://[::ffff:01.2.3.4]",
    "https://[::ffff:1.2.3]", "https://[::ffff:1.2.3.4.5]", "https://[::ffff:256.2.3.4]",
    "https://[::ffff:1.2.3.4:1]", "https://[1:2:3:4:5:6:1.2.3.4]", "https://[1:2:3:4:5:6:7:1.2.3.4]",
    "https://[1:2:3:4:5:1.2.3.4]", "https://[1:2:3:4:5:6:7:8]", "https://[1:2:3:4:5:6:7:8:9]",
    "https://[1:2:3:4:5:6:7]", "https://[1::2::3]", "https://[12345::]", "https://[0000::]",
    "https://[00000::]", "https://[::g]", "https://[::G]", "https://[::F]", "https://[::-1]",
    "https://[::+1]", "https://[:: 1]", "https://[::1 ]", "https://[ ::1]", "https://[::0x1]",
    "https://[:1:2:3:4:5:6:7]", "https://[1:2:3:4:5:6:7::]", "https://[::2:3:4:5:6:7:8]",
    "https://[1:2:3:4::5:6:7:8]", "https://[1:2:3:4:5:6:7:8::]", "https://[::1:2:3:4:5:6:7:8]",
    "https://[::]", "https://[:::]", "https://[:]", "https://[::::]", "https://[1:]", "https://[:1]",
    "https://[1::]", "https://[1:::2]", "https://[::\uff11]", "https://[::\u0661]", "https://[1]",
    "https://[1:2]", "https://[::1%\u00e9]", "https://[::1%/]", "https://[fe80::1%eth0]:80/x",
    "https://\u4f8b\u3048.invalid", "https://\uff45xample.invalid", "https://x\u00e9",
    "https://x/\uff0f", "https://x/\uff1f", "https://x/\uff03", "https://x/\u2100",
    "https://x\u2100y:80", "https://x\uff0e", "https://\u3002", "https://x\u2488",
    "https://\u2488", "https://x\u00a0y", "https://\u2024", "https://x\u2024y",
    *(f"https://x{c}y" for c in CUT), "https://" + "x" * 5000, "https://x/" + "y" * 5000,
]
for url in URLS:
    over(f"base_url = {url[:50]!r}", A + f'"model_providers.a.base_url" = {quoted(url)}\n')

# --- the file as a whole
case("no file")
case("empty file", "")
case("no table", "max_body_chars = 42\n")
case("an empty table", "[classifier]\n")
case("a bare jev table", JEV)
case("a claude table with a model", CLAUDE + 'model = "haiku"\n')
case("a codex table", CODEX)
case("every key set",
     CODEX + 'model = "m"\nclaude_settings = "/s.json"\napi_key_env = "K"\nenabled = false\n'
     "agent_writes = false\ndream_writes = false\ntimeout_s = 7\nblock_threshold = 0.25\n"
     '[classifier.codex_overrides]\nmodel = "o"\n')
case("every key set, in upper case",
     '[classifier]\nEXECUTOR = "codex"\nEXECUTOR_PATH = "/opt/bin/codex"\nMODEL = "m"\n'
     'CLAUDE_SETTINGS = "/s.json"\nAPI_KEY_ENV = "K"\nENABLED = false\nAGENT_WRITES = false\n'
     'DREAM_WRITES = false\nTIMEOUT_S = 7\nBLOCK_THRESHOLD = 0.25\nCODEX_OVERRIDES = { model = "o" }\n')
case("each switch false", JEV + "enabled = false\nagent_writes = false\ndream_writes = false\n")
case("classifier = 5", "classifier = 5\n")
case("classifier is a text", 'classifier = "jev"\n')
case("classifier is an array", "classifier = []\n")
case("classifier is a boolean", "classifier = false\n")
case("classifier is a date", "classifier = 1979-05-27\n")
case("classifier is an array of tables", '[[classifier]]\nexecutor = "jev"\n')
case("classifier is an inline table", 'classifier = { executor = "jev" }\n')
case("classifier is an empty inline table", "classifier = {}\n")
case("classifier in dotted keys", 'classifier.executor = "jev"\nclassifier.timeout_s = 3\n')
case("the table's name in upper case", '[CLASSIFIER]\nexecutor = "jev"\n')
case("the table's name in mixed case, invalid", '[Classifier]\nexecutor = "gpt"\n')
case("the table's name in two cases", '[Classifier]\nexecutor = "gpt"\n[classifier]\nexecutor = "jev"\n')
case("the table's name quoted", '["classifier"]\nexecutor = "jev"\n')
case("the table inside another", '[memriver.classifier]\nexecutor = "gpt"\n')
case("the table after another", '[dream]\nexecutor = "gpt"\n' + JEV)
case("the table before another", JEV + '[dream]\nexecutor = "gpt"\n')
case("a subtable that is not a key", JEV + "[classifier.other]\nx = 1\n")
case("a subtable alone", "[classifier.other]\nx = 1\n")
case("a subtable named like a key", JEV + "[classifier.timeout_s]\nx = 1\n")
case("an invalid core setting beside the table", 'max_body_chars = "x"\n' + JEV)
case("unknown keys", JEV + 'unknown = 1\nroot = "/x"\n_env_file = "x"\n')
case("the old key backend", '[classifier]\nbackend = "jev"\n')
case("the old key jev_model", JEV + 'jev_model = "jev-1"\n')
case("every bad key",
     '[classifier]\nenabled = 1\nagent_writes = 1\ndream_writes = 1\nexecutor = "gpt"\n'
     'model = " "\nclaude_settings = "rel"\ntimeout_s = 0\napi_key_env = "1x"\n'
     'block_threshold = 2\n[classifier.codex_overrides]\n"features.hooks" = false\n')
case("every key bad, the path too",
     "[classifier]\nblock_threshold = 2\ntimeout_s = 0\ndream_writes = 1\nagent_writes = 1\n"
     'enabled = 1\napi_key_env = "1x"\ncodex_overrides = 5\nclaude_settings = "rel"\nmodel = " "\n'
     'executor_path = "rel"\nexecutor = "gpt"\n')
case("every key of another type",
     "[classifier]\nexecutor = 1\nexecutor_path = 1\nmodel = 1\nclaude_settings = 1\n"
     "codex_overrides = 1\napi_key_env = 1\nenabled = 1\nagent_writes = 1\ndream_writes = 1\n"
     "timeout_s = []\nblock_threshold = []\n")
case("claude without a path, and other bad keys", '[classifier]\nexecutor = "claude"\ntimeout_s = 0\nmodel = ""\n')
# the file's bytes
case("a byte order mark", b"\xef\xbb\xbf" + JEV.encode())
case("a byte order mark and no table", b"\xef\xbb\xbfmax_body_chars = 42\n")
case("CRLF", JEV.replace("\n", "\r\n") + "timeout_s = 3\r\n")
case("a bare CR between lines", JEV.replace("\n", "\r") + "timeout_s = 3\r")
case("a bare CR at the end", JEV + "\r")
case("CR CR LF", JEV + "timeout_s = 3\r\r\n")
case("a bare CR in a multi-line text", JEV + 'model = """a\rb"""\n')
case("CRLF in a multi-line text", JEV + 'model = """a\r\nb"""\n')
case("a bare CR in a text", JEV + 'model = "a\rb"\n')
case("a bare CR in a literal text", JEV + "model = 'a\rb'\n")
case("a bare CR in a comment", JEV + "# a\rtimeout_s = 0\n")
case("a line ending escaped in a multi-line text, CRLF", JEV + 'model = """a\\\r\n  b"""\n')
case("a line ending escaped in a multi-line text, CR", JEV + 'model = """a\\\r  b"""\n')
case("without a final newline", JEV.rstrip("\n"))
case("not TOML", "[classifier\n")
case("not TOML after the table", JEV + "this is not = = toml\n")
case("not UTF-8", JEV.encode() + b"a = '\xff'\n")
case("not UTF-8 in a comment", JEV.encode() + b"# \xff\n")
case("a NUL", JEV.encode() + b"\x00")
case("a control character in a comment", JEV.encode() + b"# \x01\n")
case("the table twice", JEV + JEV)
case("a foreign table twice", JEV + "[a]\nx = 1\n[a]\ny = 2\n")
case("a foreign integer that overflows", JEV + "[other]\nx = 9223372036854775808\n")
case("a unicode bare key", JEV + "caf\u00e9 = 1\n")
# what only one of the two parsers takes for TOML
BARE = {"reads": {}}
case("a trailing comma in the inline table", 'classifier = { executor = "jev", }\n',
     rust=BARE, why=core.TOML_1_1)
case("a newline in the inline table", 'classifier = { executor = "jev",\n timeout_s = 3 }\n',
     rust={"reads": {"timeout_s": "3", "timeout": "3"}}, why=core.TOML_1_1)
case("an escape of TOML 1.1 in a model", JEV + 'model = "\\x41"\n',
     rust={"reads": {"model": "A"}}, why=core.TOML_1_1)
case("an escape of TOML 1.1 in a foreign key", JEV + '[other]\nx = "\\e"\n', rust=BARE,
     why=core.TOML_1_1)
case("a time without seconds in a foreign key", JEV + "[other]\nx = 07:32\n", rust=BARE,
     why=core.TOML_1_1)
case("a foreign integer of 4300 digits", JEV + f"[other]\nx = {'9' * 4300}\n")
case("a foreign integer of 4301 digits", JEV + f"[other]\nx = {'9' * 4301}\n", rust=BARE,
     why=core.DIGITS)
case("a foreign prefix without digits", JEV + "[other]\nx = 0x\n", rust=BARE, why=core.NO_DIGITS)
case("a foreign second of 60", JEV + "[other]\nx = 23:59:60\n", rust=BARE, why=core.DATES)
case("a foreign number with a sign inside", JEV + "[other]\nx = 1_0-1\n", rust=BARE, why=core.SIGNS)
case("arrays nested 80 deep", JEV + "[other]\nx = " + "[" * 80 + "]" * 80 + "\n")
case("arrays nested 81 deep", JEV + "[other]\nx = " + "[" * 81 + "]" * 81 + "\n",
     rust={"unreadable": True}, why=core.DEPTH)


def text_of(value: int | None) -> str | None:
    """A value as text: decimal, or hexadecimal where the interpreter will not write it out."""
    if value is None:
        return None
    try:
        return str(value)
    except ValueError:
        return hex(value)


def answer(root: Path) -> dict | None:
    try:
        table = load_classifier_settings(root)
    except SettingsError as err:
        assert err.env_fields == ()
        return {"unreadable": err.unreadable, "fields": list(err.fields), "error": str(err)}
    if table is None:
        return None
    assert all(type(item) in (str, bool) for item in table.codex_overrides.values())
    return {"values": {
        "executor": table.executor, "executor_path": table.executor_path, "model": table.model,
        "claude_settings": table.claude_settings, "codex_overrides": table.codex_overrides,
        "api_key_env": table.api_key_env, "enabled": table.enabled,
        "agent_writes": table.agent_writes, "dream_writes": table.dream_writes,
        "timeout_s": text_of(table.timeout_s), "block_threshold": repr(table.block_threshold),
        "timeout": text_of(table.timeout)}}


def overrides_line(file: bytes) -> str | None:
    """What check_codex_overrides says of the table's codex_overrides, the key in any case."""
    table = tomllib.loads(file.decode("utf-8"))["classifier"]
    value = next(item for key, item in table.items() if key.lower() == "codex_overrides")
    try:
        assert check_codex_overrides(value) is value
    except ValueError as err:
        return str(err)
    return None


def record(work: Path) -> list[dict]:
    rows = []
    seen: dict[str, int] = {}
    for number, c in enumerate(cases):
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
        row["answer"] = answer(root)
        if c["overrides"]:
            row["overrides_line"] = overrides_line(c["file"])
        if c["rust"] is not None:
            row["rust"], row["why"] = c["rust"], c["why"]
        rows.append(row)
    return rows


def main() -> None:
    out, work = Path(sys.argv[1]), Path(sys.argv[2])
    rows = record(work)
    out.write_text("[\n" + ",\n".join(json.dumps(row) for row in rows) + "\n]\n", encoding="ascii")
    refused = sum(row["answer"] is not None and "error" in row["answer"] for row in rows)
    print(len(rows), "cases;", refused, "refused by Python;",
          sum("rust" in row for row in rows), "answered differently by Rust")


if __name__ == "__main__":
    main()
