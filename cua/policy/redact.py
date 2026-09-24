"""Redaction: applied to everything that reaches a log, an evidence file or an artifact."""
import re
from typing import Any

MASK = "●●●●"


class Redactor:
    def __init__(self, cfg: dict):
        self.patterns = {k: re.compile(v) for k, v in cfg.items() if k != "secret_field_names"}
        self.secret_fields = {n.lower() for n in cfg.get("secret_field_names", [])}
        self._secrets: set[str] = set()

    def add_secret_values(self, *values: str) -> None:
        """Runtime secrets (credentials read from env) are scrubbed by exact value too."""
        self._secrets |= {v for v in values if v}

    def text(self, s: str) -> str:
        for v in self._secrets:
            s = s.replace(v, MASK)
        for name, rx in self.patterns.items():
            s = rx.sub(f"[{name}]", s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: (MASK if str(k).lower() in self.secret_fields else self.obj(v)) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o
