"""The hosted OpenAI-compatible route (HARNESS_OPENAI_COMPAT, gemini-*, openrouter:<id>) and the reasoning setting of a new
OpenAI model (HARNESS_OPENAI_EFFORT), with no API key: a local mock server stands in for the provider. The other routes'
requests must not change."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace as NS

import pytest

from collab_intel.harness import llm
from collab_intel.harness.sandbox import TOOLS, Sandbox


class _Mock:
    """A tiny OpenAI-compatible server: records every request (headers and body) and answers from a list of replies
    (dicts merged into a chat.completion with unknown fields at every level)."""

    def __init__(self, replies):
        self.requests, self.replies = [], list(replies)
        mock = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                mock.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                msg = {"role": "assistant", "content": None, "x_unknown_message_field": [1, 2]}
                msg.update(mock.replies.pop(0))
                out = {"id": "cmpl-mock", "object": "chat.completion", "created": 0, "model": body["model"],
                       "x_unknown_top_level": {"a": 1}, "system_fingerprint": None,
                       "choices": [{"index": 0, "message": msg, "x_unknown_choice": True,
                                    "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 150,
                                 "x_unknown_usage": 3}}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def live(monkeypatch, tmp_path):
    """Real calls (no dry run) to the mock, with an isolated route table, client cache and usage log."""
    logs: list[dict] = []
    monkeypatch.delenv("HARNESS_DRY_RUN", raising=False)
    monkeypatch.setattr(llm, "OPENAI_COMPAT", {})
    monkeypatch.setattr(llm, "_compat_resolved", {})
    monkeypatch.setattr(llm, "_clients", {})
    monkeypatch.setattr(llm, "_log", logs.append)
    monkeypatch.setattr(llm, "total_spent", lambda: 0.0)
    return logs


def _tool_call(cid, name, args, **extra):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}, **extra}


def test_mock_server_route_sends_the_models_own_key_no_vllm_fields_and_runs_its_tool_calls(live, monkeypatch):
    sig = {"google": {"thought_signature": "c2lnLUE="}}
    details = [{"type": "reasoning.encrypted", "data": "opaque"}]
    mock = _Mock([{"tool_calls": [_tool_call("c1", "read_file", {"path": "solution.py"}, extra_content=sig, x_unknown_tc=1),
                                  _tool_call(None, "list_files", {})], "reasoning_details": details},
                  {"content": "Fixed and posted."}])
    monkeypatch.setenv("MOCK_PROVIDER_KEY", "mock-key-1")
    monkeypatch.setenv("HARNESS_VLLM_API_KEY", "the-vllm-key")  # must not be used on this route
    llm.OPENAI_COMPAT["my-model"] = {"base_url": mock.url, "api_key_env": "MOCK_PROVIDER_KEY", "model": "vendor/my-model-v1",
                                     "extra": {"reasoning_effort": "low"}, "price": [1.0, 2.0]}
    sb = Sandbox({"solution.py": "def f():\n    return 1\n"}, [])
    try:
        convo = [{"role": "user", "content": "your turn"}]
        out = llm.call_model("my-model", "sys", convo, tools=TOOLS, max_tokens=300, tag="t")
        assert [tc["name"] for tc in out["tool_calls"]] == ["read_file", "list_files"]
        assert out["tool_calls"][0]["input"] == {"path": "solution.py"} and out["tool_calls"][1]["id"].startswith("call_")
        # executed as the episode does, and the results go back under the call ids
        results = [{"id": tc["id"], "content": sb.call(tc["name"], tc["input"])} for tc in out["tool_calls"]]
        assert "return 1" in results[0]["content"] and "solution.py" in results[1]["content"]
        convo += [{"role": "assistant", "content": out["text"], "tool_calls": out["tool_calls"]},
                  {"role": "tool_results", "results": results}]
        out2 = llm.call_model("my-model", "sys", convo, tools=TOOLS, max_tokens=300, tag="t")
        assert out2["text"] == "Fixed and posted."
    finally:
        sb.cleanup()
        mock.close()
    first, second = (r["body"] for r in mock.requests)
    assert all(r["auth"] == "Bearer mock-key-1" for r in mock.requests) and all(r["path"] == "/v1/chat/completions" for r in mock.requests)
    assert "chat_template_kwargs" not in json.dumps([r["body"] for r in mock.requests])
    assert first["model"] == "vendor/my-model-v1" and first["reasoning_effort"] == "low"
    assert first["max_tokens"] == 300 + llm.THINKING_HEADROOM and "max_completion_tokens" not in first
    assert [t["function"]["name"] for t in first["tools"]] == [t["name"] for t in TOOLS]
    # the provider's attachments go back with the model's own tool calls: the thought signature and reasoning_details
    asst = next(m for m in second["messages"] if m["role"] == "assistant")
    assert asst["tool_calls"][0]["extra_content"] == sig and "extra_content" not in asst["tool_calls"][1]
    assert asst["reasoning_details"] == details and "provider_echo" not in json.dumps(second)
    assert [m["tool_call_id"] for m in second["messages"] if m["role"] == "tool"] == ["c1", out["tool_calls"][1]["id"]]
    # usage: total_tokens counts 40 thinking tokens left out of completion_tokens; priced at the entry's price
    assert live[0]["usage"] == {"input_tokens": 100, "output_tokens": 50} and live[0]["provider"] == "openai_compat"
    assert live[0]["extra_body"] == {"reasoning_effort": "low"}
    assert live[0]["cost_usd"] == pytest.approx((100 * 1.0 + 50 * 2.0) / 1e6)


def test_gemini_and_openrouter_presets_at_the_lightest_reasoning(live, monkeypatch):
    mock = _Mock([{"content": "hi"}, {"content": "hi"}, {"content": "hi"}])
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(llm, "_openrouter_models", {
        "qwen/qwen3-14b": {"pricing": {"prompt": "0.00000012", "completion": "0.00000024"}, "reasoning": {"mandatory": False},
                           "supported_parameters": ["reasoning", "tools"]},
        "x/thinker": {"pricing": {"prompt": "0.000001", "completion": "0.000002"}, "supported_parameters": ["reasoning"],
                      "reasoning": {"mandatory": True, "supported_efforts": ["high", "medium", "low"]}}})
    for m in ("gemini-3.8-flash", "openrouter:qwen/qwen3-14b", "openrouter:x/thinker"):  # the base URL alone points at the mock
        llm.OPENAI_COMPAT[m] = {"base_url": mock.url}
    try:
        for m in ("gemini-3.8-flash", "openrouter:qwen/qwen3-14b", "openrouter:x/thinker"):
            assert llm.call_model(m, "sys", [{"role": "user", "content": "x"}], max_tokens=50)["text"] == "hi"
    finally:
        mock.close()
    g, q, t = mock.requests
    assert g["auth"] == "Bearer g-key" and g["body"]["model"] == "gemini-3.8-flash" and g["body"]["reasoning_effort"] == "minimal"
    assert q["auth"] == "Bearer or-key" and q["body"]["model"] == "qwen/qwen3-14b" and q["body"]["reasoning"] == {"effort": "none"}
    assert t["body"]["reasoning"] == {"effort": "low"}  # reasoning cannot be turned off: the lowest supported effort
    assert live[1]["cost_usd"] == pytest.approx((100 * 0.12 + 50 * 0.24) / 1e6)
    # the presets themselves (no entry): Google's endpoint and key; thinking off where Gemini allows it
    llm.OPENAI_COMPAT.clear(), llm._compat_resolved.clear()
    assert llm.openai_compat_config("gemini-3-pro-preview") == {"base_url": llm.GEMINI_BASE, "api_key_env": "GEMINI_API_KEY",
                                                                 "extra": {"reasoning_effort": "minimal"}, "model": "gemini-3-pro-preview"}
    assert llm.openai_compat_config("gemini-2.5-flash")["extra"] == {"reasoning_effort": "none"}
    assert llm.cost_usd("gemini-3-pro-preview", {"input_tokens": 1_000_000}) == 5.0  # unpriced: the (5, 25) default


def test_routing_and_records_of_the_other_routes_are_unchanged(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_COMPAT", {})
    names = {"claude-haiku-4-5": "anthropic", "gpt-5-mini": "openai", "gpt-6-astra": "openai", "Qwen/Qwen3-14B": "vllm",
             "together:moonshotai/Kimi-K3": "together", "DeepSeek-V4-Flash": "together", "gemini-3.8-flash": "openai_compat",
             "openrouter:meta-llama/llama-4-maverick": "openai_compat"}
    assert {m: llm.provider_of(m) for m in names} == names
    leaderboard = ["claude-opus-5-5", "claude-haiku-4-5", "gpt-5-mini", "Qwen/Qwen3-14B", "together:zai-org/GLM-5.3"]
    assert llm.run_config(leaderboard) == llm.run_config()  # no new key in the record of a run without such a model
    assert "openai_compat" not in llm.run_config(leaderboard) and "openai_effort" not in llm.run_config()
    assert llm.run_config(["gemini-3.8-flash"])["openai_compat"]["gemini-3.8-flash"]["api_key_env"] == "GEMINI_API_KEY"
    # tool calls are sent back exactly as before on every other route, even if one carried a provider attachment
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "list_files", "input": {},
                                                                  "extra_content": {"x": 1}, "provider_echo": {"y": 2}}]}]
    assert llm._to_openai("s", msgs) == [{"role": "system", "content": "s"}, {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]}]
    # an explicit entry wins over the vLLM default, so a partner can be mapped to a hosted copy
    monkeypatch.setitem(llm.OPENAI_COMPAT, "Qwen/Qwen3-14B", {"base_url": "https://example.org/v1", "api_key_env": "K"})
    assert llm.provider_of("Qwen/Qwen3-14B") == "openai_compat"


def test_vllm_request_keeps_its_chat_template_switch(monkeypatch):
    seen = []

    class C:
        def create(self, **kw):
            seen.append(kw)
            return NS(choices=[NS(message=NS(content="ok", tool_calls=None), finish_reason="stop")], id="x",
                      usage=NS(prompt_tokens=1, completion_tokens=1))
    monkeypatch.delenv("HARNESS_DRY_RUN", raising=False)
    monkeypatch.setattr(llm, "_client", lambda provider, model=None: NS(chat=NS(completions=C())))
    monkeypatch.setattr(llm, "_log", lambda e: None)
    llm.call_model("Qwen/Qwen3-14B", "s", [{"role": "user", "content": "x"}], max_tokens=10)
    assert seen[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}} and seen[0]["max_tokens"] == 10


def test_new_openai_model_effort_on_chat_and_on_the_responses_fallback(monkeypatch):
    """HARNESS_OPENAI_EFFORT: the effort goes on chat completions; a model that rejects function tools with reasoning there
    moves to the Responses API with the same effort (as gpt-6-astra does with its built-in setting)."""
    seen, resp_seen = [], []

    class BadRequest(Exception):
        status_code = 400

    class Chat:
        def create(self, **kw):
            seen.append(kw)
            raise BadRequest("Function tools with reasoning_effort are not supported for gpt-x in /v1/chat/completions. "
                             "To use function tools, use /v1/responses or set reasoning_effort to 'none'.")

    class Responses:
        def create(self, **kw):
            resp_seen.append(kw)
            return NS(output=[], output_text="done", status="completed", id="r", usage=NS(input_tokens=5, output_tokens=2))
    monkeypatch.delenv("HARNESS_DRY_RUN", raising=False)
    monkeypatch.setattr(llm, "_client", lambda provider, model=None: NS(chat=NS(completions=Chat()), responses=Responses()))
    monkeypatch.setattr(llm, "_log", lambda e: None)
    monkeypatch.setattr(llm, "total_spent", lambda: 0.0)
    monkeypatch.setattr(llm, "_RESPONSES_ONLY", set())
    monkeypatch.setitem(llm.OPENAI_EFFORT, "gpt-x", "low")
    monkeypatch.setitem(llm.RESPONSES_REASONING, "gpt-x", {"effort": "low"})
    out = llm.call_model("gpt-x", "s", [{"role": "user", "content": "x"}], tools=TOOLS, max_tokens=10)
    assert out["text"] == "done" and seen[0]["reasoning_effort"] == "low" and resp_seen[0]["reasoning"] == {"effort": "low"}
    assert "openai_effort" in llm.run_config()


def test_missing_setup_names_the_route_or_the_key(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_COMPAT", {"no-url": {"api_key_env": "K"}})
    monkeypatch.setattr(llm, "_compat_resolved", {})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert "unknown provider" in llm.missing_setup("mystery-model")
    assert llm.missing_setup("gemini-3.8-flash") == "gemini-3.8-flash: GEMINI_API_KEY not set (add it to .env)"
    assert "no base_url" in llm.missing_setup("no-url")
    assert llm.missing_setup("Qwen/Qwen3-14B") is None  # local vLLM: no key
