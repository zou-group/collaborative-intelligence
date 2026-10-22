"""`collab_intel/<harness model name>`: an Inspect model whose requests the harness's own route makes.

    get_model("collab_intel/claude-sonnet-4-6")      # the referee, exactly as the leaderboard called it
    inspect eval ... --model collab_intel/claude-opus-5-5   # the evaluated model on the leaderboard's own route

The task's model roles default to these models (task.py: referee, second_rater, tiebreak_rater, partner_1..3), so the
partners and judges are Inspect model roles whose default is the leaderboard's model with its exact harness settings
(llm.py: MODEL_EXTRAS, ROUTE_EXTRA_BODY, the OpenAI effort, the vLLM chat-template switch, the output budget, the judges'
temperature 0, the OpenRouter data policy). A role bound to one of them is called through the harness directly, as on
the leaderboard (task.py), so what is sent is byte for byte the leaderboard's request (tests/test_inspect_eval.py
compares the request payloads); a role bound to any other Inspect model goes through Inspect (bridge.py).

As the evaluated model, a collab_intel model is called through Inspect (logged as a model event) with the request the
harness would make: the bridge hands this provider the harness's own request (bridge.PENDING) and llm._call_once sends
it, so `--model collab_intel/<name>` sends what scripts/run_benchmark.py sends for <name>. Inspect's generation options
(--reasoning-effort, --temperature, ...) do not apply: the harness's settings for the model do. Called by other code
(no pending harness request), it converts Inspect's messages and tools to the harness's and calls llm.call_model.
"""
from __future__ import annotations

import json
import uuid
from functools import partial
from typing import Any

import anyio
from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ContentReasoning,
    ContentText,
    GenerateConfig,
    ModelAPI,
    ModelOutput,
    ModelUsage,
    modelapi,
)
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo

from collab_intel.harness import llm

API = "collab_intel"
_STOP = {"end_turn": "stop", "stop": "stop", "stop_sequence": "stop", "completed": "stop", "tool_use": "tool_calls",
         "tool_calls": "tool_calls", "max_tokens": "max_tokens", "length": "max_tokens", "incomplete": "max_tokens",
         "refusal": "content_filter", "content_filter": "content_filter"}


@modelapi(name=API)
def collab_intel_api() -> type[ModelAPI]:
    return HarnessModelAPI


class HarnessModelAPI(ModelAPI):
    """An Inspect model API whose calls go through the harness's route for the model name (module docstring)."""

    def __init__(self, model_name: str, base_url: str | None = None, api_key: str | None = None,
                 config: GenerateConfig | None = None, **model_args: Any) -> None:
        super().__init__(model_name=model_name, base_url=base_url, api_key=api_key, api_key_vars=[],
                         config=config or GenerateConfig())
        if model_name.startswith(llm.INSPECT_PREFIXES):
            raise ValueError(f"collab_intel/{model_name}: name a harness model, e.g. collab_intel/claude-sonnet-4-6")
        llm.provider_of(model_name)  # an unknown name fails here, with the harness's own message

    async def generate(self, input: list[Any], tools: list[ToolInfo], tool_choice: ToolChoice,
                       config: GenerateConfig) -> ModelOutput:
        from collab_intel.inspect_eval.bridge import PENDING

        name = self.model_name
        req = PENDING.get()
        if req is not None and req.get("model") == name:
            # the harness's own request, made as llm.call_model makes it for `name` (the bridge's call_model did the
            # budget, the empty-reply retry and the usage log)
            temperature = req["temperature"] if req["temperature"] is not None and llm.accepts_temperature(name) else None
            out, log = await anyio.to_thread.run_sync(partial(
                llm._call_once, name, llm.provider_of(name), req["system"], req["messages"], req["tools"], req["budget"],
                req["thinking"], temperature, req["timeout"]))
        else:
            system, messages = from_inspect(input)
            htools = [{"name": t.name, "description": t.description,
                       "input_schema": t.parameters.model_dump(exclude_none=True)} for t in tools] or None
            out = await anyio.to_thread.run_sync(partial(
                llm.call_model, name, system, messages, htools, max_tokens=config.max_tokens or 1500, tag="inspect",
                temperature=config.temperature))
            log = {}
        return to_output(name, out, log)

    def should_retry(self, ex: Exception) -> bool:
        return False  # the harness's route has retried already (llm._with_backoff)

    def max_tokens(self) -> int | None:
        return None


def to_output(name: str, out: dict[str, Any], log: dict[str, Any]) -> ModelOutput:
    """A harness result (llm._call_once / call_model) as an Inspect ModelOutput; the harness's own figures (cost,
    refusal, empty-reply retry, usage-log fields) ride in metadata["harness"]."""
    content: list[Any] = []
    if out.get("reasoning"):
        content.append(ContentReasoning(reasoning=out["reasoning"]))
    if out.get("text") or not content:
        content.append(ContentText(text=out.get("text") or ""))
    calls = [ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:16]}", function=tc["name"], arguments=tc["input"])
             for tc in out.get("tool_calls") or []]
    msg = ChatMessageAssistant(content=content if len(content) > 1 else content[0].text, tool_calls=calls or None,
                               model=name, source="generate")
    u = out.get("usage") or {}
    usage = ModelUsage(input_tokens=u.get("input_tokens", 0) or 0, output_tokens=u.get("output_tokens", 0) or 0,
                       total_tokens=(u.get("input_tokens", 0) or 0) + (u.get("output_tokens", 0) or 0),
                       input_tokens_cache_read=u.get("cache_read_input_tokens") or None,
                       input_tokens_cache_write=u.get("cache_creation_input_tokens") or None)
    stop = _STOP.get(str(out.get("stop_reason")), "tool_calls" if calls else "unknown")
    harness = {"cost_usd": out.get("cost_usd"), "stop_reason": out.get("stop_reason"),
               **{k: out[k] for k in ("refusal", "meta") if out.get(k)}, "log": json.loads(json.dumps(log, default=str))}
    return ModelOutput(model=name, choices=[ChatCompletionChoice(message=msg, stop_reason=stop)], usage=usage,
                       metadata={"harness": harness})


def from_inspect(input: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    """Inspect ChatMessages as the harness's (system prompt, neutral messages) (llm.py docstring)."""
    system, out = "", []
    for m in input:
        if isinstance(m, ChatMessageSystem):
            system = (system + "\n\n" + m.text) if system else m.text
        elif isinstance(m, ChatMessageUser):
            out.append({"role": "user", "content": m.text})
        elif isinstance(m, ChatMessageAssistant):
            out.append({"role": "assistant", "content": m.text,
                        "tool_calls": [{"id": tc.id, "name": tc.function, "input": tc.arguments} for tc in m.tool_calls or []]})
        elif isinstance(m, ChatMessageTool):
            r = {"id": m.tool_call_id, "content": m.text}
            if out and out[-1]["role"] == "tool_results":
                out[-1]["results"].append(r)
            else:
                out.append({"role": "tool_results", "results": [r]})
    return system, out
