"""Evidence: a structured event log plus richer signals (screenshot, DOM snapshot) per run.
Everything passes through the Redactor before touching disk."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.policy.redact import Redactor


class EvidenceRecorder:
    def __init__(self, root: Path, run_id: str, redactor: Redactor):
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor

    def log(self, event: str, **fields: Any) -> None:
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event,
                **self.redactor.obj(fields)}
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")

    def write_json(self, name: str, obj: Any) -> str:
        (self.dir / name).write_text(json.dumps(self.redactor.obj(obj), indent=2, default=str), encoding="utf-8")
        return name

    async def screenshot(self, name: str, surface) -> str | None:
        try:
            (self.dir / f"{name}.png").write_bytes(await surface.screenshot())
            return f"{name}.png"
        except Exception as e:  # noqa: BLE001  evidence must never mask the real failure
            self.log("evidence_error", what="screenshot", error=repr(e))
            return None

    async def snapshot(self, name: str, surface) -> str | None:
        try:
            html = await surface.dom_snapshot()
            (self.dir / f"{name}.html").write_text(self.redactor.text(html), encoding="utf-8")
            return f"{name}.html"
        except Exception as e:  # noqa: BLE001
            self.log("evidence_error", what="dom_snapshot", error=repr(e))
            return None
