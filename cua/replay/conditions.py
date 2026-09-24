"""Evaluate schema Conditions against a live surface, and describe them for failure reports."""
import re

from cua.schema.capability import PLACEHOLDER, Condition


def fill(s: str, params: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: params[m.group(1)], s)


def _norm(s: str) -> str:
    return " ".join(s.split())


async def holds(cond: Condition, surface, params: dict[str, str]) -> bool:
    k = cond.kind
    if k in ("text_present", "text_absent"):
        present = _norm(fill(cond.text, params)) in _norm(await surface.page_text(cond.frame))
        return present if k == "text_present" else not present
    if k == "url_matches":
        return re.search(fill(cond.pattern, params), await surface.url()) is not None
    if k == "element_present":
        return await surface.exists(cond.target)
    if k == "element_text_matches":
        try:
            return re.search(fill(cond.pattern, params), await surface.read(cond.target)) is not None
        except Exception:  # noqa: BLE001  an unreadable element simply does not match
            return False
    if k == "all_of":
        return all([await holds(c, surface, params) for c in cond.conditions])
    if k == "any_of":
        return any([await holds(c, surface, params) for c in cond.conditions])
    raise ValueError(f"unknown condition kind {k}")


def describe(cond: Condition, params: dict[str, str]) -> str:
    k = cond.kind
    where = f" in frame '{cond.frame}'" if getattr(cond, "frame", None) else ""
    if k == "text_present":
        return f"text '{fill(cond.text, params)}' present{where}"
    if k == "text_absent":
        return f"text '{fill(cond.text, params)}' absent{where}"
    if k == "url_matches":
        return f"url matching /{fill(cond.pattern, params)}/"
    if k == "element_present":
        return "target element present"
    if k == "element_text_matches":
        return f"element text matching /{fill(cond.pattern, params)}/"
    joiner = " AND " if k == "all_of" else " OR "
    return "(" + joiner.join(describe(c, params) for c in cond.conditions) + ")"
