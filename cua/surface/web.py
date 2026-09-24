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

    # ── navigation / state ──
    async def goto(self, url: str) -> None:
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
                parts.append(await f.locator("body").inner_text(timeout=1000))
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
        await self._do((await self._resolve(target)).click(timeout=3000))

    async def fill(self, target: Target, text: str) -> None:
        await self._do((await self._resolve(target)).fill(text, timeout=3000))

    async def select(self, target: Target, value: str) -> None:
        await self._do((await self._resolve(target)).select_option(label=value, timeout=3000))

    async def read(self, target: Target) -> str:
        return (await self._do((await self._resolve(target)).inner_text(timeout=3000))).strip()

    async def exists(self, target: Target) -> bool:
        try:
            await self._resolve(target)
            return True
        except TargetNotFound:
            return False

    # ── evidence ──
    async def screenshot(self) -> bytes:
        masks = []
        for f in self.page.frames:
            masks.append(f.locator("input[type=password]"))
        return await self.page.screenshot(mask=masks)

    async def dom_snapshot(self) -> str:
        parts = []
        for f in self.page.frames:
            try:
                parts.append(f"<!-- frame name={f.name!r} url={f.url} -->\n" + await f.content())
            except PlaywrightError:
                pass
        return "\n".join(parts)
