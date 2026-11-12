"""
Browser automation via Playwright.

Runs a single persistent headed Chromium instance owned by this agent
(independent of the in-app holographic BrowserAgent and the separate
local-agent.js Playwright server on :3001).

Capabilities: open/navigate, new/close tabs, search, click, type, fill forms,
back/forward, scroll. Lazy-initialized; robust to closed pages.
"""

from __future__ import annotations

import asyncio
import re
import threading
from typing import Any, Dict, Optional
from urllib.parse import quote_plus, urlsplit

from .registry import STATE, ToolError, register

# A dedicated event loop + thread runs all Playwright coroutines, because
# Playwright's sync API can deadlock under FastAPI's threadpool. We use the
# async API marshalled through a single loop.
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_LOOP_THREAD: Optional[threading.Thread] = None
_LOOP_LOCK = threading.Lock()


def _get_loop() -> "asyncio.AbstractEventLoop":
    global _LOOP, _LOOP_THREAD
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            _LOOP = asyncio.new_event_loop()
            _LOOP_THREAD = threading.Thread(target=_run_loop, daemon=True)
            _LOOP_THREAD.start()
        return _LOOP


def _run_loop() -> None:
    loop = _LOOP
    assert loop is not None
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        try:
            loop.close()
        except Exception:
            pass


def _run(coro):
    """Submit a coroutine to the dedicated Playwright loop and block on it."""
    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=60)


# --- Async Playwright lifecycle ---------------------------------------------


async def _ensure_browser_async() -> Any:
    if STATE.page is not None:
        try:
            # Health check: a cheap op; if the page died, recreate.
            _ = STATE.page.url
            return STATE.page
        except Exception:
            STATE.reset_playwright()

    if STATE.playwright is None:
        from playwright.async_api import async_playwright

        STATE.playwright = await async_playwright().start()

    if STATE.browser is None:
        STATE.browser = await STATE.playwright.chromium.launch(
            headless=False,
            args=["--start-maximized", "--no-sandbox"],
        )
        STATE.context = await STATE.browser.new_context(viewport=None)

    if STATE.context is None:
        STATE.context = await STATE.browser.new_context(viewport=None)

    pages = STATE.context.pages
    if pages:
        STATE.page = pages[-1]
    else:
        STATE.page = await STATE.context.new_page()
    return STATE.page


async def _page() -> Any:
    return await _ensure_browser_async()


async def _page_snapshot(page: Any, max_chars: int = 6000) -> Dict[str, str]:
    """Return concise visible page text so MYRAA can report what it opened."""
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        text = await page.locator("body").inner_text(timeout=5000)
    except Exception as exc:  # noqa: BLE001
        text = f"Page text is unavailable: {exc}"
    limit = max(500, min(10000, int(max_chars)))
    return {"title": title[:300], "url": page.url, "page_text": text[:limit]}


def _normalize_url(raw: str) -> str:
    url = raw.strip()
    if not url:
        raise ToolError("Empty URL.")
    if url == "about:blank":
        return url
    scheme_match = re.match(r"^([a-z][a-z0-9+.-]*):", url, flags=re.IGNORECASE)
    if scheme_match and scheme_match.group(1).lower() not in {"http", "https"}:
        if not re.match(r"^[a-z0-9.-]+:\d+(?:/|$)", url, flags=re.IGNORECASE):
            raise ToolError("Only regular HTTP(S) websites can be opened in MYRAA's browser.")
        url = "https://" + url
    elif not scheme_match:
        url = "https://" + url
    elif not url.lower().startswith(("http://", "https://")):
        raise ToolError("Specify a complete HTTP(S) URL.")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("Only regular HTTP(S) websites can be opened in MYRAA's browser.")
    if parsed.username or parsed.password:
        raise ToolError("URLs containing embedded credentials are not allowed.")
    return url


# --- Handlers ---------------------------------------------------------------


@register("desktopBrowserOpen")
async def browser_open(args: Dict[str, Any]) -> Dict[str, Any]:
    url = _normalize_url(args.get("url") or "https://www.google.com")
    page = await _page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not open {url}: {e}")
    return {"result": f"Opened {url} in the automation browser.", **(await _page_snapshot(page))}


@register("desktopBrowserNavigate")
async def browser_navigate(args: Dict[str, Any]) -> Dict[str, Any]:
    # Alias of desktopBrowserOpen, retained for clarity.
    return await browser_open(args)


@register("desktopBrowserOpenTab")
async def browser_open_tab(args: Dict[str, Any]) -> Dict[str, Any]:
    url = _normalize_url(args.get("url") or "about:blank")
    await _ensure_browser_async()
    ctx = STATE.context
    page = await ctx.new_page()
    STATE.page = page  # make it active
    if url != "about:blank":
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Opened tab but navigation failed: {e}")
    return {"result": f"New tab opened at {url}.", **(await _page_snapshot(page))}


@register("desktopBrowserCloseTab")
async def browser_close_tab(args: Dict[str, Any]) -> Dict[str, Any]:
    page = await _page()
    try:
        await page.close()
    except Exception:
        pass
    pages = STATE.context.pages if STATE.context else []
    STATE.page = pages[-1] if pages else None
    if STATE.page is None:
        return {"result": "Closed the last tab; browser now empty."}
    return {"result": f"Closed tab. Active tab now: {STATE.page.url}"}


@register("desktopBrowserSearch")
async def browser_search(args: Dict[str, Any]) -> Dict[str, Any]:
    query = args.get("query") or args.get("q")
    engine = (args.get("engine") or "google").strip().lower()
    if not query:
        raise ToolError("Parameter 'query' is required.")
    q = quote_plus(str(query))
    url = {
        "google": f"https://www.google.com/search?q={q}",
        "youtube": f"https://www.youtube.com/results?search_query={q}",
        "github": f"https://github.com/search?q={q}",
        "duckduckgo": f"https://duckduckgo.com/?q={q}",
        "bing": f"https://www.bing.com/search?q={q}",
    }.get(engine)
    if not url:
        raise ToolError(f"Unsupported engine '{engine}'.")
    page = await _page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Search navigation failed: {e}")
    return {"result": f"Searched {engine} for '{query}'.", **(await _page_snapshot(page))}


@register("desktopBrowserClick")
async def browser_click(args: Dict[str, Any]) -> Dict[str, Any]:
    selector = args.get("selector")
    text = args.get("text")
    page = await _page()
    original_page_count = len(STATE.context.pages) if STATE.context else 0
    try:
        if selector:
            try:
                await page.click(selector, timeout=5000)
            except Exception:
                await page.get_by_text(str(selector), exact=False).first.click(timeout=5000)
        elif text:
            label = str(text)
            link = page.get_by_role("link", name=label, exact=False)
            button = page.get_by_role("button", name=label, exact=False)
            if await link.count():
                await link.first.click(timeout=5000)
            elif await button.count():
                await button.first.click(timeout=5000)
            else:
                await page.get_by_text(label, exact=False).first.click(timeout=5000)
        else:
            raise ToolError("Provide 'selector' or 'text' to click.")
        await page.wait_for_timeout(700)
        pages = STATE.context.pages if STATE.context else []
        if len(pages) > original_page_count:
            page = pages[-1]
            STATE.page = page
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Click failed: {e}")
    return {"result": f"Clicked {selector or text}.", **(await _page_snapshot(page))}


@register("desktopBrowserReadPage")
async def browser_read_page(args: Dict[str, Any]) -> Dict[str, Any]:
    """Read the current assistant-browser page title and visible text."""
    page = await _page()
    max_chars = args.get("max_chars", 6000)
    try:
        return {"result": "Read the currently open browser page.", **(await _page_snapshot(page, max_chars))}
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not read the current page: {e}")


@register("desktopBrowserType")
async def browser_type(args: Dict[str, Any]) -> Dict[str, Any]:
    text = args.get("text")
    selector = args.get("selector")
    clear_first = bool(args.get("clear", True))
    if not text:
        raise ToolError("Parameter 'text' is required.")
    page = await _page()
    try:
        if selector:
            await page.fill(selector, str(text), timeout=5000)
        else:
            if clear_first:
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Delete")
            await page.keyboard.type(str(text))
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Type failed: {e}")
    return {"result": f"Typed {len(str(text))} characters."}


@register("desktopBrowserFillForm")
async def browser_fill_form(args: Dict[str, Any]) -> Dict[str, Any]:
    """Fill multiple fields. fields = { selector: value, ... }"""
    fields = args.get("fields")
    submit = args.get("submit")  # optional selector to click after filling
    if not isinstance(fields, dict) or not fields:
        raise ToolError("Parameter 'fields' (object of selector->value) is required.")
    page = await _page()
    filled = 0
    try:
        for sel, val in fields.items():
            await page.fill(str(sel), str(val), timeout=5000)
            filled += 1
        if submit:
            await page.click(str(submit), timeout=5000)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Form fill failed after {filled} field(s): {e}")
    extra = " and submitted." if submit else "."
    return {"result": f"Filled {filled} field(s){extra}"}


@register("desktopBrowserGoBack")
async def browser_go_back(args: Dict[str, Any]) -> Dict[str, Any]:
    page = await _page()
    try:
        await page.go_back(timeout=15000)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Back failed: {e}")
    return {"result": f"Went back. Now on {page.url}."}


@register("desktopBrowserGoForward")
async def browser_go_forward(args: Dict[str, Any]) -> Dict[str, Any]:
    page = await _page()
    try:
        await page.go_forward(timeout=15000)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Forward failed: {e}")
    return {"result": f"Went forward. Now on {page.url}."}


@register("desktopBrowserScroll")
async def browser_scroll(args: Dict[str, Any]) -> Dict[str, Any]:
    direction = (args.get("direction") or "down").lower()
    amount = int(args.get("amount", 500))
    delta = amount if direction != "up" else -amount
    page = await _page()
    try:
        await page.mouse.wheel(0, delta)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Scroll failed: {e}")
    return {"result": f"Scrolled {direction} {amount}px."}


# Wrap the async handlers so FastAPI's sync threadpool path can call them.
# Each @register'd async function above is replaced by a sync wrapper below.
def _sync_wrap(async_fn):
    def wrapper(args: Dict[str, Any]) -> Dict[str, Any]:
        return _run(async_fn(args))

    wrapper.__name__ = async_fn.__name__
    wrapper.__doc__ = async_fn.__doc__
    return wrapper


# Re-register the async handlers as synchronous wrappers so the registry
# dispatcher (which is sync) can call them uniformly.
from .registry import TOOLS  # noqa: E402

for _name in [
    "desktopBrowserOpen",
    "desktopBrowserNavigate",
    "desktopBrowserOpenTab",
    "desktopBrowserCloseTab",
    "desktopBrowserSearch",
    "desktopBrowserClick",
    "desktopBrowserReadPage",
    "desktopBrowserType",
    "desktopBrowserFillForm",
    "desktopBrowserGoBack",
    "desktopBrowserGoForward",
    "desktopBrowserScroll",
]:
    _orig = TOOLS[_name]
    if asyncio.iscoroutinefunction(_orig):
        TOOLS[_name] = _sync_wrap(_orig)


def shutdown_browser() -> None:
    """Cleanly stop the Playwright browser (called on app shutdown)."""
    if STATE.browser is None:
        return

    async def _stop():
        try:
            if STATE.browser:
                await STATE.browser.close()
        except Exception:
            pass
        try:
            if STATE.playwright:
                await STATE.playwright.stop()
        except Exception:
            pass
        STATE.reset_playwright()

    try:
        _run(_stop())
    except Exception:
        STATE.reset_playwright()


__all__ = [
    "browser_open",
    "browser_navigate",
    "browser_open_tab",
    "browser_close_tab",
    "browser_search",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_go_back",
    "browser_go_forward",
    "browser_scroll",
    "shutdown_browser",
]
