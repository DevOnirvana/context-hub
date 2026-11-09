"""LLM layer (SPEC 12, 17.33 to 17.37): one door to the Claude API, with record / replay / live.

    replay  (default) no network; every response comes from .llm_cache/. A miss fails loudly.
    record  calls the API, saves the response under its cache key, logs usage and cost.
    live    calls the API, logs usage and cost, saves nothing.
    auto    a hit plays back like replay; a miss calls the API like record and saves a NEW entry,
            never overwriting an existing one (free chat in the demo UI, Step 10).

Why a replay cache: the demo must run identically every time, offline, at zero cost. The model's
output is not deterministic (and sampling parameters are not accepted, SPEC 17.33), so the only
real guarantee is to replay a recorded response for an identical request.

The model only proposes (structured output) and words replies. Everything it returns is validated
here with pydantic; what is stored, and who sees it, is decided by hub/ code.
"""
import contextlib
import copy
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import anthropic
from anthropic.lib._parse._transform import transform_schema  # the SDK's own strict-schema transform
from anthropic.types import Message
from dotenv import dotenv_values
from pydantic import BaseModel, TypeAdapter

ROOT = Path(__file__).resolve().parent.parent
Mode = Literal["replay", "record", "live", "auto"]
API_BASE_URL = "https://api.anthropic.com"


# --- errors --------------------------------------------------------------------------------

class LLMError(Exception):
    """Base for every LLM-layer failure, so callers can catch them in one place."""


class LLMConfigError(LLMError):
    """Missing or invalid configuration (for example no API key in .env for record/live)."""


class ReplayCacheMiss(LLMError):
    """Replay mode found no recorded response for this exact request. Never falls back to the network."""


class LLMUnavailable(LLMError):
    """The API could not be reached or did not answer in time."""


class LLMRefused(LLMError):
    """The model declined (stop_reason 'refusal'). Agents escalate to a human (SPEC 17.35)."""

    def __init__(self, category: str | None):
        self.category = category
        super().__init__(f"the model declined this request (category: {category or 'unspecified'})")


class BudgetExceeded(LLMError):
    """Paid calls stop once logged spend reaches LLM_BUDGET_USD (SPEC 17.36)."""


# --- pricing (USD per million tokens, SPEC 17.36) ------------------------------------------

@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float  # 5-minute TTL
    cache_read: float


PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_write=2.50, cache_read=0.10),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
}
# Haiku 5.5 is priced higher once the whole prompt (including cache reads and writes) exceeds 100K.
HAIKU_LONG = Price(input=0.50, output=2.50, cache_write=0.625, cache_read=0.05)
HAIKU_LONG_THRESHOLD = 100_000


def cost_usd(model: str, usage: dict) -> float:
    if model not in PRICES:
        # An unknown model must not be logged as free: the budget would silently lie.
        raise LLMConfigError(f"no price for model {model}; add it to agents/llm.py PRICES")
    write = usage.get("cache_creation_input_tokens") or 0
    read = usage.get("cache_read_input_tokens") or 0
    uncached = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    price = PRICES[model]
    if model == "claude-haiku-5-5" and uncached + write + read > HAIKU_LONG_THRESHOLD:
        price = HAIKU_LONG
    return (uncached * price.input + write * price.cache_write + read * price.cache_read + out * price.output) / 1e6


# --- configuration ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMConfig:
    mode: Mode
    model_id: str
    extraction_model_id: str
    budget_usd: float
    cache_dir: Path
    usage_path: Path
    base_url: str = API_BASE_URL
    api_key: str = field(default="", repr=False)  # never printed
    langsmith_tracing: bool = False                # optional, never required for the demo (SPEC 12)
    langsmith_api_key: str = field(default="", repr=False)


def load_config(env_file: Path = ROOT / ".env", *, mode: Mode | None = None, cache_dir: Path | None = None,
                usage_path: Path | None = None, base_url: str | None = None) -> LLMConfig:
    """Settings come from .env. The API key comes ONLY from .env (SPEC 17.37): dotenv_values reads
    the file without touching os.environ, so a shell ANTHROPIC_API_KEY is never used.
    LLM_MODE may be overridden from the shell (it is not a secret), e.g. LLM_MODE=record pytest ..."""
    values = dotenv_values(env_file) if env_file.exists() else {}
    chosen = mode or os.environ.get("LLM_MODE") or values.get("LLM_MODE") or "replay"
    if chosen not in ("replay", "record", "live", "auto"):
        raise LLMConfigError(f"LLM_MODE must be replay, record, live or auto, not {chosen!r}")
    config = LLMConfig(
        mode=chosen,
        model_id=values.get("MODEL_ID") or "claude-sonnet-5-5",
        extraction_model_id=values.get("EXTRACTION_MODEL_ID") or "claude-haiku-5-5",
        # The shell may LOWER the budget for one run (a per-run cap, e.g. LLM_BUDGET_USD=0.5), never raise it.
        budget_usd=min(float(values.get("LLM_BUDGET_USD") or 10), float(os.environ.get("LLM_BUDGET_USD") or "inf")),
        cache_dir=cache_dir or ROOT / ".llm_cache",
        usage_path=usage_path or ROOT / "results" / "llm_usage.csv",
        base_url=base_url or API_BASE_URL,
        api_key=values.get("ANTHROPIC_API_KEY") or "",
        langsmith_tracing=(values.get("LANGSMITH_TRACING") or "false").lower() == "true",
        langsmith_api_key=values.get("LANGSMITH_API_KEY") or "",
    )
    # auto needs the key only on a cache miss, so it is checked there, not here.
    if config.mode in ("record", "live") and not config.api_key:
        raise LLMConfigError(f"LLM_MODE={config.mode} needs ANTHROPIC_API_KEY in {env_file} "
                             "(the shell environment is ignored on purpose). Or use LLM_MODE=replay.")
    if config.langsmith_tracing and not config.langsmith_api_key:
        raise LLMConfigError(f"LANGSMITH_TRACING=true needs LANGSMITH_API_KEY in {env_file}, or set it to false.")
    return config


def default_client(config: LLMConfig) -> anthropic.Anthropic:
    # Explicit api_key and base_url: the SDK then consults no environment variable or login
    # profile for credentials, and ANTHROPIC_BASE_URL cannot redirect calls.
    # Short connect timeout and one retry: an unreachable API fails in seconds, never hangs.
    client = anthropic.Anthropic(api_key=config.api_key, base_url=config.base_url,
                                 timeout=anthropic.Timeout(60.0, connect=5.0), max_retries=1)
    if config.langsmith_tracing:
        # Imported only when switched on, so the demo never depends on LangSmith being installed or up.
        from langsmith.wrappers import wrap_anthropic
        client = wrap_anthropic(client)
    return client


def _tracing(config: LLMConfig):
    """LangSmith trace scope for one paid call. The LangSmith key is passed explicitly from .env,
    like the Anthropic key, so no shell variable decides where traces go. Replay hits are not
    traced: they are not model calls."""
    if not config.langsmith_tracing:
        return contextlib.nullcontext()
    import langsmith
    return langsmith.tracing_context(enabled=True, client=langsmith.Client(api_key=config.langsmith_api_key))


# --- the cache key ---------------------------------------------------------------------------

def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"not JSON-serialisable in a request: {type(value).__name__}")


def canonical(request: dict) -> str:
    """One byte-exact string per request: sorted keys, fixed separators, no whitespace choices."""
    return json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def cache_key(request: dict) -> str:
    """sha256 of the full request body: model, system, messages, tools, output schema, thinking,
    effort, max_tokens. Anything that can change the output changes the key."""
    return hashlib.sha256(canonical(request).encode("utf-8")).hexdigest()


def content_to_params(message: Message) -> list[dict]:
    """The assistant's content blocks, as sent back on the next request. One function for record
    and replay, so both produce byte-identical requests (and the same cache key). Blocks go back
    unchanged, thinking blocks included: Sonnet 5.5 checks that the history is append-only."""
    return [block.model_dump(mode="json", exclude_none=True) for block in message.content]


# --- the client wrapper ----------------------------------------------------------------------

@dataclass
class LLMResult:
    message: Message
    parsed: BaseModel | None
    key: str
    from_cache: bool


USAGE_COLUMNS = ["wall_time_utc", "mode", "purpose", "model", "cache_key", "input_tokens", "cache_write_tokens",
                 "cache_read_tokens", "output_tokens", "cost_usd", "cumulative_usd"]


class LLM:
    def __init__(self, config: LLMConfig, *, client_factory: Callable[[LLMConfig], Any] = default_client):
        self.config = config
        self._client_factory = client_factory
        self._client = None  # built on first paid call only; replay never builds one
        self.spent_this_session = 0.0  # paid calls made by this instance (the UI shows it)

    # -- request construction --

    @staticmethod
    def build_request(*, model: str, system: str, messages: list[dict], max_tokens: int,
                      tools: list[dict] | None = None, output_format: type[BaseModel] | None = None,
                      thinking: dict | None = None, effort: str | None = None,
                      system_extra: str | None = None, cache_messages: bool = False) -> dict:
        """The exact body sent to the API, and the input to the cache key. No sampling parameters
        (SPEC 17.33). Prompt caching: the system prompt and the last tool carry cache_control, so
        the stable prefix (tools, then system) is cached; per-call content goes in messages.
        system_extra is a second, uncached system block for per-customer instructions (the
        reply-style preference), placed after the cached prefix so it never invalidates it.
        cache_messages adds top-level automatic caching of the conversation so far, for agent
        loops that resend a growing history on every step."""
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": copy.deepcopy(messages),
        }
        if system_extra:
            request["system"].append({"type": "text", "text": system_extra})
        if cache_messages:
            request["cache_control"] = {"type": "ephemeral"}
        if tools:
            tools = copy.deepcopy(tools)
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            request["tools"] = tools
        if thinking is not None:
            request["thinking"] = thinking
        output_config: dict[str, Any] = {}
        if effort is not None:
            output_config["effort"] = effort
        if output_format is not None:
            schema = transform_schema(TypeAdapter(output_format).json_schema())
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            request["output_config"] = output_config
        return request

    def call(self, *, purpose: str, output_format: type[BaseModel] | None = None, **request_args) -> LLMResult:
        request = self.build_request(output_format=output_format, **request_args)
        key = cache_key(request)
        path = self.config.cache_dir / f"{key}.json"

        if self.config.mode in ("replay", "auto") and path.exists():
            message = Message.model_validate(json.loads(path.read_text())["response"])
            return LLMResult(message=message, parsed=self._parse(message, output_format), key=key, from_cache=True)
        if self.config.mode == "replay":
            last_user = next((m["content"] for m in reversed(request["messages"]) if m["role"] == "user"), "")
            raise ReplayCacheMiss(
                f"no recorded response for {purpose} call to {request['model']} (key {key[:16]}...).\n"
                f"  expected file: {path}\n  last user message: {str(last_user)[:160]!r}\n"
                "  The request differs from every recorded one (prompt, model, schema or input changed). "
                "Re-run with LLM_MODE=record to record it (costs money), or restore the original input.")
        if self.config.mode == "auto" and not self.config.api_key:
            raise LLMConfigError(f"{purpose}: cache miss in auto mode needs ANTHROPIC_API_KEY in .env "
                                 "(the shell environment is ignored on purpose). Or use the scripted turns.")

        self._check_budget()
        message = self._send(request)
        self._log_usage(purpose, request["model"], key, message)
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMRefused(getattr(details, "category", None))  # billed and logged, never cached
        if message.stop_reason == "max_tokens":
            raise LLMError(f"{purpose}: response hit max_tokens={request['max_tokens']}; raise it")
        parsed = self._parse(message, output_format)  # validate BEFORE caching: never record junk
        entry = json.dumps({"request": request, "response": message.model_dump(mode="json")},
                           indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if self.config.mode == "record":
            self.config.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(entry)
        elif self.config.mode == "auto":
            self._save_new(path, entry)
        return LLMResult(message=message, parsed=parsed, key=key, from_cache=False)

    # -- internals --

    def _save_new(self, path: Path, entry: str) -> None:
        """Auto mode writes only NEW entries: exclusive create, so a recorded demo answer can never
        be replaced by a later call (two tabs racing on the same miss keep the first answer)."""
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x") as f:
                f.write(entry)
        except FileExistsError:
            pass

    def _send(self, request: dict) -> Message:
        if self._client is None:
            self._client = self._client_factory(self.config)
        hint = "For the demo, set LLM_MODE=replay in .env (or in the UI settings) and rerun."
        try:
            with _tracing(self.config):
                return self._client.messages.create(**request)
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            # APITimeoutError is a subclass of APIConnectionError; both mean "no usable answer".
            raise LLMUnavailable(f"Could not reach the Claude API at {self.config.base_url} "
                                 f"({type(exc).__name__}: {exc}). {hint}") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMConfigError("The ANTHROPIC_API_KEY in .env was rejected by the API. " + hint) from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable(f"Rate limited by the Claude API. Wait and retry, or: {hint}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailable(f"The Claude API returned {exc.status_code} (overloaded or down). {hint}") from exc
            raise  # a 4xx other than auth/rate limit is a bug in our request: show it as is

    @staticmethod
    def _parse(message: Message, output_format: type[BaseModel] | None) -> BaseModel | None:
        if output_format is None:
            return None
        text = next((b.text for b in message.content if b.type == "text"), None)
        if text is None:
            raise LLMError("structured output expected, but the response has no text block")
        return output_format.model_validate_json(text)  # pydantic decides, on every path

    def spent_usd(self) -> float:
        if not self.config.usage_path.exists():
            return 0.0
        with self.config.usage_path.open() as f:
            return sum(float(row["cost_usd"]) for row in csv.DictReader(f))

    def _check_budget(self) -> None:
        spent = self.spent_usd()
        if spent >= self.config.budget_usd:
            raise BudgetExceeded(f"logged spend ${spent:.4f} has reached LLM_BUDGET_USD=${self.config.budget_usd:.2f}; "
                                 "no further paid calls. Use LLM_MODE=replay, or raise the budget in .env deliberately.")

    def _log_usage(self, purpose: str, model: str, key: str, message: Message) -> None:
        usage = message.usage.model_dump()
        cost = cost_usd(model, usage)
        self.spent_this_session += cost
        cumulative = self.spent_usd() + cost
        row = {
            # Wall-clock time is right here: this is spend accounting, not domain time.
            "wall_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": self.config.mode, "purpose": purpose, "model": model, "cache_key": key[:16],
            "input_tokens": usage.get("input_tokens") or 0,
            "cache_write_tokens": usage.get("cache_creation_input_tokens") or 0,
            "cache_read_tokens": usage.get("cache_read_input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
            "cost_usd": f"{cost:.6f}", "cumulative_usd": f"{cumulative:.6f}",
        }
        self.config.usage_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.config.usage_path.exists()
        with self.config.usage_path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=USAGE_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        print(f"LLM {self.config.mode} {purpose} {model}: in {row['input_tokens']}, cache write "
              f"{row['cache_write_tokens']}, cache read {row['cache_read_tokens']}, out {row['output_tokens']}, "
              f"${cost:.4f} (total ${cumulative:.4f} of ${self.config.budget_usd:.2f})", file=sys.stderr)
