"""Step 5: the LLM layer. No test here touches the network: replay never builds a client, and
record/live tests use a fake client or a closed local port."""
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from anthropic.types import Message
from pydantic import BaseModel, ConfigDict

from agents.llm import (LLM, BudgetExceeded, LLMConfig, LLMConfigError, LLMRefused, LLMUnavailable, ReplayCacheMiss,
                        cache_key, cost_usd, default_client, load_config)

ROOT = Path(__file__).resolve().parent.parent


class Echo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    word: str


REQ = dict(model="claude-haiku-5-5", system="Say a word.", max_tokens=50,
           messages=[{"role": "user", "content": "hello"}], thinking={"type": "disabled"}, effort="low")


def config(tmp_path, mode="replay", **kw) -> LLMConfig:
    return LLMConfig(mode=mode, model_id="claude-sonnet-5-5", extraction_model_id="claude-haiku-5-5",
                     budget_usd=kw.pop("budget", 10.0), cache_dir=tmp_path / "cache",
                     usage_path=tmp_path / "usage.csv", api_key="sk-test", **kw)


def message(text='{"word": "hi"}', stop="end_turn", usage=None) -> Message:
    return Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
        "usage": usage or {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 0}})


class FakeClient:
    def __init__(self, reply: Message):
        self.calls = []
        self.messages = self
        self._reply = reply

    def create(self, **request):
        self.calls.append(request)
        return self._reply


def no_network(_config):
    raise AssertionError("replay mode must never build an API client")


# --- 1. the cache key ------------------------------------------------------------------------

def test_key_ignores_dict_order():
    a = LLM.build_request(**REQ)
    b = json.loads(json.dumps(a))
    b = {k: b[k] for k in reversed(list(b))}  # same content, different insertion order
    assert cache_key(a) == cache_key(b)


def test_key_is_identical_across_processes():
    """Different hash seeds per process: the key must not depend on Python's hash randomisation."""
    code = ("from agents.llm import LLM, cache_key; "
            f"print(cache_key(LLM.build_request(**{REQ!r})))")
    keys = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True,
                           env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip() for seed in ("1", "2", "3")}
    assert len(keys) == 1 and keys == {cache_key(LLM.build_request(**REQ))}


@pytest.mark.parametrize("change", [
    {"model": "claude-sonnet-5-5"}, {"system": "Say a word!"}, {"max_tokens": 51}, {"effort": "medium"},
    {"messages": [{"role": "user", "content": "hello."}]},
])
def test_any_change_changes_the_key(change):
    assert cache_key(LLM.build_request(**{**REQ, **change})) != cache_key(LLM.build_request(**REQ))


def test_no_sampling_parameters_and_caching_markers():
    r = LLM.build_request(**REQ, tools=[{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}])
    assert not {"temperature", "top_p", "top_k"} & set(r)          # SPEC 17.33
    assert r["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" in r["tools"][-1] and "cache_control" not in r["tools"][0]


# --- replay ----------------------------------------------------------------------------------

def test_replay_miss_fails_loudly_without_network(tmp_path):
    llm = LLM(config(tmp_path), client_factory=no_network)
    with pytest.raises(ReplayCacheMiss) as exc:
        llm.call(purpose="extraction", output_format=Echo, **REQ)
    text = str(exc.value)
    assert "LLM_MODE=record" in text and str(tmp_path / "cache") in text and "'hello'" in text


def test_record_then_replay_returns_the_same_validated_output(tmp_path):
    fake = FakeClient(message())
    recorded = LLM(config(tmp_path, "record"), client_factory=lambda c: fake).call(purpose="t", output_format=Echo, **REQ)
    replayed = LLM(config(tmp_path, "replay"), client_factory=no_network).call(purpose="t", output_format=Echo, **REQ)
    assert (recorded.parsed, replayed.parsed, replayed.from_cache) == (Echo(word="hi"), Echo(word="hi"), True)
    assert recorded.key == replayed.key and len(fake.calls) == 1


def test_invalid_model_output_is_rejected_and_not_cached(tmp_path):
    fake = FakeClient(message('{"word": "hi", "scope": "customer"}'))  # an extra field
    with pytest.raises(Exception, match="scope"):
        LLM(config(tmp_path, "record"), client_factory=lambda c: fake).call(purpose="t", output_format=Echo, **REQ)
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").iterdir())


# --- 4. usage log, cost, budget ----------------------------------------------------------------

def test_paid_call_logs_usage_and_cost(tmp_path):
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 600, "cache_read_input_tokens": 0}
    LLM(config(tmp_path, "live"), client_factory=lambda c: FakeClient(message(usage=usage))).call(
        purpose="extraction", output_format=Echo, **REQ)
    (row,) = list(csv.DictReader((tmp_path / "usage.csv").open()))
    expected = (1000 * 0.10 + 600 * 0.125 + 200 * 0.50) / 1e6
    assert (row["mode"], row["model"], row["cache_write_tokens"]) == ("live", "claude-haiku-5-5", "600")
    assert float(row["cost_usd"]) == pytest.approx(expected) and float(row["cumulative_usd"]) == pytest.approx(expected)
    assert not (tmp_path / "cache").exists()  # live saves nothing


def test_prices():
    one_m = {"input_tokens": 1_000_000}
    assert cost_usd("claude-sonnet-5-5", {"cache_read_input_tokens": 1_000_000}) == pytest.approx(0.10)
    assert cost_usd("claude-sonnet-5-5", {"output_tokens": 1_000_000}) == pytest.approx(10.0)
    assert cost_usd("claude-haiku-5-5", {"input_tokens": 100_000}) == pytest.approx(0.01)
    assert cost_usd("claude-haiku-5-5", one_m) == pytest.approx(0.50)  # over 100K: long-context rate
    with pytest.raises(LLMConfigError):
        cost_usd("claude-unknown", one_m)


def test_budget_is_a_hard_stop(tmp_path):
    (tmp_path / "usage.csv").write_text("cost_usd\n6.0\n4.0\n")
    fake = FakeClient(message())
    with pytest.raises(BudgetExceeded, match="LLM_BUDGET_USD"):
        LLM(config(tmp_path, "record"), client_factory=lambda c: fake).call(purpose="t", output_format=Echo, **REQ)
    assert fake.calls == []


def test_refusal_is_raised_and_still_logged(tmp_path):
    with pytest.raises(LLMRefused):
        LLM(config(tmp_path, "record"), client_factory=lambda c: FakeClient(message(stop="refusal"))).call(
            purpose="agent", output_format=Echo, **REQ)
    assert len(list(csv.DictReader((tmp_path / "usage.csv").open()))) == 1  # billed, so logged
    assert not (tmp_path / "cache").exists()                                 # never replayed


# --- 5. the API key comes only from .env ---------------------------------------------------------

def test_api_key_only_from_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-from-dotenv\nLLM_MODE=record\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-shell")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-from-shell")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://evil.example")
    monkeypatch.delenv("LLM_MODE", raising=False)
    cfg = load_config(env)
    client = default_client(cfg)
    assert (cfg.api_key, client.api_key, client.auth_token) == ("sk-from-dotenv", "sk-from-dotenv", None)
    assert str(client.base_url).startswith("https://api.anthropic.com")
    assert "sk-from-dotenv" not in repr(cfg)  # the key is never printed


def test_empty_key_blocks_paid_modes_but_not_replay(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-shell")  # must not rescue it
    monkeypatch.delenv("LLM_MODE", raising=False)
    with pytest.raises(LLMConfigError, match=str(env)):
        load_config(env, mode="record")
    assert load_config(env, mode="replay").mode == "replay"


# --- 6. unreachable API: clear error, no hang -------------------------------------------------------

def test_unreachable_api_fails_fast_with_replay_instruction(tmp_path):
    cfg = config(tmp_path, "live", base_url="http://127.0.0.1:9")  # nothing listens on port 9
    started = time.monotonic()
    with pytest.raises(LLMUnavailable) as exc:
        LLM(cfg).call(purpose="agent", output_format=Echo, **REQ)
    assert time.monotonic() - started < 10
    assert "LLM_MODE=replay" in str(exc.value)


def test_langsmith_tracing_is_off_by_default_and_needs_its_key_from_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LLM_MODE=replay\n")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")       # the shell must not switch it on
    monkeypatch.delenv("LLM_MODE", raising=False)
    assert load_config(env).langsmith_tracing is False
    env.write_text("LANGSMITH_TRACING=true\nLANGSMITH_API_KEY=\n")
    with pytest.raises(LLMConfigError, match="LANGSMITH_API_KEY"):
        load_config(env)
