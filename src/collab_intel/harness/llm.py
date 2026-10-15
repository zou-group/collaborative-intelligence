"""Single model-call function; providers are chosen from the model name.

  claude-*                  Anthropic API (ANTHROPIC_API_KEY)
  gpt-* / o*                OpenAI API (OPENAI_API_KEY)
  anything in VLLM_ENDPOINTS local vLLM server, OpenAI-compatible (free); default map below,
                            override with HARNESS_VLLM_ENDPOINTS='{"Qwen/Qwen3-8B":"http://localhost:8002/v1"}'
                            (any OpenAI-compatible server; HARNESS_VLLM_API_KEY if it needs a key)
  anything in HARNESS_OPENAI_COMPAT, gemini-*, openrouter:<model id>
                            a hosted OpenAI-compatible provider, each model with its own base URL, key variable and
                            request extras (OPENAI_COMPAT below; gemini-* uses GEMINI_API_KEY, openrouter: OPENROUTER_API_KEY)

Messages use one neutral format so the episode code never sees provider details:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "input"}]}
  {"role": "tool_results", "results": [{"id", "content"}]}
Tools use the Anthropic JSON-schema shape ({"name", "description", "input_schema"}) and are
converted for OpenAI-style providers.

Returns {"text", "tool_calls", "stop_reason", "usage", "cost_usd", "model", "reasoning"} plus "meta" when an empty
reply was retried with a larger budget or the provider refused (see call_model).
Every call is appended to the usage log (HARNESS_USAGE_LOG, default data/processed/harness_usage.jsonl) with an
estimated cost; a hard budget stops paid calls when the log total exceeds HARNESS_BUDGET_USD (default 10).
HARNESS_DRY_RUN=1 returns canned replies and logs nothing.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")

# USD per 1M tokens (input, output). OpenAI figures are approximate list prices; local vLLM is free.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-4-6": (3.00, 15.00), "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4-6": (5.00, 25.00), "claude-opus-5": (5.00, 25.00), "claude-opus-5-5": (4.00, 20.00),
    "claude-fable-5": (10.00, 50.00), "claude-fable-5-1": (10.00, 50.00),
    "gpt-5": (1.25, 10.00), "gpt-5-mini": (0.25, 2.00), "gpt-5-nano": (0.05, 0.40), "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
}
# Adaptive-thinking models: thinking tokens count toward max_tokens, so add headroom.
THINKING_HEADROOM = int(os.environ.get("HARNESS_THINKING_HEADROOM", "2500"))
# Anthropic models that think by default (thinking cannot be switched off on Opus 5.5 / Fable, and Sonnet 5 / Opus 5 run
# adaptive thinking when `thinking` is omitted): they get THINKING_HEADROOM like every other reasoning model. Before
# 2026-10-03 only models with an output_config extra got it, so Opus 5.5 spent a 450-token chat budget on thinking and
# posted nothing (3 of 64 turns).
_THINKS_BY_DEFAULT_PREFIXES = ("claude-sonnet-5", "claude-opus-5", "claude-fable", "claude-mythos")
# One retry after an empty reply (no text, no tool call): the identical request with this output-token limit (or twice
# the first limit, if larger), the same for every model. See call_model.
EMPTY_RETRY_TOKENS = int(os.environ.get("HARNESS_EMPTY_RETRY_TOKENS", "16000"))
RETRY_TIMEOUT_S = 900.0
# Frontier models run at the lightest reasoning setting their API offers (user decision, 2026-10-03): Opus 5.5 and
# Fable 5.1 accept effort low/medium/high/xhigh/max (no "minimal"; thinking cannot be disabled), so "low".
MODEL_EXTRAS: dict[str, dict[str, Any]] = {"claude-sonnet-5": {"output_config": {"effort": "medium"}},
                                           "claude-opus-5-5": {"output_config": {"effort": "low"}},
                                           "claude-fable-5-1": {"output_config": {"effort": "low"}},
                                           # models released after the main runs (probed 2026-10-09, lightest setting):
                                           # Sonnet 5.5 turns thinking off with "between_tools" (it rejects "disabled");
                                           # Haiku 5.5 accepts "disabled"
                                           "claude-sonnet-5-5": {"thinking": {"type": "between_tools"}},
                                           "claude-haiku-5-5": {"thinking": {"type": "disabled"}}}
# Responses-API reasoning per model (gpt-6-astra must use the Responses API with tools; it accepts effort low and up,
# not none/minimal, probed 2026-10-03), lightest setting as above.
RESPONSES_REASONING: dict[str, dict[str, Any]] = {"gpt-6-astra": {"effort": "low"},
                                                  # 2026-10-09: GPT-6.1 Sol accepts low and up (not none/minimal), as Astra
                                                  "gpt-6.1-sol": {"effort": "low"}}
# Opt-in override for follow-up studies (2026-10-08, inherited-sessions reasoning arm): HARNESS_FRONTIER_EFFORT=high runs the
# three frontier models at that effort instead of the lightest; HARNESS_THINKING_HEADROOM raises the output headroom for
# thinking to match. Both are recorded in every transcript through run_config(); the benchmark never sets them.
if os.environ.get("HARNESS_FRONTIER_EFFORT"):
    for _m in ("claude-opus-5-5", "claude-fable-5-1"):
        MODEL_EXTRAS[_m] = {"output_config": {"effort": os.environ["HARNESS_FRONTIER_EFFORT"]}}
    RESPONSES_REASONING["gpt-6-astra"] = {"effort": os.environ["HARNESS_FRONTIER_EFFORT"]}
# A new OpenAI model at the lightest reasoning setting its API offers (2026-10-14): HARNESS_OPENAI_EFFORT='{"gpt-6-luna":
# "none"}' sends that reasoning_effort on chat completions and the same effort on the Responses API (used when the model
# rejects function tools with reasoning on chat completions). Without an entry a model runs at its API default, which for
# several recent models is not the lightest (gpt-6-luna and gpt-5.6-terra, probed 2026-10-14: the default is rejected
# with tools on chat completions, so they moved to the Responses API at its default effort; both accept "none").
# Recorded in run_config() when set.
OPENAI_EFFORT: dict[str, str] = json.loads(os.environ.get("HARNESS_OPENAI_EFFORT", "") or "{}")
for _m, _e in OPENAI_EFFORT.items():
    RESPONSES_REASONING[_m] = {"effort": _e}
# Prices for models the PRICES table does not list (USD per 1M tokens, input and output), e.g.
# HARNESS_PRICES='{"gpt-5.5": [5, 30]}'; unlisted models count at (5, 25) in the usage log and the budget guard.
PRICES.update({m: (float(p[0]), float(p[1])) for m, p in json.loads(os.environ.get("HARNESS_PRICES", "") or "{}").items()})
VLLM_ENDPOINTS: dict[str, str] = json.loads(os.environ.get("HARNESS_VLLM_ENDPOINTS", "") or json.dumps({
    "Qwen/Qwen3-32B": "http://localhost:8000/v1", "Qwen/Qwen3-14B": "http://localhost:8001/v1",
    "Qwen/Qwen3-8B": "http://localhost:8002/v1"}))
VLLM_THINKING_HEADROOM = 4000
# Azure AI Foundry (model inference API, OpenAI-compatible). Deployment names listed in AZURE_FOUNDRY_MODELS in .env;
# billed to the Azure subscription, so the cost column logs 0 and the local budget guard does not count them.
AZURE_MODELS = [m for m in os.environ.get("AZURE_FOUNDRY_MODELS", "").split(",") if m.strip()]
# Together AI (OpenAI-compatible, https://api.together.xyz/v1). Model ids listed in TOGETHER_MODELS in .env, or any id with
# a "together:" prefix. Prices are looked up per call from the model list (cached) so the usage log and budget guard see them.
TOGETHER_MODELS = [m for m in os.environ.get("TOGETHER_MODELS", "").split(",") if m.strip()]
TOGETHER_BASE = "https://api.together.xyz/v1"
# Hosted OpenAI-compatible providers (2026-10-14), for testing a new model without code changes. One entry per model name:
#   HARNESS_OPENAI_COMPAT='{"<name>": {"base_url": "https://.../v1", "api_key_env": "<variable holding the key>",
#                                      "model": "<the provider's model id, default the name>",
#                                      "extra": {<request-body fields, e.g. "reasoning_effort": "low">},
#                                      "price": [<USD per 1M input tokens>, <output>]}}'
# An entry takes precedence over every other route, so it can also map a partner name (e.g. "Qwen/Qwen3-14B") to a hosted
# copy. The request is a plain chat completion (messages, tools, max_tokens) plus "extra": none of the vLLM fields
# (chat_template_kwargs) or provider-specific fields of the other routes. Unpriced models count at (5, 25) like other
# unknown models. Two built-in presets, which an entry of the same name overrides key by key (_compat_preset): gemini-*
# (Google's endpoint, GEMINI_API_KEY) and openrouter:<model id> (OPENROUTER_API_KEY), both at the lightest reasoning
# setting the API offers, the leaderboard's rule. Every resolved entry is recorded in run_config() for the run's models.
OPENAI_COMPAT: dict[str, dict[str, Any]] = json.loads(os.environ.get("HARNESS_OPENAI_COMPAT", "") or "{}")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_COMPAT_PRESET_PREFIXES = ("gemini-", "openrouter:")
_compat_resolved: dict[str, dict[str, Any]] = {}
_openrouter_models: dict[str, dict[str, Any]] = {}


def _openrouter_lightest(model_id: str) -> dict[str, Any]:
    """Price and lightest reasoning setting of an OpenRouter model, from its public model list (no key needed; fetched
    once per process). OpenRouter's reasoning docs (read 2026-10-14): `reasoning: {"effort": "none"}` disables reasoning,
    except where `reasoning.mandatory` is true (the model rejects it); `supported_efforts` lists the accepted efforts in
    descending order. So: no field for a model without the reasoning parameter, "none" where reasoning can be turned off,
    else the lowest supported effort (none sent when the model exposes no effort choice)."""
    if not _openrouter_models and not dry_run():
        try:
            import requests
            r = requests.get(f"{OPENROUTER_BASE}/models", timeout=60)
            _openrouter_models.update({m["id"]: m for m in r.json()["data"]})
        except Exception:  # noqa: BLE001
            pass
    m = _openrouter_models.get(model_id)
    if m is None:
        return {"catalog": "not in OpenRouter's model list (or the list was not fetched): the provider's default reasoning"}
    out: dict[str, Any] = {"catalog": "openrouter.ai/api/v1/models"}
    pr = m.get("pricing") or {}
    try:
        out["price"] = [float(pr["prompt"]) * 1e6, float(pr["completion"]) * 1e6]
    except (KeyError, TypeError, ValueError):
        pass
    r = m.get("reasoning") or {}
    if "reasoning" in (m.get("supported_parameters") or []):
        if not r.get("mandatory"):
            out["extra"] = {"reasoning": {"effort": "none"}}
        elif r.get("supported_efforts"):
            out["extra"] = {"reasoning": {"effort": r["supported_efforts"][-1]}}
    return out


def _compat_preset(model: str) -> dict[str, Any]:
    """The built-in settings of a gemini-* or openrouter:<model id> name ({} for any other)."""
    if model.startswith("gemini-"):
        # Google's OpenAI-compatibility page (ai.google.dev/gemini-api/docs/openai, read 2026-10-14): reasoning_effort
        # "minimal" is the lowest thinking level on every Gemini model (a model without a minimal level, e.g. Gemini 4
        # Argon or 3.1 Pro, maps it to low, its lowest; thinking cannot be turned off on Gemini 3 or 2.5 Pro), and "none"
        # turns thinking off on Gemini 2.5 Flash and Flash-Lite
        return {"base_url": GEMINI_BASE, "api_key_env": "GEMINI_API_KEY",
                "extra": {"reasoning_effort": "none" if model.startswith("gemini-2.5-flash") else "minimal"}}
    if model.startswith("openrouter:"):
        mid = model.removeprefix("openrouter:")
        return {"base_url": OPENROUTER_BASE, "api_key_env": "OPENROUTER_API_KEY", "model": mid, **_openrouter_lightest(mid)}
    return {}


def openai_compat_config(model: str) -> dict[str, Any]:
    """The resolved settings of a model on the hosted OpenAI-compatible route: its preset, then its HARNESS_OPENAI_COMPAT
    entry key by key (cached; a dry run never fetches OpenRouter's model list)."""
    if model in _compat_resolved:
        return _compat_resolved[model]
    entry = OPENAI_COMPAT.get(model) or {}
    if not isinstance(entry, dict):
        raise ValueError(f"HARNESS_OPENAI_COMPAT[{model!r}] must be an object with base_url, api_key_env, ...")
    cfg = {**_compat_preset(model), **entry}
    cfg.setdefault("model", model)
    if not cfg.get("base_url"):
        raise ValueError(f"HARNESS_OPENAI_COMPAT[{model!r}] has no base_url")
    if not dry_run():
        _compat_resolved[model] = cfg
    return cfg
# Fixed routes: a model name used throughout the project (run folders, tables) -> (provider, provider's model id). Since
# 2026-09-29 DeepSeek-V4-Flash is served by Together (treated as the same model as the earlier Azure deployment, per the
# authors); routes take precedence over AZURE_FOUNDRY_MODELS / TOGETHER_MODELS and are recorded in run_config().
MODEL_ROUTES: dict[str, tuple[str, str]] = {"DeepSeek-V4-Flash": ("together", "deepseek-ai/DeepSeek-V4-Flash-0731")}
# Request options per routed model. Together serves DeepSeek-V4-Flash with reasoning on by default (final answers ran into the
# token cap and came back empty); the Azure deployment answered without long reasoning (median 62 output tokens on final
# answers), so reasoning is switched off to match it. Recorded in run_config().
ROUTE_EXTRA_BODY: dict[str, dict[str, Any]] = {"DeepSeek-V4-Flash": {"reasoning": {"enabled": False}},
                                                # 2026-09-30: Together streams Qwen3.7-Plus with open-ended reasoning
                                                # (up to 76k tokens, 20+ min per turn, max_tokens not enforced); off, as
                                                # for the other open models (DeepSeek here, Qwen3-32B locally)
                                                "together:Qwen/Qwen3.7-Plus": {"reasoning": {"enabled": False}},
                                                # 2026-10-03 partner seat: GLM-5.3-Flash at its default (medium) effort
                                                # reasoned past the output budget in 24% of its turns and posted nothing
                                                # (ran to 2.9k-6.5k tokens at ~60 tok/s). On Together `reasoning:
                                                # {enabled: false}`, chat_template_kwargs and `thinking: disabled` are
                                                # ignored for it; reasoning_effort "low" works (~80-220 reasoning tokens,
                                                # a reply every time on replayed empty turns)
                                                "together:zai-org/GLM-5.3-Flash": {"reasoning_effort": "low"},
                                                # frontier models at their lightest setting (2026-10-03, probed with a
                                                # tool call): thinking off. DeepSeek-V4-Pro still emitted ~74 reasoning
                                                # characters with reasoning.enabled=false; the chat template switch gave 0
                                                "together:deepseek-ai/DeepSeek-V4-Pro-0813": {"chat_template_kwargs": {"enable_thinking": False}},
                                                "together:Qwen/Qwen3.7-Max": {"reasoning": {"enabled": False}},
                                                "together:moonshotai/Kimi-K3": {"reasoning": {"enabled": False}},
                                                # 2026-10-08 (recent open models, probed with a tool call): DeepSeek-V4.1-
                                                # Flash takes the same chat-template switch as V4-Pro (0 reasoning
                                                # characters, tools still used); GLM-5.3 ignores `reasoning: {enabled:
                                                # false}` and the template switch (~1.4k reasoning characters) but
                                                # reasoning_effort "low" works (~36), as for GLM-5.3-Flash
                                                "together:deepseek-ai/DeepSeek-V4.1-Flash": {"chat_template_kwargs": {"enable_thinking": False}},
                                                "together:zai-org/GLM-5.3": {"reasoning_effort": "low"},
                                                # 2026-10-09: gpt-oss-120b answers with no reasoning text when thinking
                                                # is off in the chat template (probed with a tool call)
                                                "together:openai/gpt-oss-120b": {"chat_template_kwargs": {"enable_thinking": False}}}
# Together models whose chat template renders an assistant tool-call turn with content null, followed by its tool
# results, to an empty prompt (HTTP 400 "prompt: prompt cannot be empty"; gpt-oss-120b, 2026-10-09: reproduced with one
# tool call, and the cause of the jobs that failed when the model called a tool without text). Content "" is accepted.
_NULL_CONTENT_REJECTED = {"together:openai/gpt-oss-120b"}
# Together models whose replies sometimes come back unparsed, in the model's raw output format with the special tokens
# stripped (gpt-oss-120b's harmony format, 2026-10-09: in 58 of its first 442 runs, mostly on the turn after a tool
# result, reproduced with one tool call under every reasoning setting): "analysis<reasoning>assistantcommentary
# to=functions.<tool> json{...}" or "final<reply>". The reasoning then reached the team as the model's message and the
# tool call was lost. _repair_harmony() splits such a reply back into reasoning, reply text and tool calls.
_HARMONY_LEAK = {"together:openai/gpt-oss-120b"}
# a channel name runs straight into its message once the tokens are stripped ("analysisWe ...", "final**Status**"), or
# names its recipient (" to=functions.read_file json{...}"); ordinary prose ("final answer", "finally") does not match
_HARMONY_HEAD = re.compile(r"(?:\A|assistant)(analysis|commentary|final)(?= to=functions\.|[^\sa-z])"
                           r"(?: to=functions\.([A-Za-z0-9_\-]+))?(?:\s*(?:<\|constrain\|>)?\s*(?:json|code)(?=\s*\{))?")


def _repair_harmony(text: str) -> tuple[str, str, list[dict[str, Any]]] | None:
    """(reply text, reasoning, tool calls) from a reply that came back in raw harmony format, or None if it is not one.
    Segments start at the beginning of the reply or at "assistant", each with its channel: analysis is reasoning,
    final (and commentary with no recipient) is reply text, and a segment addressed to=functions.<tool> is a tool call
    whose message is its JSON arguments."""
    if not _HARMONY_HEAD.match(text):
        return None
    heads = list(_HARMONY_HEAD.finditer(text))
    reply, reasoning, calls = [], [], []
    for i, h in enumerate(heads):
        body = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip()
        if h.group(2):
            try:
                args = json.loads(body or "{}")
            except json.JSONDecodeError:
                try:
                    args = json.loads(body[body.find("{"): body.rfind("}") + 1])
                except (json.JSONDecodeError, ValueError):
                    args = {"_raw": body}
            calls.append({"id": f"call_h{uuid.uuid4().hex[:16]}", "name": h.group(2),
                          "input": args if isinstance(args, dict) else {"_raw": body}})
        elif h.group(1) == "analysis":
            reasoning.append(body)
        else:
            reply.append(body)
    return "\n\n".join(x for x in reply if x), "\n\n".join(x for x in reasoning if x), calls
_together_prices: dict[str, tuple[float, float]] = {}
_STREAM_ONLY: set[str] = set()  # models whose provider rejects non-streamed requests (learned on the first call)
# OpenAI models that reject function tools together with reasoning on /v1/chat/completions (2026-10-02: gpt-6-astra says
# "use /v1/responses or set reasoning_effort to 'none'"). They are called through the Responses API with their default
# reasoning instead of switching reasoning off; learned on the first call, recorded per call in the usage log.
_RESPONSES_ONLY: set[str] = set()


def _stream_chat(client: Any, kwargs: dict[str, Any]) -> Any:
    """Run an OpenAI-compatible chat request streamed and rebuild the non-streamed response shape the caller reads
    (message content, reasoning, tool calls, finish reason, usage, id)."""
    from types import SimpleNamespace as NS
    stream = client.chat.completions.create(**kwargs, stream=True, stream_options={"include_usage": True})
    content, reasoning, calls, finish, usage, rid = [], [], {}, None, None, None
    for ch in stream:
        rid = rid or getattr(ch, "id", None)
        if getattr(ch, "usage", None):
            usage = ch.usage
        for c in ch.choices or []:
            d = c.delta
            if getattr(d, "content", None):
                content.append(d.content)
            r = getattr(d, "reasoning_content", None) or getattr(d, "reasoning", None)
            if r:
                reasoning.append(r)
            for tc in getattr(d, "tool_calls", None) or []:
                e = calls.setdefault(tc.index, {"id": None, "name": "", "args": ""})
                e["id"] = tc.id or e["id"]
                if tc.function is not None:
                    e["name"] += tc.function.name or ""
                    e["args"] += tc.function.arguments or ""
            if c.finish_reason:
                finish = c.finish_reason
    tool_calls = [NS(id=e["id"] or f"call_{i}", function=NS(name=e["name"], arguments=e["args"] or "{}"))
                  for i, e in sorted(calls.items())]
    msg = NS(content="".join(content), reasoning_content="".join(reasoning), tool_calls=tool_calls or None)
    return NS(choices=[NS(message=msg, finish_reason=finish)], id=rid,
              usage=usage or NS(prompt_tokens=0, completion_tokens=0))
# Qwen3 "thinking" mode on these A6000s runs ~20 tok/s and spends thousands of tokens per turn, so local
# models run in non-thinking mode unless HARNESS_VLLM_THINKING=1 (recorded in the usage log per call).
VLLM_THINKING_DEFAULT = os.environ.get("HARNESS_VLLM_THINKING", "0") == "1"
USAGE_LOG = Path(os.environ.get("HARNESS_USAGE_LOG", ROOT / "data" / "processed" / "harness_usage.jsonl"))
BUDGET_USD = float(os.environ.get("HARNESS_BUDGET_USD", "10"))
_LOCK = threading.Lock()
_clients: dict[str, Any] = {}


def run_config(models: list[str] | None = None) -> dict[str, Any]:
    """Settings that affect model behaviour, recorded in every transcript for reproducibility. `models` (the run's
    models): those on the hosted OpenAI-compatible route add their resolved settings (`openai_compat`); the key is left
    out when none is, so the record of every other run is unchanged."""
    from collab_intel.harness import judges
    run_models = [*(models or []), judges.JUDGE_MODEL, judges.SECOND_RATER, judges.TIEBREAK_RATER]
    compat = {m: openai_compat_config(m) for m in dict.fromkeys(run_models) if m and _is_compat(m)}
    return {"model_extras": MODEL_EXTRAS, "thinking_headroom": THINKING_HEADROOM,
            "vllm_thinking": VLLM_THINKING_DEFAULT, "vllm_endpoints": VLLM_ENDPOINTS,
            "judge_model": judges.JUDGE_MODEL, "second_rater": judges.SECOND_RATER, "model_routes": MODEL_ROUTES,
            "judge_temperature": judges.JUDGE_TEMPERATURE,
            "judge_temperature_sent": {m: accepts_temperature(m) for m in (judges.JUDGE_MODEL, judges.SECOND_RATER) if m},
            "route_extra_body": ROUTE_EXTRA_BODY, "responses_reasoning": RESPONSES_REASONING, "thinks_by_default_prefixes": list(_THINKS_BY_DEFAULT_PREFIXES),
            "empty_retry_tokens": EMPTY_RETRY_TOKENS, "judge_max_tokens": judges.JUDGE_MAX_TOKENS,
            **({"openai_compat": compat} if compat else {}), **({"openai_effort": OPENAI_EFFORT} if OPENAI_EFFORT else {}),
            # a dry run (HARNESS_DRY_RUN: canned replies, no model call) is marked so that it is never scored or resumed
            # as a real run (release audit USE-01 / DS-10, 2026-10-13); real runs carry no such key
            **({"dry_run": True} if dry_run() else {})}


def dry_run() -> bool:
    """HARNESS_DRY_RUN is set: every call_model returns a canned reply."""
    return bool(os.environ.get("HARNESS_DRY_RUN"))


# Anthropic models that reject sampling parameters (temperature/top_p/top_k return HTTP 400): Sonnet 5, Opus 4.7 and
# later, Opus 5.x, Fable and Mythos. Sonnet 4.6, Haiku 4.5 and Opus 4.6 (the referee and second rater) accept them.
_NO_SAMPLING_PREFIXES = ("claude-sonnet-5", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-fable", "claude-mythos")


def accepts_temperature(model: str) -> bool:
    """Whether a temperature can be sent to this model with the harness's request settings: not to Anthropic models
    that reject sampling parameters or run with an output_config (effort) extra, not to OpenAI reasoning models
    (gpt-5*, o*), which reject a non-default temperature; yes for the rest (older Claude, Together, vLLM, Azure)."""
    try:
        provider = provider_of(model)
    except ValueError:
        return False
    if provider == "anthropic":
        return not model.startswith(_NO_SAMPLING_PREFIXES) and "output_config" not in MODEL_EXTRAS.get(model, {})
    if provider == "openai":
        return not model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))
    return True


class BudgetExceeded(RuntimeError):
    pass


def provider_of(model: str) -> str:
    if model in OPENAI_COMPAT:  # an explicit entry wins over every other route
        return "openai_compat"
    if model in MODEL_ROUTES:
        return MODEL_ROUTES[model][0]
    if model.startswith("claude-"):
        return "anthropic"
    if model in VLLM_ENDPOINTS:
        return "vllm"
    if model in AZURE_MODELS:
        return "azure"
    if model in TOGETHER_MODELS or model.startswith("together:"):
        return "together"
    if model.startswith(_COMPAT_PRESET_PREFIXES):
        return "openai_compat"
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    raise ValueError(f"unknown provider for model {model!r} (claude-*, gpt-*, together:<id>, gemini-*, openrouter:<id>, "
                     "or a name in HARNESS_OPENAI_COMPAT / HARNESS_VLLM_ENDPOINTS; docs/running.md)")


def _is_compat(model: str) -> bool:
    try:
        return provider_of(model) == "openai_compat"
    except ValueError:
        return False


# the environment variables each route reads (vllm: none); see missing_setup()
_ROUTE_KEYS = {"anthropic": ("ANTHROPIC_API_KEY",), "openai": ("OPENAI_API_KEY",), "together": ("TOGETHER_API_KEY",),
               "azure": ("AZURE_FOUNDRY_KEY", "AZURE_FOUNDRY_ENDPOINT"), "vllm": ()}


def missing_setup(model: str) -> str | None:
    """Why `model` cannot be called as configured (no route, a key not set), or None. For a run's up-front check."""
    try:
        provider = provider_of(model)
        keys = (openai_compat_config(model).get("api_key_env"),) if provider == "openai_compat" else _ROUTE_KEYS[provider]
    except ValueError as e:
        return str(e)
    unset = [k for k in keys if k and not os.environ.get(k)]
    return f"{model}: {', '.join(unset)} not set (add it to .env)" if unset else None


def model_slug(model: str) -> str:
    return model.replace("/", "__")


def _client(provider: str, model: str = ""):
    key = provider if provider != "vllm" else f"vllm:{VLLM_ENDPOINTS[model]}"
    if provider == "openai_compat":
        cfg = openai_compat_config(model)
        key = f"openai_compat:{cfg['base_url']}|{cfg.get('api_key_env')}"
        if key not in _clients:
            import openai
            env = cfg.get("api_key_env")
            k = os.environ.get(env) if env else "EMPTY"
            if not k:
                raise RuntimeError(f"{env} not set (add it to .env); it holds the key for {model}")
            _clients[key] = openai.OpenAI(base_url=cfg["base_url"], api_key=k, max_retries=4, timeout=300.0)
        return _clients[key]
    if key not in _clients:
        if provider == "anthropic":
            import anthropic
            k = os.environ.get("ANTHROPIC_API_KEY")
            if not k:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            _clients[key] = anthropic.Anthropic(api_key=k, max_retries=6, timeout=180.0)
        elif provider == "openai":
            import openai
            k = os.environ.get("OPENAI_API_KEY")
            if not k:
                raise RuntimeError("OPENAI_API_KEY not set (add it to .env)")
            _clients[key] = openai.OpenAI(api_key=k, max_retries=6, timeout=180.0)
        elif provider == "together":
            import openai
            k = os.environ.get("TOGETHER_API_KEY")
            if not k:
                raise RuntimeError("TOGETHER_API_KEY not set (add it to .env)")
            _clients[key] = openai.OpenAI(base_url=TOGETHER_BASE, api_key=k, max_retries=4, timeout=300.0)
        elif provider == "azure":
            import openai
            k, ep = os.environ.get("AZURE_FOUNDRY_KEY"), os.environ.get("AZURE_FOUNDRY_ENDPOINT")
            if not (k and ep):
                raise RuntimeError("AZURE_FOUNDRY_KEY / AZURE_FOUNDRY_ENDPOINT not set (add them to .env)")
            _clients[key] = openai.OpenAI(base_url=ep, api_key=k, default_headers={"api-key": k},
                                          default_query={"api-version": os.environ.get("AZURE_FOUNDRY_API_VERSION", "2024-05-01-preview")},
                                          max_retries=4, timeout=300.0)
        else:
            import openai
            _clients[key] = openai.OpenAI(base_url=VLLM_ENDPOINTS[model], api_key=os.environ.get("HARNESS_VLLM_API_KEY") or "EMPTY",
                                          max_retries=3, timeout=600.0)
    return _clients[key]


def api_model_id(model: str) -> str:
    """The id sent to the provider (a routed name maps to the provider's id; a "together:" prefix is dropped; a hosted
    OpenAI-compatible model sends its entry's "model")."""
    if _is_compat(model):
        return openai_compat_config(model)["model"]
    return MODEL_ROUTES[model][1] if model in MODEL_ROUTES else model.removeprefix("together:")


def _together_price(model: str) -> tuple[float, float]:
    mid = api_model_id(model)
    if not _together_prices:
        try:
            import requests
            r = requests.get(f"{TOGETHER_BASE}/models", headers={"Authorization": f"Bearer {os.environ.get('TOGETHER_API_KEY', '')}"}, timeout=60)
            for m in r.json():
                pr = m.get("pricing") or {}
                _together_prices[m["id"]] = (float(pr.get("input", 0) or 0), float(pr.get("output", 0) or 0))
        except Exception:  # noqa: BLE001
            pass
    return _together_prices.get(mid, (1.0, 3.0))


def cost_usd(model: str, usage: dict[str, int]) -> float:
    if provider_of(model) in ("vllm", "azure"):
        return 0.0
    if provider_of(model) == "openai_compat" and openai_compat_config(model).get("price") is not None:
        pin, pout = (float(x) for x in openai_compat_config(model)["price"])
    else:
        pin, pout = _together_price(model) if provider_of(model) == "together" else PRICES.get(model, (5.0, 25.0))
    inp = usage.get("input_tokens", 0)
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    return (inp * pin + cr * pin * 0.1 + cw * pin * 1.25 + usage.get("output_tokens", 0) * pout) / 1e6


_SPENT: dict[str, Any] = {"offset": 0, "total": 0.0, "ino": None}
_SPENT_LOCK = threading.Lock()


def _line_cost(line: str) -> float:
    try:
        return float(json.loads(line).get("cost_usd", 0.0) or 0.0)
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return 0.0


def total_spent() -> float:
    """Estimated spend in the usage log. Incremental (2026-10-09): the log passed 270 MB and every paid call checked the
    budget by re-reading all of it; now each process adds only the complete lines written since its last call (the same
    per-line rule as before: cost_usd of each JSON line, lines that do not parse count 0)."""
    if not USAGE_LOG.exists():
        return 0.0
    with _SPENT_LOCK:
        st = USAGE_LOG.stat()
        if _SPENT["ino"] != st.st_ino or st.st_size < _SPENT["offset"]:  # a new or truncated log: start over
            _SPENT.update(offset=0, total=0.0, ino=st.st_ino)
        if st.st_size > _SPENT["offset"]:
            with USAGE_LOG.open("rb") as fh:
                fh.seek(_SPENT["offset"])
                chunk = fh.read()
            end = chunk.rfind(b"\n")
            if end >= 0:
                for line in chunk[:end].decode("utf-8", errors="replace").split("\n"):
                    if '"cost_usd"' in line:
                        _SPENT["total"] += _line_cost(line)
                _SPENT["offset"] += end + 1
        return _SPENT["total"]


def _log(entry: dict[str, Any]) -> None:
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, USAGE_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# -- provider adapters ---------------------------------------------------------------------------
def _to_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no content)"}]})
        elif m["role"] == "tool_results":
            out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
                                                    for r in m["results"]]})
    return out


def _to_openai(system: str, messages: list[dict[str, Any]], echo: bool = False) -> list[dict[str, Any]]:
    """`echo` (the hosted OpenAI-compatible route only): send back what the provider attached to its own reply for the next
    request, `provider_echo` on an assistant message and `extra_content` on a tool call (_compat_echo)."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{"id": tc["id"], "type": "function",
                                      "function": {"name": tc["name"], "arguments": json.dumps(tc["input"])},
                                      **({"extra_content": tc["extra_content"]} if echo and tc.get("extra_content") else {})}
                                     for tc in m["tool_calls"]]
                if echo and m["tool_calls"][0].get("provider_echo"):
                    msg.update(m["tool_calls"][0]["provider_echo"])
            out.append(msg)
        elif m["role"] == "tool_results":
            for r in m["results"]:
                out.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
    return out


# Set while the one empty-reply retry runs: a timeout there is final (the retry asks for a long reply under a long
# timeout; re-sending it up to 9 times could hold one turn for hours), rate limits are still waited out.
_IN_EMPTY_RETRY: contextvars.ContextVar[bool] = contextvars.ContextVar("_IN_EMPTY_RETRY", default=False)


def _with_backoff(fn: Any, tries: int = 9) -> Any:
    """Retry provider rate limits (429) and overloads (503/529) with long exponential backoff (about 10 minutes in
    total). The client's own retries are short; Together's limits are dynamic and shift with capacity, and with the
    partner seats on Together (2026-10-02) one 429 must not end a whole run."""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            code = getattr(e, "status_code", None)
            msg = str(e)
            # dropped connections and timeouts are transient too (2026-10-03: 33 of 45 failed v2 jobs were
            # APIConnectionError / APITimeoutError from the SDKs, which have no status code)
            transient = code in (429, 500, 502, 503, 504, 529) or "Too many requests" in msg or "overloaded" in msg.lower() \
                or type(e).__name__ in ("APIConnectionError", "APITimeoutError")
            if not transient or i == tries - 1 or (_IN_EMPTY_RETRY.get() and type(e).__name__ == "APITimeoutError"):
                raise
            time.sleep(min(150, 5 * 2 ** i))
    return None


def _to_responses(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The harness conversation as Responses API input items (stateless: the whole history every call)."""
    items: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            items.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
            for tc in m.get("tool_calls") or []:
                items.append({"type": "function_call", "call_id": tc["id"], "name": tc["name"],
                              "arguments": json.dumps(tc["input"])})
        elif m["role"] == "tool_results":
            for r in m["results"]:
                items.append({"type": "function_call_output", "call_id": r["id"], "output": r["content"]})
    return items


def _responses_call(client: Any, model: str, system: str, messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]] | None, max_tokens: int) -> tuple[str, list[dict[str, Any]], str, dict[str, int], Any]:
    kwargs: dict[str, Any] = {"model": api_model_id(model), "instructions": system, "input": _to_responses(messages),
                              "max_output_tokens": max_tokens}
    if tools:
        kwargs["tools"] = [{"type": "function", "name": t["name"], "description": t.get("description", ""),
                            "parameters": t["input_schema"]} for t in tools]
    if model in RESPONSES_REASONING:
        kwargs["reasoning"] = RESPONSES_REASONING[model]
    resp = _with_backoff(lambda: client.responses.create(**kwargs))
    tool_calls: list[dict[str, Any]] = []
    for it in resp.output or []:
        if getattr(it, "type", "") == "function_call":
            raw = it.arguments or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"_raw": raw}
            tool_calls.append({"id": it.call_id, "name": it.name, "input": args})
    text = (getattr(resp, "output_text", "") or "").strip()
    u = resp.usage
    usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0}
    stop = "tool_calls" if tool_calls else (getattr(resp, "status", None) or "stop")
    return text, tool_calls, stop, usage, resp


def _compat_echo(message: Any, tool_calls: list[dict[str, Any]]) -> None:
    """Hosted OpenAI-compatible route: keep what a provider attaches to a reply for the model's next request in the same
    turn (sent back by _to_openai with echo=True). Gemini 3 puts a thought signature on its tool calls
    (tool_calls[].extra_content.google.thought_signature) and answers the next request with HTTP 400 when it is missing;
    OpenRouter returns reasoning_details on the message, to be sent back with tool calls. The message-level fields ride on
    the first tool call (`provider_echo`), since only a reply with tool calls is followed by another request in its turn.
    A tool call without an id gets one, so its result can name it."""
    for tc in tool_calls:
        tc["id"] = tc["id"] or f"call_{uuid.uuid4().hex[:16]}"
    raw = getattr(message, "tool_calls", None) or []
    for tc, r in zip(tool_calls, raw):
        ec = (getattr(r, "model_extra", None) or {}).get("extra_content")
        if ec:
            tc["extra_content"] = ec
    rd = (getattr(message, "model_extra", None) or {}).get("reasoning_details")
    if rd and tool_calls:
        tool_calls[0]["provider_echo"] = {"reasoning_details": rd}


def _tools_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                               "parameters": t["input_schema"]}} for t in tools]


def _dry_run_reply(messages: list[dict], tools: list[dict] | None) -> dict[str, Any]:
    last = messages[-1].get("content", "") if messages else ""
    if isinstance(last, str) and "\n\nLabels:\n" in last and '"label"' in last:
        # a rater prompt (judges.judge_label): "other", the label a dry run has always ended up with (before 2026-10-13 the
        # judge prompt matched the final-answer branch below, gave no label and fell back to "other"); a menu without
        # "other" gets no menu label, so the run records an unparsed referee
        text = '{"label": "other", "confidence": 0.5, "evidence": "dry run"}'
    elif isinstance(last, str) and ("Reply with JSON" in last or "final answer" in last.lower()):
        text = '{"answer": "DRY_RUN", "reasoning": "dry run"}'
    elif "label" in str(last).lower() and "JSON" in str(last):
        text = '{"label": "other", "confidence": 0.5, "evidence": "dry run"}'
    elif tools:
        tc = {"id": "toolu_dry", "name": "submit", "input": {"note": "dry run"}}
        return {"text": "", "tool_calls": [tc], "stop_reason": "tool_use", "usage": {}, "cost_usd": 0.0,
                "model": "dry-run", "reasoning": ""}
    else:
        text = "[dry-run reply] I have read the channel and my private notes; here is my contribution."
    return {"text": text, "tool_calls": [], "stop_reason": "end_turn", "usage": {}, "cost_usd": 0.0,
            "model": "dry-run", "reasoning": ""}


def _anthropic_headroom(model: str) -> int:
    """Thinking headroom added to max_tokens for Anthropic models that think by default (their thinking tokens count
    toward max_tokens) or that run with an effort extra."""
    return THINKING_HEADROOM if (model.startswith(_THINKS_BY_DEFAULT_PREFIXES) or "output_config" in MODEL_EXTRAS.get(model, {})) else 0


def output_budget(model: str, max_tokens: int, thinking: bool = True) -> int:
    """The output-token limit actually sent to the provider for a visible budget of `max_tokens`."""
    provider = provider_of(model)
    if provider == "anthropic":
        return max_tokens + _anthropic_headroom(model)
    if provider == "vllm":
        return max_tokens + (VLLM_THINKING_HEADROOM if (bool(thinking) and VLLM_THINKING_DEFAULT) else 0)
    return max_tokens + THINKING_HEADROOM


def empty_retry_budget(first_budget: int) -> int:
    """The output-token limit of the one retry after an empty reply: the same rule for every model."""
    return max(EMPTY_RETRY_TOKENS, 2 * first_budget)


def _with_timeout(client: Any, timeout: float | None) -> Any:
    return client.with_options(timeout=timeout) if timeout and hasattr(client, "with_options") else client


def _call_once(model: str, provider: str, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
               budget: int, thinking: bool, temperature: float | None, timeout: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """One provider request with output-token limit `budget`. Returns (result, extra usage-log fields)."""
    extra_log: dict[str, Any] = {}
    refusal = None
    if provider == "anthropic":
        extras = MODEL_EXTRAS.get(model, {})
        kwargs: dict[str, Any] = {"model": model, "max_tokens": budget, "system": system, "messages": _to_anthropic(messages)}
        kwargs.update(extras)
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            # anthropic SDK 1.8 dropped the sampling arguments from Messages.create (a TypeError, found 2026-10-03), so
            # the value goes in the request body; if the API refuses it, the call is retried without it and the usage
            # log records that no temperature was sent
            kwargs["extra_body"] = {**kwargs.get("extra_body", {}), "temperature": temperature}
        client = _with_timeout(_client(provider), timeout)
        try:
            resp = _with_backoff(lambda: client.messages.create(**kwargs))
        except Exception as e:
            if temperature is None or getattr(e, "status_code", None) != 400 or "temperature" not in str(e):
                raise
            kwargs["extra_body"].pop("temperature", None)
            temperature = None
            resp = _with_backoff(lambda: client.messages.create(**kwargs))
        usage = {k: v for k, v in resp.usage.model_dump().items() if isinstance(v, int)}
        text_parts, tool_calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
        text = "\n".join(text_parts).strip()
        stop, reasoning = resp.stop_reason, ""
        if stop == "refusal":
            # the API's safety classifier declined the request (HTTP 200, no content, zero output tokens)
            sd = getattr(resp, "stop_details", None)
            refusal = {"category": getattr(sd, "category", None)}
        req_id = getattr(resp, "_request_id", None)
    else:
        client = _with_timeout(_client(provider, model), timeout)
        kwargs = {"model": api_model_id(model), "messages": _to_openai(system, messages, echo=provider == "openai_compat")}
        if tools:
            kwargs["tools"] = _tools_openai(tools)
        if provider == "vllm":
            think = bool(thinking) and VLLM_THINKING_DEFAULT
            kwargs["max_tokens"] = budget
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": think}}
        elif provider in ("azure", "together"):
            kwargs["max_tokens"] = budget
            if model in ROUTE_EXTRA_BODY:
                kwargs["extra_body"] = ROUTE_EXTRA_BODY[model]
                extra_log["extra_body"] = ROUTE_EXTRA_BODY[model]
            if model in _NULL_CONTENT_REJECTED:
                for msg in kwargs["messages"]:
                    if msg["role"] == "assistant" and msg.get("content") is None:
                        msg["content"] = ""
        elif provider == "openai_compat":
            kwargs["max_tokens"] = budget
            cfg = openai_compat_config(model)
            if cfg.get("extra"):
                kwargs["extra_body"] = cfg["extra"]
                extra_log["extra_body"] = cfg["extra"]
        else:
            kwargs["max_completion_tokens"] = budget
            if model in OPENAI_EFFORT:
                kwargs["reasoning_effort"] = OPENAI_EFFORT[model]
                extra_log["reasoning_effort"] = OPENAI_EFFORT[model]
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = None
        try:
            if model not in _RESPONSES_ONLY:
                resp = _with_backoff(lambda: _stream_chat(client, kwargs) if model in _STREAM_ONLY
                                     else client.chat.completions.create(**kwargs))
        except Exception as e:
            if provider == "openai" and "/v1/responses" in str(e):
                _RESPONSES_ONLY.add(model)
            elif "streaming_required" in str(e):
                # some Together deployments (e.g. Qwen3.7-Plus) only answer streamed requests: same request, streamed
                _STREAM_ONLY.add(model)
                resp = _stream_chat(client, kwargs)
            # Azure's content filter returns HTTP 400 with finish_reason "content_filter"; record an empty turn
            # (flagged in the transcript and usage log) instead of losing the whole run
            elif provider == "azure" and "content_filter" in str(e):
                return ({"text": "(no message: provider content filter)", "tool_calls": [], "stop_reason": "content_filter",
                         "usage": {"input_tokens": 0, "output_tokens": 0}, "cost_usd": 0.0, "model": model, "reasoning": ""},
                        {"content_filter": True})
            else:
                raise
        if model in _RESPONSES_ONLY:
            text, tool_calls, stop, usage, resp = _responses_call(client, model, system, messages, tools, budget)
            extra_log["api"] = "responses"
            reasoning, req_id = "", getattr(resp, "id", None)
        else:
            m = resp.choices[0].message
            text = (m.content or "").strip()
            reasoning = getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or ""
            if not isinstance(reasoning, str):
                reasoning = ""
            if resp.choices[0].finish_reason == "content_filter" and not text:
                text = "(no message: provider content filter)"
            tool_calls = []
            for tc in (m.tool_calls or []):
                raw_args = tc.function.arguments or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    try:  # some deployments append junk after the object (e.g. '{}""'): keep up to the last brace
                        args = json.loads(raw_args[: raw_args.rfind("}") + 1])
                    except (json.JSONDecodeError, ValueError):
                        args = {"_raw": raw_args}
                tool_calls.append({"id": tc.id, "name": tc.function.name, "input": args})
            if provider == "openai_compat":
                _compat_echo(m, tool_calls)
            if model in _HARMONY_LEAK and not tool_calls and (fixed := _repair_harmony(text)) is not None:
                text, leaked, tool_calls = fixed
                reasoning = "\n\n".join(x for x in (reasoning, leaked) if x)
                extra_log["harmony_repaired"] = {"tool_calls": len(tool_calls), "text": bool(text)}
            stop = resp.choices[0].finish_reason
            u = resp.usage
            usage = {"input_tokens": getattr(u, "prompt_tokens", 0) or 0, "output_tokens": getattr(u, "completion_tokens", 0) or 0}
            if provider == "openai_compat":
                # some providers leave thinking tokens out of completion_tokens but count them in total_tokens (and bill
                # them as output): the larger of the two is logged as output
                usage["output_tokens"] = max(usage["output_tokens"], (getattr(u, "total_tokens", 0) or 0) - usage["input_tokens"])
            req_id = getattr(resp, "id", None)
    c = cost_usd(model, usage)
    out = {"text": text, "tool_calls": tool_calls, "stop_reason": stop, "usage": usage, "cost_usd": c, "model": model,
           "reasoning": reasoning}
    if refusal is not None:
        out["refusal"] = refusal
        extra_log["refusal"] = refusal
    extra_log.update({"temperature": temperature, "request_id": req_id})
    return out, extra_log


# Stop reasons after which an empty reply is not retried: the provider's safety layer declined the request, and the
# same request would be declined again (seen 2026-10-03: Claude Sonnet 5 'bio' and Claude Fable 5.1 'cyber' refusals,
# reproduced on replay).
_NO_RETRY_STOPS = ("refusal", "content_filter")


def call_model(model: str, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
               max_tokens: int = 1500, tag: str = "", thinking: bool = True, temperature: float | None = None) -> dict[str, Any]:
    """`temperature` is sent only where the model accepts it (accepts_temperature); otherwise it is dropped silently
    and the provider default applies (the usage log records what was sent).

    Empty replies (2026-10-03: reasoning models spent the whole output budget on reasoning and said nothing; GLM-5.3-Flash
    as a partner in 24% of its turns, GPT-5 in 10%): when a reply has neither text nor a tool call, the IDENTICAL request
    (same messages, tools and settings) is sent once more with a larger output budget, empty_retry_budget(), by the same
    rule for every model. Not after a provider refusal or content filter (_NO_RETRY_STOPS). The first attempt is logged
    as its own usage-log line (tag + '/empty-first'), and the returned dict carries `meta` describing what happened
    ({"empty_retry": {...}} and/or {"refusal": {...}}), which the episode records in the transcript."""
    if temperature is not None and not accepts_temperature(model):
        temperature = None
    if os.environ.get("HARNESS_DRY_RUN"):
        # canned reply, no provider and no cost: nothing goes to the usage log (USE-01, 2026-10-13: dry runs, the test
        # suite's included, filled the project's log with zero-cost lines)
        return _dry_run_reply(messages, tools)
    provider = provider_of(model)
    if provider not in ("vllm", "azure") and total_spent() > BUDGET_USD:
        raise BudgetExceeded(f"estimated spend {total_spent():.2f} USD exceeds HARNESS_BUDGET_USD={BUDGET_USD}")
    vllm_think = (bool(thinking) and VLLM_THINKING_DEFAULT) if provider == "vllm" else None
    budget = output_budget(model, max_tokens, thinking)
    t0 = time.time()
    out, extra = _call_once(model, provider, system, messages, tools, budget, thinking, temperature)
    meta: dict[str, Any] = {}
    first_cost = 0.0
    if not out["text"] and not out["tool_calls"] and out["stop_reason"] not in _NO_RETRY_STOPS:
        first, first_cost = out, out["cost_usd"]
        _log({"ts": t0, "model": model, "provider": provider, "tag": tag + "/empty-first", "usage": first["usage"],
              "cost_usd": round(first["cost_usd"], 6), "max_tokens": budget, "stop_reason": first["stop_reason"],
              "vllm_thinking": vllm_think, "latency_s": round(time.time() - t0, 2), **extra})
        rbudget = empty_retry_budget(budget)
        info: dict[str, Any] = {"first_stop_reason": first["stop_reason"], "first_output_tokens": first["usage"].get("output_tokens", 0),
                                "first_max_tokens": budget, "retry_max_tokens": rbudget}
        t1 = time.time()
        token = _IN_EMPTY_RETRY.set(True)
        try:
            out, extra = _call_once(model, provider, system, messages, tools, rbudget, thinking, temperature, timeout=RETRY_TIMEOUT_S)
            info["recovered"] = bool(out["text"] or out["tool_calls"])
            info["retry_stop_reason"] = out["stop_reason"]
            out = {**out, "cost_usd": out["cost_usd"] + first["cost_usd"]}
            t0, budget = t1, rbudget
        except Exception as e:  # noqa: BLE001 - e.g. a local server whose context cannot hold the larger budget
            info.update({"recovered": False, "retry_error": f"{type(e).__name__}: {str(e)[:300]}"})
            out = dict(first)  # already in the usage log (tag + '/empty-first')
            meta["empty_retry"] = info
            out["meta"] = meta
            return out
        finally:
            _IN_EMPTY_RETRY.reset(token)
        meta["empty_retry"] = info
        extra["empty_retry"] = True
    if out.get("refusal"):
        meta["refusal"] = out["refusal"]
    log_cost = out["cost_usd"] - first_cost  # the first attempt of a retried call has its own log line
    _log({"ts": t0, "model": model, "provider": provider, "tag": tag, "usage": out["usage"], "cost_usd": round(log_cost, 6),
          "max_tokens": budget, "stop_reason": out["stop_reason"], "vllm_thinking": vllm_think,
          "latency_s": round(time.time() - t0, 2), **extra})
    if meta:
        out["meta"] = meta
    return out
