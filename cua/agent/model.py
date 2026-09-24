"""Provider seam: the agent loop only depends on this interface."""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ModelAction:
    tool: str  # click | type | select | navigate | read | done | escalate
    args: dict = field(default_factory=dict)
    reasoning: str = ""


class ModelClient(Protocol):
    def decide(self, goal: str, observation: dict, history: list[dict]) -> ModelAction: ...
