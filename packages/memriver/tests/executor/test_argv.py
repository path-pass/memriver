"""The argv each harness run gets, pinned byte for byte against the argv memriver
dream's executors and the classifier's backends built for the same inputs when each
kept its own copy of the runner: a changed flag, value or order fails here."""

from __future__ import annotations

from pathlib import Path

from memriver.executor.harness import claude_argv, codex_argv

SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}
SCHEMA_JSON = ('{"type": "object", "required": ["summary"], '
               '"properties": {"summary": {"type": "string"}}}')
FILES = Path("/scratch/files")
PROVIDER = {"model_provider": "foundry", "model": 'deploy "a"',
            "model_providers.foundry.env_key": "FOUNDRY_API_KEY",
            "model_providers.foundry.requires_openai_auth": False}

CLAUDE = ["/opt/bin/claude", "-p", "--system-prompt", "SYS", "--restricted",
          "--strict-mcp-config", "--tools", "", "--no-session-persistence",
          "--output-format", "json", "--json-schema", SCHEMA_JSON]
CODEX_HEAD = ["/opt/bin/codex", "exec", "--json", "--ephemeral", "--ignore-user-config",
              "--skip-git-repo-check", "--sandbox", "read-only"]
CODEX_SWITCHES = ["--disable", "hooks", "--disable", "shell_tool", "--disable", "unified_exec",
                  "--disable", "code_mode_host", "--disable", "multi_agent",
                  "--disable", "sleep_tool", "--disable", "goals",
                  "--disable", "image_generation", "--disable", "view_image",
                  "--disable", "plugins"]
CODEX_TAIL = ["-c", 'web_search="disabled"', "--output-schema", "/scratch/files/schema.json",
              "-c", 'model_instructions_file="/scratch/files/instructions.md"',
              "-c", "project_doc_max_bytes=0", "-o", "/scratch/files/last-message.json", "-"]
PROVIDER_ARGS = ["-c", 'model_provider="foundry"', "-c", 'model="deploy \\"a\\""',
                 "-c", 'model_providers.foundry.env_key="FOUNDRY_API_KEY"',
                 "-c", "model_providers.foundry.requires_openai_auth=false"]


def test_dream_claude_argv_without_and_with_its_settings_file():
    assert claude_argv("/opt/bin/claude", system_prompt="SYS", schema=SCHEMA) == CLAUDE
    assert claude_argv("/opt/bin/claude", system_prompt="SYS", schema=SCHEMA,
                       settings_path="/etc/memriver/auth.json") == [
        *CLAUDE, "--settings", "/etc/memriver/auth.json"]


def test_classifier_claude_argv_with_a_model_and_a_settings_file():
    assert claude_argv("/opt/bin/claude", system_prompt="SYS", schema=SCHEMA, model="haiku",
                       settings_path="/etc/memriver/auth.json") == [
        *CLAUDE, "--model", "haiku", "--settings", "/etc/memriver/auth.json"]


def test_dream_codex_argv_without_and_with_provider_overrides():
    assert codex_argv("/opt/bin/codex", files=FILES) == [
        *CODEX_HEAD, *CODEX_SWITCHES, *CODEX_TAIL]
    assert codex_argv("/opt/bin/codex", files=FILES, overrides=PROVIDER) == [
        *CODEX_HEAD, *PROVIDER_ARGS, *CODEX_SWITCHES, *CODEX_TAIL]


def test_classifier_codex_argv_puts_its_model_after_the_overrides_so_the_model_wins():
    assert codex_argv("/opt/bin/codex", files=FILES, model="gpt-5-mini",
                      overrides=PROVIDER) == [
        *CODEX_HEAD, *PROVIDER_ARGS, "-c", 'model="gpt-5-mini"', *CODEX_SWITCHES, *CODEX_TAIL]


def test_a_non_ascii_model_stays_as_it_is_and_the_instructions_path_is_escaped():
    files = Path("/scratch/café")
    assert codex_argv("/opt/bin/codex", files=files, model="café",
                      overrides={"model": "café"}) == [
        *CODEX_HEAD, "-c", 'model="café"', "-c", 'model="café"', *CODEX_SWITCHES,
        "-c", 'web_search="disabled"', "--output-schema", "/scratch/café/schema.json",
        "-c", 'model_instructions_file="/scratch/caf\\u00e9/instructions.md"',
        "-c", "project_doc_max_bytes=0", "-o", "/scratch/café/last-message.json", "-"]
