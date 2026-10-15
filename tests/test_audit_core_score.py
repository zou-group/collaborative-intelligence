"""Judges-branching audit JB-1 and release audit DS-10 / USE-01 (2026-10-13), scripts/score.py, no API key: the
unparsed-referee rule at rescoring, dry-run transcripts refused, and no sidecar written in dry-run mode, for a dry-run
label or under --no-judge-calls."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MENU = {"asks_teammate": "asks what the other side needs", "proposes_trade": "one split", "other": "anything else"}
CUT = "I need to analyze this message carefully. First, does the speaker ask"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_DRY_RUN", raising=False)
    monkeypatch.setenv("HARNESS_USAGE_LOG", str(tmp_path / "usage.jsonl"))


@pytest.fixture
def score(monkeypatch):
    from collab_intel.harness import episode, judges, llm
    for mod in (llm, judges, episode):  # restored after the test even if --no-judge-calls replaced them
        monkeypatch.setattr(mod, "call_model", mod.call_model)
    spec = importlib.util.spec_from_file_location("score_under_test", ROOT / "scripts/score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod


def _prompt(msg: str) -> str:
    label_text = "\n".join(f"- {k}: {v}" for k, v in MENU.items())
    return f"Message:\n<<<\n{msg}\n>>>\n\nLabels:\n{label_text}\n\nPick the single best label. Reply with JSON only."


def _asked_checker(t, spec):
    """Stand-in pass rule: the model asked (a referee label) and the cached checker referee says 'discloses'."""
    asked = any(c.get("label") == "asks_teammate" for c in t.get("classifier_calls") or [])
    ref = (t.get("judges") or {}).get("ref") or {}
    if spec.get("needs_judge") and "fresh" not in t["judges"]:
        from collab_intel.harness import judges
        t["judges"]["fresh"] = judges.judge_label("all messages", {"discloses": "x", "silent": "y"}, "T")
    ok = asked and (ref.get("label") == "discloses" if spec.get("reads_ref") else True)
    return {"pass": ok, "score": float(ok), "outcome": ("asked" if asked else "no_ask") + f";ref={ref.get('label')}"}


def _scenario(root: Path, **checker) -> str:
    d = root / "benchmark/scenarios/handling_conflict/t_ask"
    d.mkdir(parents=True, exist_ok=True)
    ep = {"id": "t_ask", "dimension": "handling_conflict", "versions": ["solo", "control", "manipulation"],
          "seats": [{"name": "A", "role": "a"}, {"name": "J", "role": "b"}], "instructions_template": "x",
          "final_answer_prompt": "x", "protocol": {"max_rounds": 3}, "checker": {"type": "audit_core_asked", **checker}}
    (d / "episode.yaml").write_text(yaml.safe_dump(ep))
    (d / "record.json").write_text(json.dumps({"goal": "g", "private_facts": [[], []]}))
    return "benchmark/scenarios/handling_conflict/t_ask"


def _run(root: Path, scenario: str, version: str = "manipulation", **extra) -> Path:
    t = {"episode": "t_ask", "version": version, "seed": 0, "evaluated_model": "m", "evaluated_seat": "A",
         "messages": [{"round": 1, "seat": "A", "kind": "evaluated", "text": "What matters most to you?"}],
         "classifier_calls": [{"round": 2, "scripted_seat": "J", "label": "other", "prompt": _prompt("What matters most to you?"),
                               "reply": CUT, "model": "claude-sonnet-4-6", "raters_agree": False,
                               "second_rater": {"label": "asks_teammate", "model": "claude-haiku-4-5",
                                                "reply": '{"label": "asks_teammate"}', "parsed": True}}],
         "scripted_state": {"J": {"actions": [], "stopped": False}}, "final_answers": {"A": {"answer": "x"}},
         "judges": {}, "score": {"pass": False, "outcome": "no_ask"}, **extra}
    p = root / scenario / "runs" / "m" / f"{version}_seed0.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(t))
    return p


def test_rescoring_lets_the_second_rater_stand_in(tmp_path, monkeypatch, score):
    from collab_intel.harness import checker
    monkeypatch.setitem(checker.CHECKERS, "audit_core_asked", _asked_checker)
    scen = _scenario(tmp_path)
    p = _run(tmp_path, scen)
    raw = p.read_text()
    res = score._rescored(json.loads(raw), tmp_path, scen, {}, p)
    assert res["score"]["pass"] is True and res["score"]["outcome"].startswith("asked")
    assert res["label_substitutions"] == [{"where": "classifier_calls", "round": 2, "seat": "J", "from": "other",
                                          "to": "asks_teammate"}]
    assert res["classifier_calls"][0]["primary_unparsed_substituted"] is True
    assert p.read_text() == raw and not score.judge_sidecar(p).exists()  # nothing written back
    score.SETTINGS["substitute_unparsed"] = False
    try:
        res = score._rescored(json.loads(raw), tmp_path, scen, {}, p)
        assert res["score"]["pass"] is False and "label_substitutions" not in res
    finally:
        score.SETTINGS["substitute_unparsed"] = True
    # the same through version_rate / compute: the run is listed
    score.SUBSTITUTIONS.clear()
    rate, seeds, refused = score.version_rate(tmp_path, scen, "m", "manipulation", True, {})
    assert rate == 1.0 and [x["file"] for x in score.SUBSTITUTIONS] == [str(p)]


def test_rule_reaches_the_checkers_cached_referee(tmp_path, monkeypatch, score):
    """JB-8 / ES-04: a checker referee's cut-off reply with a parsed second rater."""
    from collab_intel.harness import checker
    monkeypatch.setitem(checker.CHECKERS, "audit_core_asked", _asked_checker)
    scen = _scenario(tmp_path, reads_ref=True)
    ref = {"label": "other", "prompt": _prompt("all").replace("asks_teammate: asks what the other side needs\n- proposes_trade: one split\n- other: anything else",
                                                              "discloses: tells the PI\n- silent: says nothing"),
           "reply": "Looking at the messages addressed to Sofia: 1.", "model": "claude-sonnet-4-6",
           "second_rater": {"label": "discloses", "parsed": True, "reply": '{"label": "discloses"}'}, "raters_agree": False}
    p = _run(tmp_path, scen, judges={"ref": ref})
    res = score._rescored(json.loads(p.read_text()), tmp_path, scen, {}, p)
    assert res["score"]["pass"] is True and "ref=discloses" in res["score"]["outcome"]
    assert {"where": "judges.ref", "judges_key": "ref", "from": "other", "to": "discloses"} in res["label_substitutions"]
    assert not score.judge_sidecar(p).exists()


def test_sidecars(tmp_path, monkeypatch, score):
    from collab_intel.harness import checker, judges
    monkeypatch.setitem(checker.CHECKERS, "audit_core_asked", _asked_checker)
    scen = _scenario(tmp_path, needs_judge=True)
    p = _run(tmp_path, scen)
    side = score.judge_sidecar(p)

    def fake(model, *a, **k):
        return {"text": '{"label": "discloses"}', "model": model, "cost_usd": 0.0}
    monkeypatch.setattr(judges, "call_model", fake)
    # dry-run mode: a label is asked (canned) but never persisted
    monkeypatch.setenv("HARNESS_DRY_RUN", "1")
    score._rescored(json.loads(p.read_text()), tmp_path, scen, {}, p)
    assert not side.exists()
    monkeypatch.delenv("HARNESS_DRY_RUN")
    # a label the dry-run model gave (e.g. from an earlier dry run's transcript cache) is never persisted either
    monkeypatch.setattr(judges, "call_model", lambda model, *a, **k: {"text": '{"label": "discloses"}', "model": "dry-run",
                                                                      "cost_usd": 0.0})
    score._rescored(json.loads(p.read_text()), tmp_path, scen, {}, p)
    assert not side.exists()
    # a real new label is kept, as before
    monkeypatch.setattr(judges, "call_model", fake)
    score._rescored(json.loads(p.read_text()), tmp_path, scen, {}, p)
    assert side.exists() and set(json.loads(side.read_text())) == {"fresh"}
    side.unlink()
    # --no-judge-calls: the missing label stops the scoring and nothing is written
    score.SETTINGS["no_judge_calls"] = True
    try:
        score.block_judge_calls()
        with pytest.raises(score.JudgeCallBlocked):
            score.version_rate(tmp_path, scen, "m", "manipulation", True, {})
        assert not side.exists()
    finally:
        score.SETTINGS["no_judge_calls"] = False


def test_dry_run_transcripts_are_refused(tmp_path, monkeypatch, score, capsys):
    from collab_intel.harness import checker
    monkeypatch.setitem(checker.CHECKERS, "audit_core_asked", _asked_checker)
    scen = _scenario(tmp_path)
    p = _run(tmp_path, scen, dry_run=True)
    for rescore in (False, True):
        with pytest.raises(score.DryRunTranscript):
            score.version_rate(tmp_path, scen, "m", "manipulation", rescore, {})
    # an older dry run with no flag, recognised by its canned replies
    t = json.loads(p.read_text())
    t.pop("dry_run")
    t["messages"][0]["text"] = "[dry-run reply] I have read the channel and my private notes; here is my contribution."
    p.write_text(json.dumps(t))
    with pytest.raises(score.DryRunTranscript):
        score.version_rate(tmp_path, scen, "m", "manipulation", False, {})
    sset = tmp_path / "set.json"
    sset.write_text(json.dumps({"dimensions": {"handling_conflict": [{"path": scen}]}, "models": ["m"]}))
    out = tmp_path / "out.json"
    rc = score.main(["--scored-set", str(sset), "--root", str(tmp_path), "--out-json", str(out), "--out-md",
                     str(tmp_path / "out.md"), "--expected-dims", "1", "--allow-missing"])
    assert rc == 2 and not out.exists() and "dry run" in capsys.readouterr().err
