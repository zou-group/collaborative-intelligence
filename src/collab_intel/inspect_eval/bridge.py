"""The harness provider for Inspect models: `call_model` on an "inspect/<model>" or "inspect-role/<role>" name.

The episode code (harness/episode.py) runs unchanged in a worker thread that the Inspect solver starts with
`anyio.to_thread.run_sync`; the thread inherits the sample's context (Inspect's transcript, limits, active model). When a
seat's model name starts with one of llm.INSPECT_PREFIXES, llm._call_once hands the request to `inspect_call` here,
which converts the harness's neutral messages and tools to Inspect's ChatMessage / ToolInfo types, runs
`Model.generate` on the eval's event loop (`anyio.from_thread.run`), and converts the ModelOutput back to the harness's
result dict. So every call of the evaluated model is an ordinary Inspect model call: it is logged as a ModelEvent in the
sample's transcript, counts toward Inspect's usage and limits, and is retried by Inspect on rate limits. The harness
still applies its own rules around it (the empty-reply retry, the usage log and HARNESS_BUDGET_USD), as for every route.

  inspect/<name>       the model under evaluation (the `Bridge` the solver installs for the run); <name> is Inspect's
                       model name, e.g. inspect/openai/gpt-5-mini
  inspect-role/<role>  an Inspect model role (`--model-role partner_1=...`), resolved with get_model(role=...)

Same request as the harness's own route for the same model, where Inspect allows it:
  - the output budget (llm.output_budget on the equivalent harness name, e.g. gpt-5-mini for openai/gpt-5-mini);
  - the reasoning setting the leaderboard used for that model (llm.MODEL_EXTRAS, OPENAI_EFFORT / RESPONSES_REASONING,
    ROUTE_EXTRA_BODY, the gemini-/openrouter: presets), unless the eval sets its own (`--reasoning-effort`, `--effort`,
    `--reasoning-tokens` or `extra_body`) or the task's `leaderboard_reasoning` is false;
  - temperature only where the harness would send it (the judges' 0, if a judge is an Inspect role);
  - the price (llm.cost_usd on the equivalent name; Inspect's own cost, else 5 / 25 USD per million tokens, otherwise).
Every request to an OpenRouter model carries the harness's OpenRouter data policy (llm.OPENROUTER_PROVIDER_POLICY).
One deliberate difference: within a turn, a tool-calling reply is sent back as the provider returned it (Inspect's
ChatMessageAssistant, reasoning blocks included), as Inspect's own agents do; the harness's routes resend only its text
and tool calls. Providers that require their reasoning back (OpenAI's Responses API, Gemini) need this.
"""
from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass, field
from typing import Any

from collab_intel.harness import llm

INSPECT_PREFIX, ROLE_PREFIX = llm.INSPECT_PREFIXES
# Inspect provider -> how its model names map to the harness's names (None: no harness route for that provider)
_HARNESS_NAME = {"openai": "{}", "anthropic": "{}", "google": "{}", "openrouter": "openrouter:{}", "together": "together:{}",
                 "vllm": "{}", "collab_intel": "{}"}


@dataclass
class Bridge:
    """One episode's link to Inspect: the model under evaluation and the eval's roles, used from the worker thread."""

    model: Any  # inspect_ai.model.Model: the model under evaluation
    name: str  # its harness name, INSPECT_PREFIX + str(model)
    leaderboard_reasoning: bool = True
    # tool call id -> the provider's ChatMessageAssistant that made it, sent back as it was (module docstring)
    replies: dict[str, Any] = field(default_factory=dict)
    # what the bridge sent, per model: the reasoning setting applied (recorded in the run's `inspect` block)
    applied: dict[str, dict[str, Any]] = field(default_factory=dict)


CURRENT: contextvars.ContextVar[Bridge | None] = contextvars.ContextVar("collab_intel_inspect_bridge", default=None)
# The harness's own request for a call the bridge makes to a collab_intel/<name> model (harness_model.py): that
# provider sends exactly it, so the request is the one scripts/run_benchmark.py would make for <name>.
PENDING: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("collab_intel_pending_request", default=None)


def inspect_name(model: Any) -> str:
    return str(model)


def harness_equivalent(name: str) -> str | None:
    """The harness's own name for the same model ("inspect/openai/gpt-5-mini" -> "gpt-5-mini"), or None when the harness
    has no route for it (mockllm, a role, an unknown provider). For a role the name is resolved by the caller."""
    if name.startswith(ROLE_PREFIX):
        return None
    rest = name.removeprefix(INSPECT_PREFIX)
    provider, _, model_id = rest.partition("/")
    fmt = _HARNESS_NAME.get(provider)
    if fmt is None or not model_id:
        return None
    same = fmt.format(model_id)
    if provider == "collab_intel":  # a harness model by its own name (harness_model.py), on whatever route it has
        if same.startswith(llm.INSPECT_PREFIXES):
            return None
        try:
            llm.provider_of(same)
        except ValueError:
            return None
        return same
    if provider == "vllm" and same not in llm.VLLM_ENDPOINTS:
        return None
    try:
        p = llm.provider_of(same)
    except ValueError:
        return None
    # the name must land on the same provider (e.g. openai/<id> on the harness's OpenAI route, not a compat entry)
    want = {"openai": ("openai",), "anthropic": ("anthropic",), "google": ("openai_compat",), "openrouter": ("openai_compat",),
            "together": ("together",), "vllm": ("vllm",)}[provider]
    return same if p in want else None


def leaderboard_settings(same: str | None) -> dict[str, Any]:
    """GenerateConfig fields for the reasoning setting the harness sends for this model (empty when it sends none)."""
    if not same:
        return {}
    p = llm.provider_of(same)
    if p == "anthropic":
        extras = dict(llm.MODEL_EXTRAS.get(same) or {})
        out: dict[str, Any] = {}
        effort = (extras.pop("output_config", None) or {}).get("effort")
        if effort:
            out["effort"] = effort
        if extras:
            out["extra_body"] = extras
        return out
    if p == "openai":
        effort = llm.OPENAI_EFFORT.get(same) or (llm.RESPONSES_REASONING.get(same) or {}).get("effort")
        return {"reasoning_effort": effort} if effort else {}
    if p == "together":
        x = llm.ROUTE_EXTRA_BODY.get(same)
        return {"extra_body": dict(x)} if x else {}
    if p == "openai_compat":
        extra = dict(llm.openai_compat_config(same).get("extra") or {})
        if same.startswith("gemini-") and "reasoning_effort" in extra:  # Inspect's google provider maps it natively
            return {"reasoning_effort": extra["reasoning_effort"]}
        return {"extra_body": extra} if extra else {}
    if p == "vllm":
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": llm.VLLM_THINKING_DEFAULT}}}
    return {}


def _eval_config(model: Any) -> Any:
    """The generation config the eval itself gives this model (its own config, plus the eval's for the model under
    evaluation), before the bridge adds its per-call fields."""
    from inspect_ai.model._generate_config import active_generate_config
    from inspect_ai.model._model import active_model

    return model.config.merge(active_generate_config()) if model is active_model() else model.config


def _user_sets_reasoning(model: Any) -> bool:
    """The eval configures reasoning itself for this model (CLI / task / model config): the bridge then adds nothing."""
    cfg = _eval_config(model)
    return any(getattr(cfg, k, None) is not None for k in ("reasoning_effort", "effort", "reasoning_tokens", "extra_body"))


def with_openrouter_policy(model: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """The harness's standing OpenRouter data policy (llm.OPENROUTER_PROVIDER_POLICY: zero-data-retention hosts that
    neither store nor train on the data) on every request to an OpenRouter model, merged into the request's extra_body
    (or the eval's own) as the harness's OpenRouter route does: an entry's own provider fields are kept, these win."""
    if not inspect_name(model).startswith("openrouter/"):
        return cfg
    extra = dict(cfg.get("extra_body") or _eval_config(model).extra_body or {})
    extra["provider"] = {**(extra.get("provider") or {}), **llm.OPENROUTER_PROVIDER_POLICY}
    return {**cfg, "extra_body": extra}


def to_inspect_messages(system: str, messages: list[dict[str, Any]], replies: dict[str, Any]) -> list[Any]:
    """The harness's neutral conversation (llm.py docstring) as Inspect ChatMessages."""
    from inspect_ai.model import ChatMessageAssistant, ChatMessageSystem, ChatMessageTool, ChatMessageUser
    from inspect_ai.tool import ToolCall

    out: list[Any] = [ChatMessageSystem(content=system)]
    names: dict[str, str] = {}
    for m in messages:
        if m["role"] == "user":
            out.append(ChatMessageUser(content=m["content"]))
        elif m["role"] == "assistant":
            calls = m.get("tool_calls") or []
            for tc in calls:
                names[tc["id"]] = tc["name"]
            sent = replies.get(calls[0]["id"]) if calls else None
            if sent is not None:
                out.append(sent)
            else:
                out.append(ChatMessageAssistant(
                    content=m.get("content") or "",
                    tool_calls=[ToolCall(id=tc["id"], function=tc["name"], arguments=tc["input"]) for tc in calls] or None))
        elif m["role"] == "tool_results":
            for r in m["results"]:
                out.append(ChatMessageTool(content=r["content"], tool_call_id=r["id"], function=names.get(r["id"])))
    return out


def to_inspect_tools(tools: list[dict[str, Any]] | None) -> list[Any]:
    from inspect_ai.tool import ToolInfo, ToolParams

    return [ToolInfo(name=t["name"], description=t.get("description", ""),
                     parameters=ToolParams.model_validate(t["input_schema"])) for t in tools or []]


def _cost(name: str, same: str | None, model: Any, usage: dict[str, int], inspect_cost: float | None) -> float:
    """USD for one call: a HARNESS_PRICES entry for the inspect/ name, else the harness's price for the same model, else
    Inspect's own cost, else the harness's default for an unknown model (5 / 25 USD per million tokens); mockllm is free."""
    if name not in llm.PRICES and same:
        return llm.cost_usd(same, usage)
    if str(model).startswith("mockllm/"):
        return 0.0
    if name not in llm.PRICES and inspect_cost is not None:
        return float(inspect_cost)
    pin, pout = llm.PRICES.get(name, (5.0, 25.0))
    return (usage.get("input_tokens", 0) * pin + usage.get("cache_read_input_tokens", 0) * pin * 0.1
            + usage.get("cache_creation_input_tokens", 0) * pin * 1.25 + usage.get("output_tokens", 0) * pout) / 1e6


def inspect_call(name: str, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                 budget: int, temperature: float | None, timeout: float | None = None,
                 thinking: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    """llm._call_once for the "inspect" provider: one Inspect generate call. Returns (result, extra usage-log fields)."""
    import anyio.from_thread
    from inspect_ai.model import ContentReasoning, GenerateConfig, get_model

    bridge = CURRENT.get()
    if bridge is None:
        raise RuntimeError(f"{name}: an Inspect model can only be called inside the Inspect task "
                           "(collab_intel.inspect_eval: inspect eval collab_intel/collaborative_intelligence ...)")
    role = name.removeprefix(ROLE_PREFIX) if name.startswith(ROLE_PREFIX) else None

    async def generate() -> tuple[Any, Any, dict[str, Any]]:
        model = get_model(role=role, required=True) if role else bridge.model
        same = harness_equivalent(INSPECT_PREFIX + inspect_name(model))
        if inspect_name(model).startswith("collab_intel/"):  # the harness's own route: it sends the harness's request
            token = PENDING.set({"model": same, "system": system, "messages": messages, "tools": tools, "budget": budget,
                                 "thinking": thinking, "temperature": temperature, "timeout": timeout})
            try:
                output = await model.generate(to_inspect_messages(system, messages, bridge.replies),
                                              tools=to_inspect_tools(tools), config=GenerateConfig(max_tokens=budget))
            finally:
                PENDING.reset(token)
            return model, output, {"same": same, "applied": {}, "temperature": temperature}
        # the output budget call_model computed (llm.output_budget: for the evaluated model, as the harness's own route for
        # the same model; for a role, whose model is only known here, the visible budget plus THINKING_HEADROOM)
        cfg: dict[str, Any] = {"max_tokens": budget}
        if temperature is not None and (same is None or llm.accepts_temperature(same)):
            cfg["temperature"] = temperature
        if timeout:
            cfg["timeout"] = int(timeout)
        applied: dict[str, Any] = {}
        if bridge.leaderboard_reasoning and not _user_sets_reasoning(model):
            applied = leaderboard_settings(same)
            cfg.update(applied)
        cfg = with_openrouter_policy(model, cfg)
        output = await model.generate(to_inspect_messages(system, messages, bridge.replies),
                                      tools=to_inspect_tools(tools), config=GenerateConfig(**cfg))
        return model, output, {"same": same, "applied": applied, "temperature": cfg.get("temperature")}

    model, output, info = anyio.from_thread.run(generate)
    msg = output.message
    text = (msg.text or "").strip()
    reasoning = "\n\n".join(c.reasoning for c in msg.content if isinstance(c, ContentReasoning)) \
        if isinstance(msg.content, list) else ""
    tool_calls = []
    for tc in msg.tool_calls or []:
        args = tc.arguments if isinstance(tc.arguments, dict) else {"_raw": str(tc.arguments)}
        if tc.parse_error and not args:
            args = {"_raw": tc.parse_error}
        tool_calls.append({"id": tc.id or f"call_{uuid.uuid4().hex[:16]}", "name": tc.function, "input": args})
    if tool_calls:
        bridge.replies[tool_calls[0]["id"]] = msg
    u = output.usage
    usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0,
             "cache_read_input_tokens": getattr(u, "input_tokens_cache_read", 0) or 0,
             "cache_creation_input_tokens": getattr(u, "input_tokens_cache_write", 0) or 0}
    stop = output.stop_reason
    out: dict[str, Any] = {"text": text, "tool_calls": tool_calls, "stop_reason": stop, "usage": usage,
                           "cost_usd": _cost(name, info["same"], model, usage, getattr(u, "total_cost", None)),
                           "model": name, "reasoning": reasoning}
    extra: dict[str, Any] = {"temperature": info["temperature"],
                             "request_id": getattr(output, "response_id", None), "inspect_model": inspect_name(model),
                             **({"inspect_settings": info["applied"]} if info["applied"] else {})}
    harness = (output.metadata or {}).get("harness")
    if harness is not None:  # a collab_intel model: the harness's own stop reason, cost, refusal and usage-log fields
        out.update({"stop_reason": harness.get("stop_reason") or stop, "cost_usd": float(harness.get("cost_usd") or 0.0)})
        extra = {**(harness.get("log") or {}), "inspect_model": inspect_name(model)}
        if harness.get("refusal"):
            out["refusal"] = extra["refusal"] = harness["refusal"]
        return out, extra
    if stop == "content_filter" and not text and not tool_calls:
        # the provider's safety layer declined (Inspect's stop reason for Anthropic's "refusal" and content filters): a
        # provider refusal, as the harness's Anthropic route records it (scripts/score.py leaves the scenario out)
        out["refusal"] = {"category": "content_filter"}
        extra["refusal"] = out["refusal"]
    bridge.applied.setdefault(inspect_name(model), info["applied"])
    return out, extra
