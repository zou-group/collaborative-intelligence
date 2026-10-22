"""The Inspect port sends what the leaderboard sent, byte for byte (no API key, no paid call).

Every provider request of a run (the evaluated seat, the partners, the judges) is recorded by fake provider clients put
in place of the harness's (llm._client), which answer with fixed replies. The same scenario is run twice:

  harness   episode.run_episode, as scripts/run_benchmark.py runs it, with the evaluated model claude-opus-5-5;
  Inspect   the Inspect task with `--model collab_intel/claude-opus-5-5` and the default model roles (the partners and
            judges are roles whose default is the leaderboard's model on the harness's route), and again with the roles
            bound explicitly to those defaults.

The recorded request payloads (every keyword argument sent to the provider's SDK, JSON with sorted keys) must be equal,
call by call. Two scenarios: a chat scenario with Anthropic and OpenAI partners and the Anthropic referee and second
rater, and a coding scenario with tools and the vLLM Qwen3-14B partner.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("inspect_ai")

from collab_intel.harness import llm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PARTNERS = ["claude-haiku-4-5", "gpt-5-mini", "Qwen/Qwen3-14B"]
CHAT = "benchmark/scenarios/emotional_intelligence/h_l_hurricane_watch_selfdoubt"
CODING = "benchmark/scenarios/taking_initiative/b_loadsheet_late_transfer__r5"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, default=str)


def _reply(messages: list[dict]) -> str:
    last = _text(messages[-1].get("content", "")) if messages else ""
    if "\n\nLabels:\n" in last:
        label = last.split("\n\nLabels:\n", 1)[1].split(":", 1)[0].lstrip("- ").strip()
        return json.dumps({"label": label, "confidence": 0.9, "evidence": "fixed"})
    if "Reply with JSON" in last:
        return json.dumps({"answer": "fixed answer", "reasoning": "fixed"})
    return "A fixed message to the team."


class Recorder:
    """Fake SDK clients for every route the run uses; each request's keyword arguments are kept."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def _keep(self, provider: str, kwargs: dict) -> None:
        self.calls.append((provider, json.dumps(kwargs, sort_keys=True, default=str)))

    def client(self, provider: str, model: str = ""):
        rec = self

        class Client:
            def with_options(self, **_):
                return self

        c = Client()
        if provider == "anthropic":
            def create(**kwargs):
                rec._keep(provider, kwargs)
                return NS(content=[NS(type="text", text=_reply(kwargs["messages"]))], stop_reason="end_turn",
                          usage=NS(model_dump=lambda: {"input_tokens": 11, "output_tokens": 7}), _request_id="req_fake")
            c.messages = NS(create=create)
        else:
            def create(**kwargs):
                rec._keep(provider, kwargs)
                msg = NS(content=_reply(kwargs["messages"]), tool_calls=None, reasoning_content=None, model_extra={})
                return NS(choices=[NS(message=msg, finish_reason="stop")], id="chatcmpl_fake", model_extra={},
                          usage=NS(prompt_tokens=11, completion_tokens=7, total_tokens=18))
            c.chat = NS(completions=NS(create=create))
        return c


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    rec = Recorder()
    monkeypatch.delenv("HARNESS_DRY_RUN", raising=False)
    monkeypatch.setattr(llm, "_client", rec.client)
    monkeypatch.setattr(llm, "USAGE_LOG", tmp_path / "usage.jsonl")
    monkeypatch.setattr(llm, "BUDGET_USD", 1e9)
    from collab_intel.harness import judges
    for attr in ("JUDGE_MODEL", "SECOND_RATER", "TIEBREAK_RATER"):  # the task sets them for the eval; restored after
        monkeypatch.setattr(judges, attr, getattr(judges, attr))
    from collab_intel.inspect_eval import task as task_mod
    monkeypatch.setattr(task_mod, "_check_setup", lambda models: None)  # no key or server check: the clients are fakes
    return rec


def _inspect_run(scenario: str, root: Path, model_roles: dict | None = None) -> None:
    from inspect_ai import eval as inspect_eval
    from inspect_ai.model import get_model

    from collab_intel.inspect_eval.task import collaborative_intelligence

    # seeds="0": the one harness run it is compared with (the set's three test seeds are the same request at seeds 1, 2)
    log = inspect_eval(collaborative_intelligence(scenarios=scenario.rsplit("/", 1)[-1], versions="test", gate=False,
                                                  runs_root=str(root), sandbox="harness", seeds="0"),
                       model=get_model("collab_intel/claude-opus-5-5", memoize=False), model_roles=model_roles,
                       log_dir=str(root / "logs"), display="none")[0]
    assert log.status == "success", log.error
    sample = log.samples[0]
    assert any(type(e).__name__ == "ModelEvent" for e in sample.events), "the evaluated model's calls are in the log"
    t = json.loads(next(root.rglob("manipulation_seed0.json")).read_text())
    assert t["inspect"]["leaderboard_conditions"] is True and t["inspect"]["role_overrides"] == {}
    assert t["partner_models"] == PARTNERS


@pytest.mark.parametrize("scenario", [CHAT, CODING])
def test_inspect_requests_equal_the_harness_requests(scenario, recorder, tmp_path):
    from collab_intel.harness.episode import run_episode

    run_episode(ROOT / scenario, "manipulation", "claude-opus-5-5", PARTNERS, seed=0)
    harness = list(recorder.calls)
    recorder.calls.clear()
    _inspect_run(scenario, tmp_path / "a")
    default_roles = list(recorder.calls)
    recorder.calls.clear()
    _inspect_run(scenario, tmp_path / "b", model_roles={
        "referee": "collab_intel/claude-sonnet-4-6", "second_rater": "collab_intel/claude-haiku-4-5",
        "partner_1": "collab_intel/claude-haiku-4-5", "partner_2": "collab_intel/gpt-5-mini",
        "partner_3": "collab_intel/Qwen/Qwen3-14B"})
    bound_roles = list(recorder.calls)

    providers = {p for p, _ in harness}
    assert {"anthropic", "openai"} <= providers and ("vllm" in providers) == (scenario == CODING)
    assert len(harness) > 20
    assert default_roles == harness  # every request, in order, byte for byte
    assert bound_roles == harness
    sent = [json.loads(k) for p, k in harness if p == "anthropic"]
    assert any(k.get("output_config") == {"effort": "low"} for k in sent), "the evaluated model's leaderboard setting"
    assert any(k.get("extra_body", {}).get("temperature") == 0.0 and k["model"] == "claude-sonnet-4-6" for k in sent)
    if scenario == CODING:
        assert any(k.get("tools") for k in sent)
        vllm = [json.loads(k) for p, k in harness if p == "vllm"]
        assert all(k["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}} for k in vllm)


def test_a_role_bound_elsewhere_goes_through_inspect(recorder, tmp_path):
    """A role bound to a model other than a collab_intel one is called through Inspect: the harness's clients are
    not used for it, and the run records the override."""
    from inspect_ai.model import ModelOutput, get_model

    partner = get_model("mockllm/partner", custom_outputs=lambda *a: ModelOutput.from_content("mockllm/partner", "hi"),
                        memoize=False)
    from inspect_ai import eval as inspect_eval

    from collab_intel.inspect_eval.task import collaborative_intelligence

    log = inspect_eval(collaborative_intelligence(scenarios=CHAT.rsplit("/", 1)[-1], versions="test", gate=False,
                                                  runs_root=str(tmp_path), sandbox="harness"),
                       model=get_model("collab_intel/claude-opus-5-5", memoize=False),
                       model_roles={"partner_2": partner}, log_dir=str(tmp_path / "logs"), display="none")[0]
    assert log.status == "success"
    t = json.loads(next(tmp_path.rglob("manipulation_seed0.json")).read_text())
    assert t["partner_models"][1] == "inspect-role/partner_2" and t["inspect"]["leaderboard_conditions"] is False
    assert t["inspect"]["role_overrides"] == {"partner_2": "mockllm/partner"}
    assert not any(json.loads(k).get("model") == "gpt-5-mini" for _, k in recorder.calls)


def test_the_hosted_qwen_partner(recorder, tmp_path, monkeypatch):
    """-T qwen_partner=openrouter: the Qwen3-14B seats go to OpenRouter's DeepInfra endpoint with thinking off, the
    local server's sampling (temperature 0.6, top_p 0.95, top_k 20) and the harness's data policy; the run says it is
    not under the leaderboard's conditions."""
    monkeypatch.setattr(llm, "OPENAI_COMPAT", dict(llm.OPENAI_COMPAT))  # the task sets the entry for the process
    monkeypatch.setattr(llm, "_compat_resolved", {})
    from inspect_ai import eval as inspect_eval
    from inspect_ai.model import get_model

    from collab_intel.inspect_eval.task import collaborative_intelligence

    log = inspect_eval(collaborative_intelligence(scenarios=CODING.rsplit("/", 1)[-1], versions="test", gate=False,
                                                  runs_root=str(tmp_path), sandbox="harness", qwen_partner="openrouter"),
                       model=get_model("collab_intel/claude-opus-5-5", memoize=False), log_dir=str(tmp_path / "logs"),
                       display="none")[0]
    assert log.status == "success", log.error
    sent = [json.loads(k) for p, k in recorder.calls if p == "openai_compat"]
    assert sent and not [k for p, k in recorder.calls if p == "vllm"]
    for k in sent:
        assert k["model"] == "qwen/qwen3-14b" and k["extra_body"]["reasoning"] == {"effort": "none"}
        assert (k["extra_body"]["temperature"], k["extra_body"]["top_p"], k["extra_body"]["top_k"]) == (0.6, 0.95, 20)
        assert k["extra_body"]["provider"] == {"only": ["deepinfra"], "allow_fallbacks": False,
                                               **llm.OPENROUTER_PROVIDER_POLICY}
    t = json.loads(next(tmp_path.rglob("manipulation_seed0.json")).read_text())
    assert t["partner_models"] == PARTNERS and t["inspect"]["leaderboard_conditions"] is False
    assert t["harness_config"]["openai_compat"]["Qwen/Qwen3-14B"]["model"] == "qwen/qwen3-14b"
