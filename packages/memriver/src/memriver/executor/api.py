"""JevExecutor: TypeSafe's jev through Pydantic AI -- one structured request, no tools.

The request is Pydantic AI's: an Agent with a TypeSafeModel and StructuredDict(schema)
as its output; the interface's system prompt becomes the Agent's instructions (none when
empty) and the prompt is the state. Each call builds and closes its own HTTP client,
SDK client and Agent inside one asyncio.run, bounded as a whole by timeout_s; a caller
already running an event loop in its thread gets "exit" (memriver calls it from sync
code and from fastmcp's worker threads).

What it never does: retry (neither the SDK nor the Agent), follow a redirect (the key
would travel to the new location), read TYPESAFE_API_KEY or TYPESAFE_BASE_URL (the key
comes from the variable the caller names, the address is fixed), let the SDK log (its
DEBUG lines carry the prompt and the response body), trace, or print Pydantic AI's
first-run banner. A failure comes back as its kind alone: exception text can carry the
response body, so it is never read, logged or chained.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping

import httpx2
import pydantic_ai
from pydantic_ai import Agent, StructuredDict
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.models.typesafe import TypeSafeModel
from pydantic_ai.providers.typesafe import TypeSafeProvider
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from ..settings import JEV_BASE_URL
from . import Executor, Result

# the SDK's DEBUG wire log carries the prompt and the response body: off for the whole
# process, never lowered and restored around one call (calls may overlap)
_sdk_logger = logging.getLogger("typesafe_sdk")
_sdk_logger.disabled = True
# a later logging configuration may re-enable existing loggers; it leaves level and
# propagate alone, so those two carry the silence on their own
_sdk_logger.setLevel(logging.CRITICAL + 1)
_sdk_logger.propagate = False
# memriver owns its stderr (an MCP server's, a scheduled run's)
pydantic_ai.BANNER_ENABLED = False


def _http_kind(status: int) -> str:
    if status in (401, 403):
        return "login"
    return "quota" if status == 429 else "exit"


def _fits(answer: object, schema: Mapping) -> bool:
    """Whether `answer` is an object with every required field and number fields the
    schema allows (finite, not a boolean, within minimum/maximum): StructuredDict
    passes the model's numbers through unchecked."""
    if not isinstance(answer, dict):
        return False
    for name, field in schema.get("properties", {}).items():
        if field.get("type") not in ("number", "integer") or name not in answer:
            continue
        value = answer[name]
        if isinstance(value, bool) or not isinstance(value, int | float) \
                or not math.isfinite(value):
            return False
        if not field.get("minimum", -math.inf) <= value <= field.get("maximum", math.inf):
            return False
    return all(name in answer for name in schema.get("required", ()))


class JevExecutor(Executor):
    name = "jev"

    def __init__(self, *, model: str, api_key_env: str, env: Mapping[str, str]) -> None:
        self._model, self._api_key_env, self._env = model, api_key_env, env

    def run(self, *, system_prompt: str, prompt: str, schema: dict,
            timeout_s: int) -> Result:
        key = self._env.get(self._api_key_env)
        if not key:
            # before anything is built: no request, and no fallback to another variable
            return Result(error="login")
        try:
            answer = asyncio.run(self._ask(key, system_prompt, prompt, schema, timeout_s))
        except TimeoutError:
            return Result(error="timeout")
        except ModelHTTPError as err:
            return Result(error=_http_kind(err.status_code))
        except ModelAPIError as err:
            # the connection failed, or timed out below the whole-call bound
            return Result(error="timeout" if isinstance(err.__cause__, TimeoutError)
                          else "exit")
        except UnexpectedModelBehavior:
            return Result(error="unparsable")
        except Exception:  # noqa: BLE001 - any other failure is "exit", its text unread
            return Result(error="exit")
        return Result(value=answer) if _fits(answer, schema) else Result(error="unparsable")

    async def _ask(self, key: str, system_prompt: str, prompt: str, schema: dict,
                   timeout_s: int) -> object:
        async with asyncio.timeout(timeout_s):
            async with httpx2.AsyncClient(follow_redirects=False) as http, \
                    AsyncTypeSafeClient(api_key=key, base_url=JEV_BASE_URL, http_client=http,
                                        retry=RetryPolicy(max_retries=0)) as sdk:
                model = TypeSafeModel(self._model,
                                      provider=TypeSafeProvider(typesafe_client=sdk))
                agent = Agent(model, output_type=StructuredDict(schema), retries=0,
                              instructions=system_prompt or None)
                agent.instrument = False
                result = await agent.run(prompt, model_settings={"timeout": timeout_s},
                                         infer_name=False)
                return result.output
