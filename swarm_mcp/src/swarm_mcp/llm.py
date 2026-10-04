"""A small LLM client seam for tools that call a model (rubric sweeps, later others).

Tools depend on the ``LLMClient`` protocol, never on a vendor SDK, so tests run
with ``FakeClient`` and no network:

    client = get_client()                      # AnthropicClient, or LLMUnavailable with a clear message
    res = client.complete(system, prompt, max_tokens=1024)
    res.text, res.input_tokens, res.output_tokens, res.model

Settings come from ``swarm.toml`` ``[llm]`` (see ``config.py``):

| setting / env var            | default             | meaning                                                   |
|------------------------------|---------------------|-----------------------------------------------------------|
| ANTHROPIC_API_KEY (env)      | (unset)             | required: no key, no model calls                          |
| [llm] model / SWARM_LLM_MODEL| claude-sonnet-5-5   | model id (the env var wins)                               |
| [llm] effort                 | low                 | ``output_config.effort``; ``none`` omits it (e.g. Haiku)  |
| [llm] fallbacks              | true                | server-side refusal fallback (``fallbacks: "default"``)   |

The key check is deliberately explicit: the SDK can also pick up other
credential sources, but a sweep spends money, so it only runs when the operator
has opted in by setting ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from swarm_mcp.config import Config

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "low"  # a per-record yes/no judgement; raise it for subtle rubrics
FALLBACK_BETA = "server-side-fallback-2026-07-01"  # gates the scalar form fallbacks="default"
_OFF = {"", "0", "off", "none", "false", "no"}


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str | None = None


class LLMError(RuntimeError):
    """A model call failed (network, auth, rate limit, refusal...). The message is safe to show."""


class LLMUnavailable(LLMError):
    """No client can be built: missing API key or missing SDK. The message says how to fix it."""


@runtime_checkable
class LLMClient(Protocol):
    model: str

    def complete(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        """One single-turn completion. Raise ``LLMError`` on failure."""
        ...


# --------------------------------------------------------------------------- fake (tests)


class FakeClient:
    """Scripted client for tests.

    ``responses`` is either a list of strings returned in order (the last one
    repeats when the list runs out), or a callable ``(system, prompt) -> str``.
    A response that is an ``Exception`` instance is raised instead. Every call
    is recorded in ``calls`` as ``(system, prompt, max_tokens)``. Token counts
    are chars/4, like the sweep estimate.
    """

    def __init__(
        self,
        responses: Sequence[str | Exception] | Callable[[str, str], str | Exception],
        model: str = "fake-model",
    ):
        self.model = model
        self._responses = responses
        self.calls: list[tuple[str, str, int]] = []
        self._lock = threading.Lock()

    def complete(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        with self._lock:
            i = len(self.calls)
            self.calls.append((system, prompt, max_tokens))
        if callable(self._responses):
            out = self._responses(system, prompt)
        else:
            if not self._responses:
                raise LLMError("FakeClient has no scripted responses")
            out = self._responses[min(i, len(self._responses) - 1)]
        if isinstance(out, Exception):
            raise out
        return LLMResult(
            text=out,
            input_tokens=(len(system) + len(prompt)) // 4,
            output_tokens=max(1, len(out) // 4),
            model=self.model,
            stop_reason="end_turn",
        )


# --------------------------------------------------------------------------- Anthropic


class AnthropicClient:
    """``LLMClient`` over the official ``anthropic`` SDK (Messages API).

    Sends one user turn with a system prompt. With fallbacks on (the default) the
    request goes through the beta endpoint with ``fallbacks="default"``, so a
    policy decline is retried server-side on Anthropic's recommended substitute
    model. A response that still ends in ``stop_reason == "refusal"`` raises
    ``LLMError``. ``res.model`` is the model that actually served the response.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        effort: str | None = DEFAULT_EFFORT,
        fallbacks: bool = True,
        timeout: float = 120.0,
        max_retries: int = 2,
    ):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - anthropic is a declared dependency
            raise LLMUnavailable(
                "The 'anthropic' package is not installed. Run `uv sync --directory swarm_mcp` "
                "(it is a declared dependency of swarm-mcp)."
            ) from e
        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)

    def complete(self, system: str, prompt: str, max_tokens: int) -> LLMResult:
        a = self._anthropic
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        try:
            if self.fallbacks:
                resp = self._client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                resp = self._client.messages.create(**kwargs)
        except a.AuthenticationError as e:
            raise LLMError("Anthropic rejected the API key (401). Check ANTHROPIC_API_KEY.") from e
        except a.PermissionDeniedError as e:
            raise LLMError(f"Anthropic denied the request (403): {e.message}") from e
        except a.NotFoundError as e:
            raise LLMError(f"Model or endpoint not found (404). Check [llm] model / SWARM_LLM_MODEL={self.model!r}.") from e
        except a.RateLimitError as e:
            raise LLMError("Rate limited by Anthropic (429) after retries; lower the cap or retry later.") from e
        except a.BadRequestError as e:
            raise LLMError(f"Anthropic rejected the request (400): {e.message}") from e
        except a.APIStatusError as e:
            raise LLMError(f"Anthropic API error ({e.status_code}): {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"Could not reach the Anthropic API: {e}") from e
        except a.AnthropicError as e:
            raise LLMError(f"Anthropic SDK error ({type(e).__name__}): {e}") from e

        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise LLMError(f"The model declined this record (stop_reason=refusal, category={cat}).")
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        usage = resp.usage
        return LLMResult(
            text=text,
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            model=str(resp.model or self.model),
            stop_reason=resp.stop_reason,
        )


def _config(config: "Config | None") -> "Config":
    from swarm_mcp.config import Config

    return config if config is not None else Config.load()


def get_client(config: "Config | None" = None) -> LLMClient:
    """Build the configured client, or raise ``LLMUnavailable`` saying exactly what is missing."""
    config = _config(config)
    if not config.api_key:
        raise LLMUnavailable(
            "No LLM configured: ANTHROPIC_API_KEY is not set, so no model calls were made. For the swarm-mcp "
            "command line, export it in your shell; for the MCP server, set it in .mcp.json's env block (or the "
            "shell that starts it) and restart the server. Dry runs and cost estimates work without a key."
        )
    try:
        import anthropic  # noqa: F401
    except ImportError as e:
        raise LLMUnavailable(
            "The 'anthropic' package is not installed in this environment. Run `uv sync --directory swarm_mcp`."
        ) from e
    effort = (config.llm_effort or DEFAULT_EFFORT).strip().lower()
    return AnthropicClient(
        model=config.llm_model or DEFAULT_MODEL,
        api_key=config.api_key,
        effort=None if effort in _OFF else effort,
        fallbacks=config.llm_fallbacks,
    )


def configured_model(config: "Config | None" = None) -> str:
    return _config(config).llm_model or DEFAULT_MODEL
