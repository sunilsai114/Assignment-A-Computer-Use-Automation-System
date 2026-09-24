"""Policy: an explicit, default-deny allowlist plus risk classification.

Enforced in two independent places so a bug in one does not open the other:
  1. the surface's network guard (WebSurface) asks `check_url` for every request the browser makes;
  2. the replay/agent loops ask `check_step` before every action.
"""
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

import yaml

from cua.config import ROOT
from cua.policy.redact import Redactor
from cua.schema.capability import RISK_ORDER, Risk, Step


class Verdict(str, Enum):
    allow = "allow"
    block = "block"
    needs_approval = "needs_approval"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.verdict == Verdict.block

    @property
    def needs_approval(self) -> bool:
        return self.verdict == Verdict.needs_approval


ALLOW = Decision(Verdict.allow)


class Policy:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.hosts = set(cfg["allowed_hosts"])
        self.paths = cfg["allowed_paths"]
        self.actions = set(cfg["allowed_actions"])
        self.markers = [m.lower() for m in cfg["irreversible_markers"]]
        self.irreversible_policy = cfg.get("irreversible_policy", "require_approval")
        self.app_error_markers = [m.lower() for m in cfg.get("app_error_markers", [])]
        self.limits = cfg["limits"]
        self.redactor = Redactor(cfg["redact"])

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> "Policy":
        return cls(yaml.safe_load((path or ROOT / "config" / "policy.yaml").read_text(encoding="utf-8")))

    def check_url(self, url: str) -> Decision:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return Decision(Verdict.block, f"scheme '{u.scheme}' is not allowed")
        if u.hostname not in self.hosts:
            return Decision(Verdict.block, f"host '{u.hostname}' is not on the allowlist")
        if not any(fnmatch(u.path or "/", pat) for pat in self.paths):
            return Decision(Verdict.block, f"path '{u.path}' is not on the allowlist")
        return ALLOW

    def risk_of(self, step: Step) -> Risk:
        """Declared risk, raised to `irreversible` if the step's wording matches a marker."""
        texts = [step.intent]
        if step.target:
            texts += [str(v) for loc in step.target.locators for v in loc.params.values()]
        if step.value and step.value.literal:
            texts.append(step.value.literal)
        blob = " ".join(texts).lower()
        derived = Risk.irreversible if any(m in blob for m in self.markers) else Risk.safe
        return max(step.risk, derived, key=RISK_ORDER.get)

    def check_step(self, step: Step, current_url: str = "") -> Decision:
        if step.action.value not in self.actions:
            return Decision(Verdict.block, f"action '{step.action.value}' is not allowed")
        if current_url and current_url != "about:blank" and (d := self.check_url(current_url)).blocked:
            return Decision(Verdict.block, f"current page is outside the allowlist: {d.reason}")
        if self.risk_of(step) == Risk.irreversible:
            if self.irreversible_policy == "block":
                return Decision(Verdict.block, f"step '{step.id}' is irreversible and policy is 'block'")
            return Decision(Verdict.needs_approval, f"step '{step.id}' is irreversible: a human must approve it")
        return ALLOW
