"""Serena's own browser, read as text and driven by element, not by pixels.

Screenshot loops spend most of their time on the model deciding where to click.
Here the model reads the page's accessibility snapshot (roles, names and element
refs) and sends a whole sequence of steps in one call: click, fill, select, wait
for the next screen. Playwright runs them locally at machine speed over CDP and
stops at the first step that fails, so a known flow costs one model call instead
of one per click. This is the approach Playwright MCP and Claude in Chrome take.

It only ever attaches to her Edge profile's own loopback debugging port, verified
to belong to that profile. Input goes through CDP, not XTest, so it never touches
his pointer or keyboard and never trips the takeover monitor.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import re
import threading
from pathlib import Path

from core.computer_platform import ComputerError

MAX_STEPS = 25
STEP_TIMEOUT = 5.0
MAX_STEP_TIMEOUT = 30.0
SNAPSHOT_CHARS = 14000
RESULT_SNAPSHOT_CHARS = 7000
READ_CHARS = 3000
KINDS = (
    "goto",
    "click",
    "fill",
    "select",
    "check",
    "uncheck",
    "press",
    "wait_for",
    "read",
    "tab",
    "back",
)
TARGET_KEYS = ("ref", "role", "label", "placeholder", "text", "selector")
STOPPED = "stopped: input is held or the session ended"


class WebError(ComputerError):
    pass


def _target(value, where):
    if not isinstance(value, dict) or not any(key in value for key in TARGET_KEYS):
        raise WebError(f"{where} needs a target: one of {', '.join(TARGET_KEYS)}")
    extra = set(value) - set(TARGET_KEYS) - {"name", "exact", "nth"}
    if extra:
        raise WebError(f"{where} has unknown target fields: {sorted(extra)}")
    if "ref" in value and not re.fullmatch(r"e\d{1,6}", str(value["ref"])):
        raise WebError(f"{where} ref must look like e12")
    if "nth" in value and (type(value["nth"]) is not int or not 0 <= value["nth"] < 100):
        raise WebError(f"{where} nth must be 0-99")
    for key in TARGET_KEYS + ("name",):
        if key in value and (not isinstance(value[key], str) or not 0 < len(value[key]) <= 300):
            raise WebError(f"{where} {key} must be 1-300 characters")


def validate_steps(steps):
    """Reject a malformed batch before anything runs."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise WebError(f"a browser batch needs 1-{MAX_STEPS} steps")
    for index, step in enumerate(steps):
        where = f"step {index + 1}"
        if not isinstance(step, dict) or len(set(step) & set(KINDS)) != 1:
            raise WebError(f"{where} must have exactly one of: {', '.join(KINDS)}")
        (kind,) = set(step) & set(KINDS)
        extra = set(step) - {kind, "value", "timeout"}
        if extra:
            raise WebError(f"{where} has unknown fields: {sorted(extra)}")
        timeout = step.get("timeout", STEP_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_STEP_TIMEOUT:
            raise WebError(f"{where} timeout must be 0-{MAX_STEP_TIMEOUT:g} seconds")
        body = step[kind]
        if kind == "goto":
            if not isinstance(body, str) or not re.fullmatch(r"https?://\S{1,2040}", body):
                raise WebError(f"{where} goto needs one http(s) URL")
        elif kind in {"click", "check", "uncheck", "read"}:
            _target(body, where)
        elif kind in {"fill", "select"}:
            _target(body, where)
            if not isinstance(step.get("value"), str) or len(step["value"]) > 2000:
                raise WebError(f"{where} {kind} needs a value of at most 2000 characters")
        elif kind == "press":
            if not isinstance(body, str) or not re.fullmatch(r"[A-Za-z0-9+]{1,40}", body):
                raise WebError(f"{where} press needs a key such as Enter or Control+A")
        elif kind == "wait_for":
            if not isinstance(body, dict) or len(set(body) & {"text", "url", "target", "gone"}) != 1:
                raise WebError(f"{where} wait_for needs one of text, url, target, gone")
            if "target" in body:
                _target(body["target"], where)
            elif "gone" in body:
                _target(body["gone"], where)
            elif not isinstance(next(iter(body.values())), str):
                raise WebError(f"{where} wait_for text/url must be a string")
        elif kind == "tab":
            if not isinstance(body, dict) or len(set(body) & {"index", "url_contains", "title_contains"}) != 1:
                raise WebError(f"{where} tab needs index, url_contains or title_contains")
        elif kind == "back" and body not in (True, 1, {}):
            raise WebError(f"{where} back takes true")
    return steps


class HerBrowser:
    """Playwright attached to her Edge over CDP, on its own event loop thread."""

    def __init__(self, profile, *, prepare=None):
        self.profile = Path(profile)
        # Called before attaching: makes sure her Edge is up with its port.
        self.prepare = prepare
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="computer-web", daemon=True)
        self.thread.start()
        self.playwright = None
        self.browser = None
        self.page = None

    def call(self, coroutine, timeout):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError as exc:
            future.cancel()
            raise WebError("her browser did not answer in time") from exc

    # -- connection ------------------------------------------------------
    async def _attach(self):
        if self.browser is not None and self.browser.is_connected():
            return
        from playwright.async_api import async_playwright

        from core.computer_browser import BrowserError, _discover

        if self.prepare is not None:
            await asyncio.to_thread(self.prepare)
        try:
            port = await _discover(self.profile, 8)
        except BrowserError as exc:
            raise WebError(f"her browser has no automation port: {exc}") from exc
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}", timeout=8000
        )
        self.page = None
        self.opened = asyncio.Event()
        for context in self.browser.contexts:
            # Tabs register asynchronously, after the click that opened them returns.
            context.on("page", lambda _page: self.opened.set())

    def _pages(self):
        return [page for context in self.browser.contexts for page in context.pages]

    async def _current(self):
        await self._attach()
        pages = self._pages()
        if self.page is not None and self.page in pages and not self.page.is_closed():
            return self.page
        # Prefer the tab she can see: the one whose document is visible and focused.
        for page in reversed(pages):
            try:
                if await page.evaluate("document.visibilityState === 'visible' && document.hasFocus()"):
                    self.page = page
                    return page
            except Exception:
                continue
        if not pages:
            context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
            self.page = await context.new_page()
            return self.page
        self.page = pages[-1]
        return self.page

    async def _snapshot_text(self, page, limit):
        try:
            # The same ref-annotated snapshot Playwright MCP gives models.
            text = await page._impl_obj._channel.send("snapshotForAI", None, {"timeout": 5000})
        except Exception:
            text = await page.locator("body").aria_snapshot(timeout=5000)
        text = text if isinstance(text, str) else str(text)
        if len(text) > limit:
            text = text[:limit] + f"\n… snapshot truncated at {limit} characters"
        return text

    async def _state(self, page, limit):
        tabs = []
        for index, item in enumerate(self._pages()):
            try:
                title = await item.title()
            except Exception:
                title = ""
            tabs.append({"index": index, "title": title[:120], "url": item.url[:300], "current": item is page})
        return {
            "url": page.url,
            "title": await page.title(),
            "tabs": tabs,
            "snapshot": await self._snapshot_text(page, limit),
        }

    # -- operations --------------------------------------------------------
    def snapshot(self):
        async def run():
            page = await self._current()
            return await self._state(page, SNAPSHOT_CHARS)

        return self.call(run(), 30)

    def run(self, steps, cancelled):
        validate_steps(steps)
        return self.call(self._run(steps, cancelled), MAX_STEPS * MAX_STEP_TIMEOUT + 20)

    def _locator(self, page, target):
        if "ref" in target:
            locator = page.locator(f"aria-ref={target['ref']}")
        elif "role" in target:
            locator = page.get_by_role(
                target["role"], name=target.get("name"), exact=target.get("exact", True)
            ) if target.get("name") else page.get_by_role(target["role"])
        elif "label" in target:
            locator = page.get_by_label(target["label"], exact=target.get("exact", False))
        elif "placeholder" in target:
            locator = page.get_by_placeholder(target["placeholder"], exact=target.get("exact", False))
        elif "text" in target:
            locator = page.get_by_text(target["text"], exact=target.get("exact", False))
        else:
            locator = page.locator(target["selector"])
        return locator.nth(target["nth"]) if "nth" in target else locator

    async def _run(self, steps, cancelled):
        page = await self._current()
        starting_url = page.url
        recorded = []
        recordable = True
        results = []
        ok = True
        for index, step in enumerate(steps):
            if cancelled():
                results.append({"step": index + 1, "ok": False, "detail": STOPPED})
                ok = False
                break
            (kind,) = set(step) & set(KINDS)
            timeout = float(step.get("timeout", STEP_TIMEOUT)) * 1000
            before = len(self._pages())
            self.opened.clear()
            try:
                saved = copy.deepcopy(step)
                target = saved[kind]
                if kind == "wait_for":
                    target = target.get("target", target.get("gone"))
                if isinstance(target, dict) and "ref" in target:
                    stable = await self._recipe_target(page, target, timeout)
                    target.clear()
                    target.update(stable)
            except Exception:
                # Learning is optional; a missing old ref must not block normal input.
                recordable = False
            # Resolving a ref awaits the page, so a hold can land after the check
            # above. Never dispatch input that was held while we were resolving.
            if cancelled():
                results.append({"step": index + 1, "ok": False, "detail": STOPPED})
                ok = False
                break
            try:
                detail = await self._step(page, kind, step, timeout)
                if recordable:
                    recorded.append(saved)
                # A click that opened a popup or tab continues there (sign-in
                # flows). Give a popup a moment only when more steps follow.
                if kind == "click" and index < len(steps) - 1 and len(self._pages()) == before:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.opened.wait(), 0.3)
                pages = self._pages()
                if kind == "click" and len(pages) > before:
                    page = self.page = pages[-1]
                    await page.wait_for_load_state("domcontentloaded", timeout=timeout)
                    detail = (detail or "") + f"; opened a new tab, now on {page.url[:200]}"
                elif kind == "tab":
                    page = self.page
                results.append({"step": index + 1, "ok": True, **({"detail": detail} if detail else {})})
            except Exception as exc:
                message = str(exc).split("\n")[0][:400]
                results.append({"step": index + 1, "ok": False, "detail": message})
                ok = False
                break
        state = await self._state(page, RESULT_SNAPSHOT_CHARS)
        return {"ok": ok, "steps": results, **state,
                "_recipe_steps": recorded if ok else [],
                "_recipe_partial": ok and not recordable, "_starting_url": starting_url}

    async def _recipe_target(self, page, target, timeout):
        """Keep semantic identity, never snapshot-local refs or DOM positions."""
        locator = self._locator(page, target)
        candidates = []
        with contextlib.suppress(Exception):
            snapshot = await locator.aria_snapshot(timeout=timeout)
            match = re.match(r'^- ([a-z]+) ("(?:[^"\\]|\\.)*")', snapshot.strip())
            if match:
                candidates.append({"role": match[1], "name": json.loads(match[2]), "exact": True})
        with contextlib.suppress(Exception):
            labels = await locator.evaluate("""element => ({
                label: element.getAttribute('aria-label') ||
                    Array.from(element.labels || []).map(label => label.textContent).join(' ').trim(),
                text: (element.textContent || '').trim()
            })""", timeout=timeout)
            candidates.extend({key: labels[key], "exact": True} for key in ("label", "text")
                              if labels.get(key))
        for candidate in candidates:
            try:
                _target(candidate, "recipe")
                if await self._locator(page, candidate).count() == 1:
                    return candidate
            except Exception:
                continue
        raise WebError("recipe target has no unique semantic identity")

    async def _step(self, page, kind, step, timeout):
        body = step[kind]
        if kind == "goto":
            await page.goto(body, wait_until="domcontentloaded", timeout=timeout)
        elif kind == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=timeout)
        elif kind == "press":
            await page.keyboard.press(body)
        elif kind == "tab":
            pages = self._pages()
            if "index" in body:
                if not 0 <= int(body["index"]) < len(pages):
                    raise WebError("no tab with that index")
                chosen = pages[int(body["index"])]
            else:
                key, needle = next(iter(body.items()))
                chosen = None
                for item in pages:
                    haystack = item.url if key == "url_contains" else await item.title()
                    if needle.lower() in haystack.lower():
                        chosen = item
                if chosen is None:
                    raise WebError(f"no tab whose {key.split('_')[0]} contains {needle!r}")
            await chosen.bring_to_front()
            self.page = chosen
        elif kind == "wait_for":
            if "text" in body:
                await page.get_by_text(body["text"]).first.wait_for(state="visible", timeout=timeout)
            elif "url" in body:
                needle = body["url"]
                await page.wait_for_url(lambda url: needle in url, timeout=timeout)
            elif "target" in body:
                await self._locator(page, body["target"]).first.wait_for(state="visible", timeout=timeout)
            else:
                await self._locator(page, body["gone"]).first.wait_for(state="hidden", timeout=timeout)
        else:
            locator = self._locator(page, body)
            if kind == "click":
                await locator.click(timeout=timeout)
            elif kind == "fill":
                kind_of = await locator.get_attribute("type", timeout=timeout)
                if (kind_of or "").lower() == "password":
                    raise WebError("password fields are Raghav's: end with a handoff instead")
                await locator.fill(step["value"], timeout=timeout)
            elif kind == "select":
                try:
                    await locator.select_option(label=step["value"], timeout=timeout)
                except Exception:
                    await locator.select_option(value=step["value"], timeout=timeout)
            elif kind == "check":
                await locator.check(timeout=timeout)
            elif kind == "uncheck":
                await locator.uncheck(timeout=timeout)
            elif kind == "read":
                text = await locator.first.inner_text(timeout=timeout)
                return text[:READ_CHARS]
        return None

    def close(self):
        async def shutdown():
            # Detach only: stopping Playwright drops the CDP connection and
            # leaves her Edge and its tabs running.
            self.browser = None
            playwright, self.playwright = self.playwright, None
            if playwright is not None:
                with contextlib.suppress(Exception):
                    await playwright.stop()

        with contextlib.suppress(Exception):
            self.call(shutdown(), 10)
        self.loop.call_soon_threadsafe(self.loop.stop)
