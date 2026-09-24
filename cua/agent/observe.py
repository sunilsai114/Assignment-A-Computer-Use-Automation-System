"""Observation: what the model sees each turn.

An accessibility-style list of controls (role + name + label, per frame) plus each frame's visible text.
This is the same shape a desktop accessibility tree (UIA/AX) gives, so the agent loop is not tied to a
clean DOM. Elements are addressed by short refs (e7) that are only valid for one observation; the model
never sees or writes selectors. Everything rendered for the model passes through the Redactor.
"""
from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError, Frame

from cua.policy.redact import Redactor

REF_ATTR = "data-cua-ref"
MAX_TEXT_PER_FRAME = 900

# Tags each interesting element with a ref and returns the facts the recorder later turns into locators.
OBSERVE_JS = r"""
([start, refAttr]) => {
  document.querySelectorAll('[' + refAttr + ']').forEach(e => e.removeAttribute(refAttr));
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const roleOf = e => {
    const r = e.getAttribute('role'); if (r) return r;
    const t = e.tagName.toLowerCase(), ty = (e.getAttribute('type') || 'text').toLowerCase();
    if (t === 'a') return 'link'; if (t === 'button') return 'button'; if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') { if (['submit','button','reset','image'].includes(ty)) return 'button';
      if (ty === 'checkbox' || ty === 'radio') return ty; return 'textbox'; }
    if (t === 'td' || t === 'th') return 'cell'; if (/^h[1-6]$/.test(t)) return 'heading';
    return 'clickable';
  };
  const out = []; let n = start;
  const add = (e, extra) => {
    const ref = 'e' + (n++); e.setAttribute(refAttr, ref);
    const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || '').toLowerCase();
    const td = e.closest('td'), prev = td && td !== e ? td.previousElementSibling : null;
    let text = '';
    if (tag === 'select') text = e.selectedIndex >= 0 ? clean(e.options[e.selectedIndex].text) : '';
    else if (tag === 'input') text = type === 'password' ? '' : (['submit','button'].includes(type) ? '' : clean(e.value));
    else text = clean(e.innerText).slice(0, 80);
    out.push(Object.assign({ref, tag, type, role: roleOf(e), name_attr: e.getAttribute('name'),
      aria: clean(e.getAttribute('aria-label')), label: e.labels && e.labels.length ? clean(e.labels[0].innerText) : '',
      adjacent: prev ? clean(prev.innerText) : '',
      value_attr: tag === 'input' && ['submit','button'].includes(type) ? e.value : null,
      options: tag === 'select' ? [...e.options].map(o => clean(o.text)) : null, text}, extra || {}));
  };
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,[onclick],[role=button],[role=link],h1,h2,h3';
  for (const e of document.querySelectorAll(sel)) if (visible(e)) add(e);
  for (const tr of document.querySelectorAll('tr')) {   // data cells in leaf rows, keyed by the row's label
    if (tr.querySelector('tr')) continue;
    const tds = [...tr.children].filter(c => c.tagName === 'TD');
    if (tds.length < 2) continue;
    const rowText = clean(tds[0].innerText); if (!rowText) continue;
    tds.forEach((td, i) => { if (i === 0 || td.querySelector('input,select,textarea,button,a,[onclick]')) return;
      if (clean(td.innerText)) add(td, {row_text: rowText, col: i}); });
  }
  return {elements: out, text: document.body ? document.body.innerText : '', next: n};
}
"""


@dataclass
class Element:
    ref: str
    frame_path: list[str]
    tag: str
    type: str
    role: str
    name_attr: str | None
    aria: str
    label: str
    adjacent: str
    value_attr: str | None
    options: list[str] | None
    text: str
    row_text: str | None = None
    col: int | None = None

    @property
    def frame(self) -> str | None:
        return self.frame_path[-1] if self.frame_path else None

    @property
    def name(self) -> str:
        """What a person would call this control."""
        return self.aria or self.label or self.adjacent or self.value_attr or self.text


@dataclass
class Observation:
    url: str
    elements: list[Element]
    frame_text: dict[str, str] = field(default_factory=dict)  # frame name ('' = top document) -> visible text

    def get(self, ref: str) -> Element | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def render(self, redactor: Redactor, structure_only: bool = False) -> str:
        """The text the model sees: redacted; data cells carry their row label so the model can pick outputs.
        structure_only drops visible text and cell values: that version goes to evidence files, because names
        and other free-text PII cannot be caught by pattern redaction."""
        lines = [f"URL: {redactor.text(self.url)}"]
        frames = sorted({e.frame or "" for e in self.elements} | set(self.frame_text))
        for fr in frames:
            text = " ".join(self.frame_text.get(fr, "").split())[:MAX_TEXT_PER_FRAME]
            lines.append(f"\nFRAME {fr or '(top)'}")
            if text and not structure_only:
                lines.append(f"  visible text: {redactor.text(text)}")
            for e in (x for x in self.elements if (x.frame or "") == fr):
                shown = "…" if structure_only and e.row_text is not None else redactor.text(e.name)
                bits = [f"  {e.ref} {e.role} \"{shown}\""]
                if e.row_text is not None:
                    bits.append(f"[row \"{redactor.text(e.row_text)}\", col {e.col}]")
                if e.type == "password":
                    bits.append("(password)")
                if e.options:
                    bits.append(f"options={e.options}")
                if e.role == "textbox" and e.text and not structure_only:
                    bits.append(f"value=\"{redactor.text(e.text)}\"")
                lines.append(" ".join(bits))
        return "\n".join(lines)


def frame_path(frame: Frame) -> list[str]:
    names = []
    while frame.parent_frame is not None:
        names.append(frame.name)
        frame = frame.parent_frame
    return list(reversed(names))


async def observe(surface) -> Observation:
    counter, elements, texts = 1, [], {}
    for f in surface.page.frames:
        if f.is_detached():
            continue
        try:
            res = await f.evaluate(OBSERVE_JS, [counter, REF_ATTR])
        except PlaywrightError:
            continue  # frame mid-navigation or a frameset shell
        counter = res["next"]
        path = frame_path(f)
        texts[path[-1] if path else ""] = res["text"] or ""
        elements += [Element(frame_path=path, **e) for e in res["elements"]]
    return Observation(url=surface.page.url, elements=elements, frame_text=texts)


async def frame_texts(surface) -> dict[str, str]:
    """Visible text per frame name ('' = top document): used to see what an action changed."""
    out = {}
    for f in surface.page.frames:
        if f.is_detached():
            continue
        try:
            path = frame_path(f)
            out[path[-1] if path else ""] = await f.evaluate("document.body ? document.body.innerText : ''")
        except PlaywrightError:
            pass
    return out
