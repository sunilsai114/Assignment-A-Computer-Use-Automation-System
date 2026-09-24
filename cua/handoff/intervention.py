"""Intervention requests: what a human operator needs to act (minimal file-backed store).
The control-transfer state machine that uses these is added in the handoff milestone."""
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Intervention:
    id: str
    run_id: str
    capability_id: str
    step_id: str | None
    reason: str
    url: str
    screenshot: str | None
    state: str = "open"  # open -> claimed -> resolved | aborted
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    human_actions: list[dict] = field(default_factory=list)


class InterventionStore:
    def __init__(self, root: Path):
        self.root = Path(root) / "interventions"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, **kw) -> Intervention:
        iv = Intervention(id=f"iv-{uuid.uuid4().hex[:8]}", **kw)
        self.save(iv)
        return iv

    def save(self, iv: Intervention) -> None:
        (self.root / f"{iv.id}.json").write_text(json.dumps(asdict(iv), indent=2), encoding="utf-8")

    def load(self, iv_id: str) -> Intervention:
        return Intervention(**json.loads((self.root / f"{iv_id}.json").read_text(encoding="utf-8")))
