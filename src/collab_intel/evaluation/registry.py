"""The model registry: models/<id>.yaml, one file per evaluated configuration (docs/evaluation_policy.md).

A configuration is a model name the harness routes (src/collab_intel/harness/llm.py) plus the settings it was run
with; another reasoning setting of the same model is its own file (a labelled row on the leaderboard). The file holds
no key: `access.key_env` names the environment variable that holds it, and `env` (extra harness environment, e.g.
HARNESS_OPENAI_COMPAT) may not set a variable whose name looks like a secret. models/_template.yaml documents every
field; files whose name starts with "_" are not entries.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

STATUSES = ("verified", "self-reported", "pending")
REQUIRED = ("id", "display_name", "model", "status", "access", "reasoning", "budget_usd", "evaluation")
RECORD_FIELDS = ("status", "evaluation", "notes")  # the record of an evaluation, not the configuration it ran
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.I)
HARNESS_ENV = ("HARNESS_OPENAI_COMPAT", "HARNESS_OPENAI_EFFORT", "HARNESS_PRICES", "HARNESS_VLLM_ENDPOINTS",
               "HARNESS_EFFORT_HEADROOM", "TOGETHER_MODELS")


def problems(cfg: dict[str, Any], path: Path | None = None) -> list[str]:
    """What is wrong with a registry entry (empty if nothing)."""
    out = [f"missing field {k!r}" for k in REQUIRED if k not in cfg]
    if out:
        return out
    if path is not None and path.stem != cfg["id"]:
        out.append(f"id {cfg['id']!r} differs from the file name {path.stem!r}")
    if cfg["status"] not in STATUSES:
        out.append(f"status must be one of {', '.join(STATUSES)}")
    acc = cfg.get("access") or {}
    for k in ("provider", "endpoint", "key_env", "data_policy"):
        if not acc.get(k):
            out.append(f"access.{k} is missing (state it, even when the harness default applies)")
    if acc.get("key_env") and not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(acc["key_env"])):
        out.append("access.key_env must be the NAME of an environment variable, never a key")
    r = cfg.get("reasoning") or {}
    if not r.get("label") or "sent" not in r:
        out.append("reasoning.label and reasoning.sent are required (label: lightest, or the effort of a variant)")
    for k, v in (cfg.get("env") or {}).items():
        if SECRET_NAME.search(k):
            out.append(f"env.{k}: a secret never goes in the registry; name its variable in access.key_env")
        if k not in HARNESS_ENV:
            out.append(f"env.{k}: not a harness setting ({', '.join(HARNESS_ENV)})")
        if isinstance(v, str) and re.search(r"(sk-|key-)[A-Za-z0-9]{16,}", v):
            out.append(f"env.{k} looks like it holds a key")
    p = cfg.get("price_usd_per_mtok")
    if p is not None and (not isinstance(p, list) or len(p) != 2):
        out.append("price_usd_per_mtok must be [input, output] in USD per million tokens")
    try:
        if float(cfg["budget_usd"]) <= 0:
            out.append("budget_usd must be positive")
    except (TypeError, ValueError):
        out.append("budget_usd must be a number")
    if not (cfg.get("evaluation") or {}).get("scored_set"):
        out.append("evaluation.scored_set is required (benchmark/final_set.json for a new model)")
    return out


def run_config_sha256(path: str | Path) -> str:
    """The sha256 of the configuration the runs depend on: the entry without RECORD_FIELDS, as canonical JSON (comments
    and layout left out). Recording an evaluation (its status, dates, benchmark version, notes) leaves it unchanged."""
    cfg = yaml.safe_load(Path(path).read_text()) or {}
    body = {k: v for k, v in cfg.items() if k not in RECORD_FIELDS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def load(path: str | Path) -> dict[str, Any]:
    """A registry entry, checked (SystemExit listing the problems)."""
    p = Path(path)
    cfg = yaml.safe_load(p.read_text()) or {}
    if bad := problems(cfg, p):
        raise SystemExit(f"{p}: " + "; ".join(bad))
    return cfg


def entries(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every entry of root/models (not the files starting with "_"), checked, in file-name order."""
    return [(p, load(p)) for p in sorted((root / "models").glob("*.yaml")) if not p.name.startswith("_")]


def harness_env(cfg: dict[str, Any]) -> dict[str, str]:
    """The environment a configuration's runs need: its `env` (objects JSON-encoded) and its price for the usage log and
    the budget guard (HARNESS_PRICES, merged with an `env` entry of the same name)."""
    out = {k: v if isinstance(v, str) else json.dumps(v) for k, v in (cfg.get("env") or {}).items()}
    price = cfg.get("price_usd_per_mtok")
    if price is not None:
        prices = json.loads(out.get("HARNESS_PRICES", "{}") or "{}")
        prices.setdefault(cfg["model"], [float(price[0]), float(price[1])])
        out["HARNESS_PRICES"] = json.dumps(prices)
    return out
