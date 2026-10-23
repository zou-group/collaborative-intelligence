"""The submission form (src/collab_intel/evaluation/submission.py), the endpoint check (conformance.py) and
scripts/check_submission.py: no model calls."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from collab_intel.evaluation import conformance, registry  # noqa: E402
from collab_intel.evaluation import submission as sub  # noqa: E402

FORM = ROOT / "src" / "collab_intel" / "evaluation" / "submission_form.yaml"


def _cs():
    spec = importlib.util.spec_from_file_location("check_submission", ROOT / "scripts" / "check_submission.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def form(**changes):
    f = sub.load(FORM)
    for path, v in changes.items():
        d = f
        *head, last = path.split("__")
        for k in head:
            d = d.setdefault(k, {})
        if v is None:
            d.pop(last, None)
        else:
            d[last] = v
    return f


def test_template_is_a_valid_form_and_makes_valid_entries():
    f = sub.load(FORM)
    assert sub.problems(f) == []
    es = sub.entries(f, received="2026-10-22", sha256="0" * 64)
    assert [e["id"] for e in es] == ["example-1.0"]
    e = es[0]
    assert e["model"] == "submit-example-1.0" and e["status"] == "pending"
    assert e["access"]["key_env"] == "SUBMIT_EXAMPLE_1_0_KEY"
    compat = e["env"]["HARNESS_OPENAI_COMPAT"]["submit-example-1.0"]
    assert compat["base_url"] == "https://api.example.com/v1" and compat["api_key_env"] == "SUBMIT_EXAMPLE_1_0_KEY"
    assert compat["extra"] == {"reasoning_effort": "none"}
    e["budget_usd"] = 100
    assert registry.problems(e) == []


def test_entry_file_round_trips_without_anchors():
    e = sub.entries(sub.load(FORM))[0]
    e["budget_usd"] = 100
    text = sub.entry_yaml(e)
    assert "&id" not in text and "*id" not in text
    assert yaml.safe_load(text) == e


def test_effort_rows_and_routes():
    f = form(configurations=["lightest", "high"], reasoning={"lightest": {"reasoning_effort": "none"},
                                                            "high": {"reasoning_effort": "high"}})
    es = sub.entries(f)
    assert [e["id"] for e in es] == ["example-1.0", "example-1.0-high"]
    assert es[1]["model"] == "submit-example-1.0@high" and es[1]["display_name"] == "Example 1.0 (high effort)"
    assert es[1]["env"]["HARNESS_OPENAI_COMPAT"]["submit-example-1.0@high"]["extra"] == {"reasoning_effort": "high"}
    a = form(access__route="account_access", access__provider="anthropic", access__model_id="claude-next-preview",
             access__endpoint=None, reasoning={})
    e = sub.entries(a)[0]
    assert e["model"] == "claude-next-preview" and e["access"]["key_env"] == "ANTHROPIC_API_KEY" and e["env"] == {}
    o = form(access__route="public_api", access__provider="openrouter", access__model_id="lab/model-1",
             access__endpoint=None, model__released=True, model__release_date=None, publication="when_done",
             reasoning={})
    assert sub.entries(o)[0]["model"] == "openrouter:lab/model-1" and sub.entries(o)[0]["env"] == {}
    g = form(access__route="account_access", access__provider="google", access__model_id="gemini-next-preview",
             access__endpoint=None, reasoning={"lightest": {"reasoning_effort": "low"}})
    ge = sub.entries(g)[0]
    assert ge["env"] == {"HARNESS_OPENAI_COMPAT": {"gemini-next-preview": {"extra": {"reasoning_effort": "low"}}}}
    w = form(access__route="weights", access__provider=None, access__model_id=None, access__endpoint=None,
             access__weights={"repo": "lab/model-1", "revision": "0123456789abcdef0123456789abcdef01234567"},
             model__open_weights=True, reasoning={"lightest": {"chat_template_kwargs": {"enable_thinking": False}}})
    assert sub.problems(w) == []  # a 40-character commit is not taken for a key
    e = sub.entries(w)[0]
    assert e["access"]["endpoint"].startswith("http://127.0.0.1:") and e["price_usd_per_mtok"] == [0.0, 0.0]
    assert "maintainers on their own GPUs" in e["access"]["data_policy"]


@pytest.mark.parametrize("changes,expect", [
    ({"terms": "2025-01-01"}, "terms"),
    ({"contact__email": "nobody"}, "contact"),
    ({"model__release_date": None}, "release_date"),
    ({"publication": "when_done"}, "unreleased model's result is published at its release"),
    ({"access__route": "public_api"}, "public_api is for released models"),
    ({"access__route": "account_access"}, "needs a key"),
    ({"access__endpoint": "http://api.example.com/v1"}, "https base URL"),
    ({"access__data_policy": ""}, "data_policy"),
    ({"configurations": ["high"]}, "lightest"),
    ({"configurations": ["lightest", "turbo"]}, "turbo"),
    ({"notes": "key: sk-proj-abcdefghijklmnopqrstuvwx"}, "never put a key"),
    ({"notes": "token AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCD"}, "never put a key"),
    ({"reasoning": {}}, "reasoning.lightest"),
])
def test_problems_say_what_to_fix(changes, expect):
    bad = sub.problems(form(**changes))
    assert any(expect in p for p in bad), bad


def test_check_submission_without_live_sends_nothing(tmp_path, capsys):
    cs = _cs()
    assert cs.main([str(FORM), "--write-entries", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Nothing was sent" in out
    e = yaml.safe_load((tmp_path / "example-1.0.yaml").read_text())
    assert e["budget_usd"] >= 100 and registry.problems(e) == []


def test_check_submission_refuses_a_report_that_holds_the_key(tmp_path, monkeypatch, capsys):
    cs = _cs()
    secret = "sk-test-0123456789abcdefghij"
    monkeypatch.setenv("SUBMIT_EXAMPLE_1_0_KEY", secret)
    monkeypatch.setattr(cs, "live", lambda e, out, c, b: {"model": e["model"], "ok": True, "checks": {},
                                                          "problems": [], "echo": f"Bearer {secret}"})
    assert cs.main([str(FORM), "--live", "--out", str(tmp_path)]) == 3
    assert not (tmp_path / "submission_check.json").exists()
    monkeypatch.setattr(cs, "live", lambda e, out, c, b: {"model": e["model"], "ok": True, "checks": {},
                                                          "problems": []})
    assert cs.main([str(FORM), "--live", "--out", str(tmp_path)]) == 0
    rep = json.loads((tmp_path / "submission_check.json").read_text())
    assert rep["ok"] and secret not in json.dumps(rep)


class FakeLLM:
    """A well-behaved model for the endpoint check."""

    def __init__(self, good: bool = True):
        self.good, self.n = good, 0

    def call_model(self, model, system, messages, tools=None, max_tokens=1500, tag="", **kw):
        self.n += 1
        last = messages[-1]
        base = {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}, "cost_usd": 0.0}
        if not self.good:
            return {**base, "text": "I would call get_time.", "tool_calls": []}
        if tools and tools[0]["name"] == "get_time":
            if last["role"] == "user":
                return {**base, "text": "", "tool_calls": [{"id": "t1", "name": "get_time", "input": {"city": "Lisbon"}}]}
            return {**base, "text": "It is 16:20 in Lisbon.", "tool_calls": []}
        if tools:
            done = [r for m in messages if m["role"] == "tool_results" for r in m["results"]]
            plan = [("list_files", {}), ("read_file", {"path": "notes/todo.txt"}),
                    ("write_file", {"path": "status.txt", "content": "CALIBRATE SENSOR B7 BEFORE THE FRIDAY REVIEW"})]
            if len(done) < len(plan):
                name, inp = plan[len(done)]
                return {**base, "text": "", "tool_calls": [{"id": f"f{len(done)}", "name": name, "input": inp}]}
            return {**base, "text": "Done.", "tool_calls": []}
        c = last["content"]
        if "Reply with JSON only" in c:
            return {**base, "text": '{"answer": "B", "reasoning": "30 days, no new contract."}', "tool_calls": []}
        if "FAULT" in c:
            return {**base, "text": "V-12", "tool_calls": []}
        return {**base, "text": "3", "tool_calls": []}

    def provider_of(self, m):
        return "openai_compat"

    def api_model_id(self, m):
        return m

    def missing_setup(self, m):
        return None

    def run_config(self, ms):
        return {}


def test_conformance_checks_pass_a_good_endpoint_and_fail_a_bad_one(monkeypatch):
    import collab_intel.harness as h
    good = FakeLLM()
    monkeypatch.setattr(h, "llm", good, raising=False)
    monkeypatch.setitem(sys.modules, "collab_intel.harness.llm", good)
    r = conformance.run("submit-x")
    assert r["ok"], r["problems"]
    assert set(r["checks"]) == {"tool_call", "tool_result", "multi_step", "final_answer", "long_input", "concurrency"}
    bad = FakeLLM(good=False)
    monkeypatch.setattr(h, "llm", bad, raising=False)
    monkeypatch.setitem(sys.modules, "collab_intel.harness.llm", bad)
    r = conformance.run("submit-x")
    assert not r["ok"]
    assert any(p.startswith("tool_call:") and "not as text" in p for p in r["problems"])
    assert r["checks"]["tool_result"].get("skipped")


def test_form_template_and_docs_agree():
    doc = (ROOT / "docs" / "submit.md").read_text()
    assert f'Version **{sub.TERMS[-1]}**' in doc
    assert sub.load(FORM)["terms"] == sub.TERMS[-1]
    assert "Email **seanwu@stanford.edu**" in doc and "Never send a key by email" in doc
    # one way in: an email, never a public issue (an unreleased model's name is confidential)
    issues = ROOT / ".github" / "ISSUE_TEMPLATE"
    assert not (issues / "model_submission.yml").exists()
    assert "docs/submit.md" in (issues / "config.yml").read_text()


def test_form_with_copy_is_unchanged():
    f = sub.load(FORM)
    g = copy.deepcopy(f)
    sub.entries(f)
    assert f == g


def test_smoke_leaves_the_gpqa_folders_as_it_found_them(tmp_path, monkeypatch):
    cs = _cs()
    folder = "benchmark/scenarios/keeping_the_team_on_track/h_r_gpqa_example"
    (tmp_path / "kept").mkdir()

    def fake_unpack(archive, dest):
        (dest / folder).mkdir(parents=True)
        (dest / folder / "episode.yaml").write_text("x")
        return {"written": [f"{folder}/episode.yaml"], "same": [], "conflicts": [], "skipped": []}

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(cs, "ROOT", tmp_path)
    monkeypatch.setattr(cs, "unpack_gpqa", fake_unpack)
    monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: Done())
    res = cs.smoke(sub.entries(sub.load(FORM))[0], tmp_path / "out")
    assert res["ok"]
    assert not (tmp_path / folder).exists() and (tmp_path / "kept").is_dir()
