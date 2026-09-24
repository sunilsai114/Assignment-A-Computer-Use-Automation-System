"""Intervention requests: everything a human operator needs to act, persisted as one JSON file each.
The file doubles as the audit record: who took control, when, what they did, and how it ended."""
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ID_PATTERN = re.compile(r"^iv-[0-9a-f]{8}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Intervention:
    id: str
    run_id: str
    capability_id: str
    step_id: str | None
    reason: str
    url: str
    screenshot: str | None
    kind: str = "stuck"               # approval: a risky step needs a yes | stuck: automation cannot proceed
    evidence_dir: str | None = None
    state: str = "open"               # open -> claimed -> resolved | aborted | expired | session_lost
    claimed_by: str | None = None
    decision: dict | None = None      # {action, operator, note, ts}
    created_at: str = field(default_factory=now_iso)
    control_log: list[dict] = field(default_factory=list)
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
        if not ID_PATTERN.match(iv_id):  # ids reach here from HTTP: never let one become a path
            raise KeyError(f"bad intervention id {iv_id!r}")
        return Intervention(**json.loads((self.root / f"{iv_id}.json").read_text(encoding="utf-8")))
