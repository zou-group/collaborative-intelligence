"""Check continued runs against their originals (stop audit, 2026-10-12), independently of the harness's own replay check.

    uv run python scripts/check_continuations.py [--originals results/analysis/stop_rule_fix/originals] [--out FILE]

For every original under --originals (a backup of the run file taken before scripts/continue_runs.py --run moved it),
the run at the same path in the repository should now be its continuation. The check, field by field:
  - the run's identity (episode, version, seed, evaluated model, partners, seats) is the original's;
  - messages[:from_message] equal the original's messages[:from_message] exactly (every key of every entry);
  - classifier_calls, tool_calls, and the sandbox's edits, scripted edits and test runs: the first n entries (n as
    `continuation.replayed` records) equal the original's first n exactly;
  - turn orders of the rounds before from_round equal the original's;
  - the continuation block names the original (continued_from/ copy, byte-identical to the backup).
It also reports what changed at the continuation point (the original's entry at from_message vs the new one) and the
new outcome. Nothing is called; nothing is written except --out.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def first_diff(a: list[Any], b: list[Any]) -> str | None:
    if len(a) != len(b):
        return f"lengths {len(a)} vs {len(b)}"
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            if isinstance(x, dict) and isinstance(y, dict):
                keys = sorted(k for k in set(x) | set(y) if x.get(k) != y.get(k))
                return f"entry {i}: keys {keys}"
            return f"entry {i}"
    return None


def check(orig_path: Path, originals: Path) -> dict[str, Any]:
    relp = orig_path.relative_to(originals)
    new_path = ROOT / relp
    o = json.loads(orig_path.read_text())
    out: dict[str, Any] = {"run": str(relp), "model": o.get("evaluated_model")}
    if not new_path.exists():
        return {**out, "status": "missing", "problems": ["no run at the original path"]}
    t = json.loads(new_path.read_text())
    c = t.get("continuation")
    if not c:
        return {**out, "status": "not_continued", "problems": [],
                "unchanged": hashlib.sha256(new_path.read_bytes()).hexdigest() == hashlib.sha256(orig_path.read_bytes()).hexdigest()}
    problems: list[str] = []
    for k in ("episode", "version", "seed", "evaluated_model", "partner_models", "evaluated_seat"):
        if t.get(k) != o.get(k):
            problems.append(f"{k} differs")
    # seats: the same seats with the same settings; only environment seats' address triggers may differ (they were
    # widened on 2026-10-12, which is the fix; the replay checks that every replayed environment turn is the original's)
    seats_t, seats_o = t.get("seats") or [], o.get("seats") or []
    trigger_changes = []
    if len(seats_t) != len(seats_o):
        problems.append("number of seats differs")
    for a, b in zip(seats_t, seats_o):
        diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        if diff == ["triggers"]:
            trigger_changes.append(a.get("name"))
        elif diff:
            problems.append(f"seat {b.get('name')}: {diff} differ")
    fm = int(c["from_message"])
    rp = c.get("replayed") or {}
    d = first_diff(t["messages"][:fm], o["messages"][:fm])
    if d:
        problems.append(f"messages[:{fm}] differ: {d}")
    for key, n in (("classifier_calls", rp.get("classifier_calls")), ("tool_calls", rp.get("tool_calls"))):
        n = int(n or 0)
        if len(o.get(key) or []) < n:
            problems.append(f"{key}: the original has fewer than {n}")
        d = first_diff((t.get(key) or [])[:n], (o.get(key) or [])[:n])
        if d:
            problems.append(f"{key}[:{n}] differ: {d}")
    sb, sb0 = t.get("sandbox") or {}, o.get("sandbox") or {}
    for key, n in (("edits", rp.get("edits")), ("test_runs", rp.get("test_runs"))):
        n = int(n or 0)
        d = first_diff((sb.get(key) or [])[:n], (sb0.get(key) or [])[:n])
        if d:
            problems.append(f"sandbox {key}[:{n}] differ: {d}")
    fr = int(c["from_round"])
    to_new = (t.get("turn_orders") or [])[:fr - 1]  # one entry per round, round 1 first
    to_old = (o.get("turn_orders") or [])[:fr - 1]
    d = first_diff(to_new, to_old)
    if d:
        problems.append(f"turn orders before round {fr} differ: {d}")
    if t.get("system_prompts") != o.get("system_prompts"):
        problems.append("system prompts differ")
    kept = ROOT / str(c.get("of") or "")
    if not c.get("of") or not kept.exists():
        problems.append(f"continuation.of does not name an existing file: {c.get('of')}")
    elif kept.read_bytes() != orig_path.read_bytes():
        problems.append("continued_from copy is not byte-identical to the backup")
    ob = o["messages"][fm] if fm < len(o["messages"]) else None
    nb = t["messages"][fm] if fm < len(t["messages"]) else None
    sc, sc0 = t.get("score") or {}, o.get("score") or {}
    return {**out, "status": "ok" if not problems else "problem", "problems": problems,
            "from_round": fr, "from_message": fm, "boundary": (c.get("boundary") or {}).get("kind"),
            "replayed": rp, "replay_warnings": c.get("replay_warnings") or [],
            "environment_triggers_changed": trigger_changes,
            "environment_divergence": c.get("environment_divergence") or [],
            "original_at_point": None if ob is None else {k: ob.get(k) for k in ("round", "seat", "kind", "label", "action") if k in ob},
            "new_at_point": None if nb is None else {k: nb.get(k) for k in ("round", "seat", "kind", "label", "action") if k in nb},
            "original_ended": o.get("ended"), "ended": t.get("ended"), "rounds_run": t.get("rounds_run"),
            "original_outcome": sc0.get("outcome"), "original_pass": sc0.get("pass"),
            "outcome": sc.get("outcome"), "pass": sc.get("pass"), "cost_usd": t.get("cost_usd"),
            "messages": len(t["messages"]), "original_messages": len(o["messages"])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--originals", default="results/analysis/stop_rule_fix/originals")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    originals = (ROOT / a.originals).resolve()
    rows = [check(p, originals) for p in sorted(originals.rglob("*.json"))]
    for r in rows:
        print(f"{r['status']:13s} {r['run']}  " + ("; ".join(r["problems"]) if r["problems"] else
              f"R{r.get('from_round')} msg {r.get('from_message')} -> pass={r.get('pass')} {json.dumps(r.get('outcome'))}"))
    n = {s: sum(1 for r in rows if r["status"] == s) for s in ("ok", "problem", "missing", "not_continued")}
    print(n)
    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=1, default=str) + "\n")
    return 0 if n["problem"] == 0 and n["missing"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
