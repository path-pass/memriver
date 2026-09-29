"""memriver's executors as memriver dream's Executor: the name shown in the report, the
harness recorded as changed_via of every change dream makes, and each Result as dream's
ExecutorResult -- the answer or the kind of a failure, never any output. Also the hint
dream's Needs-you line carries for the first login and the first quota failure of a
run: it names memriver's [dream] keys, which memriver_dream never names."""

from __future__ import annotations

from memriver_dream import ExecutorResult

from ..executor import Executor

# [dream] refuses jev: only a harness executor reaches dream
_HARNESS = {"claude": "claude-code", "codex": "codex"}

FAILURE_HINTS = {
    "login": ("check the executor's login; for API-key, Bedrock or Vertex auth see "
              "[dream] claude_settings / codex_overrides"),
    "quota": "the executor's usage limit was hit",
}


class DreamExecutor:
    def __init__(self, executor: Executor) -> None:
        self._executor = executor
        self.name, self.harness = executor.name, _HARNESS[executor.name]

    def run(self, *, system_prompt: str, prompt: str, schema: dict,
            timeout_s: int) -> ExecutorResult:
        result = self._executor.run(system_prompt=system_prompt, prompt=prompt, schema=schema,
                                    timeout_s=timeout_s)
        return ExecutorResult(value=result.value, error=result.error)
