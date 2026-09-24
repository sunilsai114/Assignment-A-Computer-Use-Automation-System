"""Provider seam: the agent loop only depends on ModelClient. Gemini is one implementation; ScriptedClient
drives the same loop deterministically in tests (and is how the loop is exercised without an API key)."""
from dataclasses import dataclass, field
from typing import Protocol

from cua.agent.observe import Observation

# The tools the model may call. Every element argument is a ref from the current observation.
TOOLS: list[dict] = [
    {"name": "click", "description": "Click a link, button or other clickable element.",
     "params": {"element": "ref of the element, e.g. e7", "intent": "what this click is for, in a few words"}},
    {"name": "type_text", "description": "Replace the contents of a text field. Use {{input_name}} placeholders for "
     "caller inputs and {{secret:NAME}} for credentials; never invent values.",
     "params": {"element": "ref of the field", "text": "text or placeholder to type", "intent": "what this is for"}},
    {"name": "select_option", "description": "Choose an option in a drop-down.",
     "params": {"element": "ref of the drop-down", "option": "visible option text or {{input_name}}", "intent": "what this is for"}},
    {"name": "read_output", "description": "Record the value shown in an element as one of the requested outputs.",
     "params": {"element": "ref of the element showing the value", "output": "output name", "intent": "what is being read"}},
    {"name": "navigate", "description": "Go to a path within the application (rarely needed).",
     "params": {"path": "path such as /heritage/", "intent": "why"}},
    {"name": "done", "description": "The goal is complete: every requested output has been read, or the target screen is shown.",
     "params": {"summary": "one sentence on what was accomplished"}},
    {"name": "escalate", "description": "Ask a human for help: you are stuck, the screen is unexpected, or the goal "
     "would require an irreversible or unsafe action.",
     "params": {"reason": "why a human is needed"}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


@dataclass
class ModelAction:
    tool: str
    args: dict = field(default_factory=dict)
    reasoning: str = ""
    usage: dict = field(default_factory=dict)  # token counts etc., for evidence


@dataclass
class Turn:
    system: str
    user: str
    observation: Observation
    screenshot: bytes | None = None


class ModelClient(Protocol):
    name: str

    async def decide(self, turn: Turn) -> ModelAction: ...


class ScriptedClient:
    """Plays a fixed script. Elements are matched by what a person would call them, never by ref, so the
    script survives ref renumbering exactly as a real model's choices must.

    Each entry: (tool, match, args) where match is {"name": ..., "role": ..., "row_text": ..., "col": ...}."""
    name = "scripted"

    def __init__(self, script: list[tuple[str, dict, dict]]):
        self.script = list(script)
        self.calls = 0

    async def decide(self, turn: Turn) -> ModelAction:
        self.calls += 1
        if not self.script:
            return ModelAction("escalate", {"reason": "script exhausted"}, "no more scripted steps")
        tool, match, args = self.script.pop(0)
        args = dict(args)
        if match:
            el = next((e for e in turn.observation.elements
                       if all(getattr(e, k, None) == v if k != "name" else e.name == v for k, v in match.items())), None)
            if el is None:
                return ModelAction("escalate", {"reason": f"scripted element {match} not on screen"}, "")
            args["element"] = el.ref
        return ModelAction(tool, args, reasoning=f"scripted step {self.calls}")
