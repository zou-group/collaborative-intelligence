"""Reasoning-effort variants (<model>@<effort>, llm.py): what each route sends for a variant, where its runs go, what is
recorded, and that unsuffixed names (the leaderboard's) send and record exactly what they did before. No API key: the
provider clients are replaced by recorders (and a local mock server for the hosted OpenAI-compatible route)."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from collab_intel.harness import llm

LEADERBOARD = ["gpt-6-astra", "claude-opus-5-5", "claude-fable-5-1", "together:deepseek-ai/DeepSeek-V4-Pro-0813",
               "together:moonshotai/Kimi-K3", "gpt-6.1-sol", "claude-sonnet-5-5", "claude-haiku-5-5",
               "together:deepseek-ai/DeepSeek-V4.1-Flash", "together:zai-org/GLM-5.3", "together:openai/gpt-oss-120b",
               "claude-haiku-4-5", "gpt-5-mini", "Qwen/Qwen3-14B"]


@pytest.fixture
def rec(monkeypatch):
    """Real calls (no dry run) into recording clients; usage-log lines collected instead of written."""
    calls: dict[str, list] = {"anthropic": [], "chat": [], "responses": [], "timeouts": []}
    logs: list[dict] = []

    class Messages:
        def create(self, **kw):
            calls["anthropic"].append(kw)
            return NS(content=[NS(type="text", text="ok")], stop_reason="end_turn", _request_id="r",
                      usage=NS(model_dump=lambda: {"input_tokens": 10, "output_tokens": 5}))

    class Chat:
        def create(self, **kw):
            calls["chat"].append(kw)
            # as gpt-6-astra does (2026-10-02): function tools with any reasoning but "none" only on /v1/responses
            if kw.get("tools") and kw.get("reasoning_effort") != "none" and kw["model"].startswith("gpt-6"):
                raise _BadRequest("Function tools with reasoning_effort are not supported for this model in "
                                  "/v1/chat/completions. To use function tools, use /v1/responses.")
            return NS(choices=[NS(message=NS(content="ok", tool_calls=None), finish_reason="stop")], id="c",
                      usage=NS(prompt_tokens=10, completion_tokens=5))

    class Responses:
        def create(self, **kw):
            calls["responses"].append(kw)
            return NS(output=[], output_text="ok", status="completed", id="r", usage=NS(input_tokens=10, output_tokens=5))

    class Client:
        messages, chat, responses = Messages(), NS(completions=Chat()), Responses()

        def with_options(self, timeout=None):
            calls["timeouts"].append(timeout)
            return self

    monkeypatch.delenv("HARNESS_DRY_RUN", raising=False)
    monkeypatch.setattr(llm, "_client", lambda provider, model=None: Client())
    monkeypatch.setattr(llm, "_log", logs.append)
    monkeypatch.setattr(llm, "total_spent", lambda: 0.0)
    monkeypatch.setattr(llm, "_RESPONSES_ONLY", set())
    monkeypatch.setattr(llm, "_together_price", lambda m: (1.0, 2.0))
    calls["log"] = logs
    return calls


class _BadRequest(Exception):
    status_code = 400


def _ask(model, tools=None):
    return llm.call_model(model, "s", [{"role": "user", "content": "x"}], tools=tools, max_tokens=450, tag="t")


def test_unsuffixed_names_send_and_record_what_they_did(rec):
    """The leaderboard's models: no `effort` in the log, no `effort_variants` in the record, the same request fields."""
    assert "effort_variants" not in llm.run_config(LEADERBOARD) and llm.run_config(LEADERBOARD) == llm.run_config()
    _ask("claude-sonnet-5-5")
    _ask("claude-opus-5-5")
    a, b = rec["anthropic"]
    assert a["model"] == "claude-sonnet-5-5" and a["thinking"] == {"type": "between_tools"} and "output_config" not in a
    assert a["max_tokens"] == 450 + llm.THINKING_HEADROOM
    assert b["output_config"] == {"effort": "low"} and "thinking" not in b and b["max_tokens"] == 450 + llm.THINKING_HEADROOM
    _ask("gpt-6-astra", tools=[{"name": "f", "description": "", "input_schema": {"type": "object"}}])
    assert rec["responses"][-1]["reasoning"] == {"effort": "low"} and rec["responses"][-1]["model"] == "gpt-6-astra"
    _ask("together:zai-org/GLM-5.3")
    assert rec["chat"][-1]["extra_body"] == {"reasoning_effort": "low"} and rec["chat"][-1]["max_tokens"] == 450 + llm.THINKING_HEADROOM
    assert all("effort" not in x for x in rec["log"]) and rec["timeouts"] == []


def test_anthropic_variant_sets_the_effort_and_lets_the_model_think(rec):
    _ask("claude-opus-5-5@high")
    _ask("claude-sonnet-5-5@high")
    _ask("claude-haiku-5-5@medium")
    opus, sonnet, haiku = rec["anthropic"]
    assert opus["model"] == "claude-opus-5-5" and opus["output_config"] == {"effort": "high"} and "thinking" not in opus
    # Sonnet 5.5's between_tools (thinking off) is dropped: adaptive thinking, its default, at that effort
    assert sonnet["model"] == "claude-sonnet-5-5" and sonnet["output_config"] == {"effort": "high"} and "thinking" not in sonnet
    assert haiku["thinking"] == {"type": "adaptive"} and haiku["output_config"] == {"effort": "medium"}
    assert opus["max_tokens"] == 450 + llm.EFFORT_HEADROOM["high"] and haiku["max_tokens"] == 450 + llm.EFFORT_HEADROOM["medium"]
    assert "temperature" not in json.dumps(rec["anthropic"]) and not llm.accepts_temperature("claude-opus-5-5@high")
    assert [x["effort"] for x in rec["log"]] == ["high", "high", "medium"] and [x["model"] for x in rec["log"]][0] == "claude-opus-5-5@high"
    assert rec["timeouts"] == [llm.EFFORT_TIMEOUT_S] * 3
    # priced as the base model, at its list price (2026-10-20: these four were missing and counted at the (5, 25) fallback)
    assert llm.cost_usd("claude-opus-5-5@high", {"output_tokens": 10**6}) == llm.cost_usd("claude-opus-5-5", {"output_tokens": 10**6})
    for m, (pin, pout) in {"gpt-6-astra": (10, 50), "gpt-6.1-sol": (2, 10), "claude-sonnet-5-5": (2, 10),
                           "claude-haiku-5-5": (0.1, 0.5), "gpt-6-luna": (0.1, 0.5)}.items():
        for name in (m, m + "@high"):
            assert llm.cost_usd(name, {"input_tokens": 10**6, "output_tokens": 10**6}) == pytest.approx(pin + pout), name


def test_openai_variant_on_chat_and_on_the_responses_fallback(rec):
    tools = [{"name": "f", "description": "", "input_schema": {"type": "object"}}]
    _ask("gpt-6-astra@high", tools=tools)
    _ask("gpt-6.1-sol@xhigh")
    assert rec["chat"][0]["model"] == "gpt-6-astra" and rec["chat"][0]["reasoning_effort"] == "high"
    assert rec["responses"][0]["model"] == "gpt-6-astra" and rec["responses"][0]["reasoning"] == {"effort": "high"}
    assert rec["responses"][0]["max_output_tokens"] == 450 + llm.EFFORT_HEADROOM["high"]
    assert rec["chat"][1]["model"] == "gpt-6.1-sol" and rec["chat"][1]["reasoning_effort"] == "xhigh"
    assert {x["model"]: x["effort"] for x in rec["log"]} == {"gpt-6-astra@high": "high", "gpt-6.1-sol@xhigh": "xhigh"}
    # the base model's learned Responses-only state is its own: the variant learned it on its first call
    assert "gpt-6-astra@high" in llm._RESPONSES_ONLY and "gpt-6-astra" not in llm._RESPONSES_ONLY


def test_together_and_vllm_variants(rec):
    _ask("together:deepseek-ai/DeepSeek-V4-Pro-0813@high")
    _ask("together:openai/gpt-oss-120b@none")
    _ask("Qwen/Qwen3-14B@high")
    hi, none, q = rec["chat"]
    assert hi["model"] == "deepseek-ai/DeepSeek-V4-Pro-0813" and hi["extra_body"] == {"reasoning": {"enabled": True}, "reasoning_effort": "high"}
    assert none["extra_body"] == {"reasoning": {"enabled": False}, "chat_template_kwargs": {"enable_thinking": False}}
    assert q["model"] == "Qwen/Qwen3-14B" and q["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
    assert q["max_tokens"] == 450 + llm.VLLM_THINKING_HEADROOM


def test_hosted_compat_variant_replaces_the_reasoning_field(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_COMPAT", {})
    monkeypatch.setattr(llm, "_compat_resolved", {})
    monkeypatch.setattr(llm, "_openrouter_models", {"google/gemini-4-argon": {
        "pricing": {"prompt": "0.000002", "completion": "0.00001"}, "supported_parameters": ["reasoning", "tools"],
        "reasoning": {"mandatory": True, "supported_efforts": ["high", "medium", "low"]}}})
    monkeypatch.delenv("HARNESS_DRY_RUN", raising=False)
    base = llm.openai_compat_config("openrouter:google/gemini-4-argon")
    hi = llm.openai_compat_config("openrouter:google/gemini-4-argon@high")
    assert base["extra"]["reasoning"] == {"effort": "low"} and hi["extra"]["reasoning"] == {"effort": "high"}
    assert hi["model"] == "google/gemini-4-argon" and hi["price"] == base["price"] and hi["effort"] == "high"
    assert llm.api_model_id("openrouter:google/gemini-4-argon@high") == "google/gemini-4-argon"
    g = llm.openai_compat_config("gemini-4-argon@high")
    assert g["extra"]["reasoning_effort"] == "high" and g["model"] == "gemini-4-argon" and g["base_url"] == llm.GEMINI_BASE
    rc = llm.run_config(["openrouter:google/gemini-4-argon@high"])
    assert rc["effort_variants"]["openrouter:google/gemini-4-argon@high"]["request"]["extra_body"]["reasoning"] == {"effort": "high"}
    assert rc["openai_compat"]["openrouter:google/gemini-4-argon@high"]["effort"] == "high"


def test_names_and_run_folders():
    assert llm.split_effort("gpt-6-astra@high") == ("gpt-6-astra", "high") and llm.split_effort("gpt-6-astra") == ("gpt-6-astra", None)
    assert llm.model_slug("openrouter:google/gemini-4-argon@high") == "openrouter:google__gemini-4-argon@high"
    assert llm.model_slug("gpt-6-astra@high") != llm.model_slug("gpt-6-astra")
    assert "must be one of" in llm.missing_setup("gpt-6-astra@ultra")
    assert "no 'none'" in (llm.missing_setup("claude-opus-5-5@none") or "")
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("score", Path(__file__).resolve().parents[1] / "scripts/score.py")
    score = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(score)
    assert score.model_slug("gpt-6-astra@high") == llm.model_slug("gpt-6-astra@high")
    sc = {"exclude_models": ["together:moonshotai/Kimi-K3"]}
    assert score.excluded_by_set("together:moonshotai/Kimi-K3@high", sc) and not score.excluded_by_set("gpt-6-astra@high", sc)


def test_a_dry_run_of_a_variant_writes_its_own_folder_and_records_the_effort(tmp_path, monkeypatch):
    from pathlib import Path

    from collab_intel.harness.episode import run_episode
    monkeypatch.setenv("HARNESS_DRY_RUN", "1")
    ROOT = Path(__file__).resolve().parents[1]
    scen = ROOT / "benchmark/scenarios/emotional_intelligence/h_l_hurricane_watch_selfdoubt"
    t = run_episode(scen, "solo", "claude-opus-5-5@high", ["claude-haiku-4-5", "gpt-5-mini", "Qwen/Qwen3-14B"], seed=0,
                    out_dir=tmp_path)
    assert (tmp_path / "claude-opus-5-5@high" / "solo_seed0.json").is_file()
    assert t["harness_config"]["effort_variants"]["claude-opus-5-5@high"]["request"] == {"output_config": {"effort": "high"}}
