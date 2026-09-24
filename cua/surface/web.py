"""Playwright-backed web surface. Works on framesets and nested-table markup: locators are resolved
per frame, and controls are found by label/role/text/row-position rather than ids or CSS classes."""
import asyncio
import re
import time
from urllib.parse import urljoin

from playwright.async_api import Browser, Error as PlaywrightError, Frame, Locator as PWLocator

from cua.policy.engine import Policy
from cua.schema.capability import Locator, Target
from cua.surface.base import NavigationBlocked, SurfaceError, TargetNotFound


def xp(s: str) -> str:
    """XPath string literal safe for quotes."""
    if "'" not in s:
        return f"'{s}'"
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in s.split("'")) + ")"


class WebSurface:
    def __init__(self, browser: Browser, base_url: str, policy: Policy, headless: bool = True):
        self.browser, self.base_url, self.policy = browser, base_url, policy
        self.last_status: int | None = None
        self.last_locator_index = 0
        self.blocked: list[str] = []
        self._inflight = 0
        self.context = self.page = None
        self.controller = None  # set by SessionController.attach(); gates every mutating action

    @classmethod
    async def open(cls, browser: Browser, base_url: str, policy: Policy) -> "WebSurface":
        s = cls(browser, base_url, policy)
        s.context = await browser.new_context(viewport={"width": 1100, "height": 760})
        s.page = await s.context.new_page()
        s.page.set_default_timeout(policy.limits["action_timeout_ms"])
        await s.context.route("**/*", s._guard)
        s.page.on("request", lambda _r: setattr(s, "_inflight", s._inflight + 1))
        for ev in ("requestfinished", "requestfailed"):
            s.page.on(ev, lambda _r: setattr(s, "_inflight", max(0, s._inflight - 1)))
        s.page.on("response", s._on_response)
        return s

    async def close(self) -> None:
        await self.context.close()

    def alive(self) -> bool:
        """False once the page, context or browser is gone (e.g. someone closed the window)."""
        return self.page is not None and not self.page.is_closed() and self.browser.is_connected()

    # ── guards & bookkeeping ──
    async def _guard(self, route) -> None:
        d = self.policy.check_url(route.request.url)
        if d.blocked:
            self.blocked.append(f"{route.request.url}: {d.reason}")
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    def _on_response(self, resp) -> None:
        if resp.request.is_navigation_request():
            self.last_status = resp.status

    def reset_status(self) -> None:
        self.last_status = None

    def _guard_control(self) -> None:
        if self.controller is not None:
            self.controller.assert_automation()

    async def install_human_recorder(self, callback) -> None:
        """Capture what a human does in this live session (every frame). `callback(payload, frame=name)`."""
        await self.context.expose_binding(
            "__cuaHuman", lambda source, payload: callback(payload, frame=source["frame"].name))
        await self.context.add_init_script(HUMAN_RECORDER_JS)
        for f in self.page.frames:  # frames already loaded before install
            try:
                await f.evaluate(HUMAN_RECORDER_JS)
            except PlaywrightError:
                pass

    # ── navigation / state ──
    async def goto(self, url: str) -> None:
        self._guard_control()
        full = urljoin(self.base_url, url)
        if (d := self.policy.check_url(full)).blocked:
            raise NavigationBlocked(d.reason)
        try:
            await self.page.goto(full, wait_until="load")  # "load" includes child frames of a frameset
        except PlaywrightError as e:
            if self.blocked:
                raise NavigationBlocked(self.blocked[-1]) from e
            raise SurfaceError(str(e).splitlines()[0]) from e

    async def url(self) -> str:
        return self.page.url

    async def settle(self, timeout_ms: int = 8000, quiet_ms: int = 150) -> int:
        """Wait until no requests are in flight for `quiet_ms`. Returns milliseconds waited."""
        start = time.monotonic()
        quiet_since = None
        while (time.monotonic() - start) * 1000 < timeout_ms:
            if self._inflight <= 0:
                quiet_since = quiet_since or time.monotonic()
                if (time.monotonic() - quiet_since) * 1000 >= quiet_ms:
                    break
            else:
                quiet_since = None
            await asyncio.sleep(0.05)
        return int((time.monotonic() - start) * 1000)

    # ── frames ──
    def _frame(self, path: list[str]) -> Frame:
        cur = self.page.main_frame
        for name in path:
            # child_frames can still list a frame from before a reload; only live frames count
            nxt = next((f for f in cur.child_frames if f.name == name and not f.is_detached()), None) \
                or next((f for f in self.page.frames if f.name == name and not f.is_detached()), None)
            if nxt is None:
                raise TargetNotFound(f"frame '{name}' not found")
            cur = nxt
        return cur

    async def page_text(self, frame: str | None = None) -> str:
        frames = [f for f in self.page.frames if f.name == frame] if frame else self.page.frames
        parts = []
        for f in frames:
            try:
                # evaluate, not locator("body"): a frameset has no <body> and locator() would wait out its timeout
                parts.append(await f.evaluate("document.body ? document.body.innerText : ''"))
            except PlaywrightError:
                pass  # frame mid-navigation; the caller polls
        return "\n".join(parts)

    # ── locator resolution ──
    @staticmethod
    def _to_locator(frame: Frame, l: Locator) -> PWLocator | None:
        p = l.params
        if l.kind == "role":
            name = re.compile(".+") if p["name"] == ".+" else p["name"]
            return frame.get_by_role(p["role"], name=name, exact=True) if isinstance(name, str) \
                else frame.get_by_role(p["role"], name=name)
        if l.kind == "label_text":
            sib = (f"xpath=//td[normalize-space()={xp(str(p['text']))}]/following-sibling::td[1]"
                   "//*[self::input or self::select or self::textarea]")
            return frame.get_by_label(str(p["text"]), exact=True).or_(frame.locator(sib))
        if l.kind == "attr":
            value = str(p["value"]).replace("\\", "\\\\").replace('"', '\\"')
            return frame.locator(f'[{p["attr"]}="{value}"]')
        if l.kind == "text":
            return frame.get_by_text(str(p["text"]), exact=True)
        if l.kind == "table_cell":
            row = xp(str(p["row_text"]))
            return frame.locator(f"xpath=//tr[not(.//tr)][td[1][normalize-space()={row}]]/td[{int(p['col']) + 1}]")
        if l.kind == "structural":
            return frame.locator(str(p["path"]))
        return None  # 'visual' needs a screenshot-driven surface; not supported here

    async def _resolve(self, target: Target) -> PWLocator:
        frame = self._frame(target.frame_path)
        tried = []
        for i, l in enumerate(target.locators):
            loc = self._to_locator(frame, l)
            if loc is None:
                tried.append(f"{l.kind}: unsupported on this surface")
                continue
            try:
                n = await loc.count()
                if n == 1 and target.expect_tag:
                    tag = (await loc.evaluate("e => e.tagName")).lower()
                    if tag != target.expect_tag:
                        tried.append(f"{l.kind}: matched <{tag}>, expected <{target.expect_tag}>")
                        continue
            except PlaywrightError as e:
                tried.append(f"{l.kind}: {str(e).splitlines()[0]}")
                continue
            if n == 1:
                self.last_locator_index = i
                return loc
            tried.append(f"{l.kind} {dict(l.params)}: {n} matches")
        raise TargetNotFound("; ".join(tried))

    # ── actions ──
    async def _do(self, coro):
        try:
            return await coro
        except PlaywrightError as e:
            if self.blocked:
                raise NavigationBlocked(self.blocked[-1]) from e
            raise SurfaceError(str(e).splitlines()[0]) from e

    async def click(self, target: Target) -> None:
        self._guard_control()
        await self._do((await self._resolve(target)).click(timeout=3000))

    async def fill(self, target: Target, text: str) -> None:
        self._guard_control()
        await self._do((await self._resolve(target)).fill(text, timeout=3000))

    async def select(self, target: Target, value: str) -> None:
        self._guard_control()
        await self._do((await self._resolve(target)).select_option(label=value, timeout=3000))

    async def read(self, target: Target) -> str:
        return (await self._do((await self._resolve(target)).inner_text(timeout=3000))).strip()

    async def identify(self, target: Target, attr: str) -> str | None:
        """If the target resolves to exactly one element, return that element's `attr` (else None).
        The recorder uses this to prove a locator hits the very element the agent acted on."""
        try:
            return await (await self._resolve(target)).get_attribute(attr, timeout=1000)
        except (TargetNotFound, PlaywrightError):
            return None

    async def exists(self, target: Target) -> bool:
        try:
            await self._resolve(target)
            return True
        except TargetNotFound:
            return False

    # ── evidence ──
    async def screenshot(self, mask_data: bool = False) -> bytes:
        """Password fields are always masked. mask_data also blanks table values (not their row labels) and
        typed field contents: used for evidence files, which outlive the run and may be widely read."""
        masks = []
        for f in self.page.frames:
            if f.is_detached():
                continue
            masks.append(f.locator("input[type=password]"))
            if mask_data:
                masks.append(f.locator("tr:not(:has(tr)) > td:not(:first-child)"))
                masks.append(f.locator("input:not([type=submit]):not([type=button]):not([type=hidden])"))
        return await self.page.screenshot(mask=masks)

    async def dom_snapshot(self) -> str:
        parts = []
        for f in self.page.frames:
            try:
                parts.append(f"<!-- frame name={f.name!r} url={f.url} -->\n" + await f.content())
            except PlaywrightError:
                pass
        return "\n".join(parts)


# Injected into every frame. Describes controls the way the artifact does (label/role text), never by
# position, so a human's actions can later be turned into steps. Password values never leave the page.
HUMAN_RECORDER_JS = r"""
(() => {
  if (window.__cuaRecorder) return; window.__cuaRecorder = true;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const describe = el => {
    const lab = el.labels && el.labels[0] ? el.labels[0].innerText : '';
    const cell = el.closest && el.closest('td');
    const adj = cell && cell.previousElementSibling ? cell.previousElementSibling.innerText : '';
    return clean(el.getAttribute('aria-label') || lab || adj || el.value || el.innerText);
  };
  const send = (kind, el, extra) => {
    try { window.__cuaHuman({kind, tag: el.tagName.toLowerCase(), name: el.getAttribute('name'),
      type: el.getAttribute('type'), text: describe(el), path: location.pathname, ...extra}); } catch (e) {}
  };
  document.addEventListener('click', e => {
    const el = e.target.closest('a,button,input,select,[onclick]') || e.target; send('click', el, {});
  }, true);
  document.addEventListener('change', e => {
    const el = e.target; const secret = (el.type || '').toLowerCase() === 'password';
    send('input', el, {value: secret ? '●●●●' : String(el.value).slice(0, 200)});
  }, true);
})();
"""
