"""Gemini implementation of ModelClient, via function calling (mode ANY: every turn is exactly one tool call).

Deliberately plain function calling over our own tools rather than a provider-specific computer-use model:
our observation is an accessibility-style element list, so any tool-calling model can drive it, and
swapping providers means one new class.
"""
import asyncio
import time

from google import genai
from google.genai import errors, types

from cua.agent.model import TOOL_NAMES, TOOLS, ModelAction, Turn


def _declarations() -> list[types.FunctionDeclaration]:
    decls = []
    for t in TOOLS:
        props = {k: {"type": "string", "description": v} for k, v in t["params"].items()}
        props["reasoning"] = {"type": "string", "description": "one short sentence: why this is the right next action"}
        decls.append(types.FunctionDeclaration(
            name=t["name"], description=t["description"],
            parameters_json_schema={"type": "object", "properties": props, "required": list(props)}))
    return decls


class GeminiClient:
    def __init__(self, api_key: str, model: str, min_interval_s: float = 4.0, max_retries: int = 6):
        self.client = genai.Client(api_key=api_key)
        self.name = model
        self.min_interval_s = min_interval_s  # stay under free-tier requests-per-minute limits
        self.max_retries = max_retries
        self._last = 0.0
        self.tools = [types.Tool(function_declarations=_declarations())]

    async def decide(self, turn: Turn) -> ModelAction:
        parts = [types.Part(text=turn.user)]
        if turn.screenshot:
            parts.append(types.Part.from_bytes(data=turn.screenshot, mime_type="image/png"))
        config = types.GenerateContentConfig(
            system_instruction=turn.system, tools=self.tools, temperature=0,
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        resp = await self._call([types.Content(role="user", parts=parts)], config)
        usage = {}
        if resp.usage_metadata:
            usage = {"prompt_tokens": resp.usage_metadata.prompt_token_count,
                     "output_tokens": resp.usage_metadata.candidates_token_count}
        calls = resp.function_calls or []
        if not calls or calls[0].name not in TOOL_NAMES:
            return ModelAction("escalate", {"reason": "model returned no usable tool call"}, "", usage)
        args = dict(calls[0].args or {})
        return ModelAction(calls[0].name, args, str(args.pop("reasoning", "")), usage)

    async def _call(self, contents, config):
        for attempt in range(self.max_retries):
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            try:
                return await self.client.aio.models.generate_content(model=self.name, contents=contents, config=config)
            except errors.APIError as e:
                retryable = e.code == 429 or (e.code or 0) >= 500
                if not retryable or attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(min(60, 5 * 2 ** attempt))  # rate limit or transient server error
        raise RuntimeError("unreachable")
