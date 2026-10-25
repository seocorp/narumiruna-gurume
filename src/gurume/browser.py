"""Explicit headed-browser retrieval; never an automatic HTTP fallback."""

import contextlib
import os
import shutil
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .restaurant import _is_area_not_found

BROWSER_COMMANDS = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")
CHALLENGE_CLEARED = "() => document.readyState !== 'loading' && !('_cf_chl_opt' in window)"


@dataclass(frozen=True)
class BrowserDocument:
    """A main-frame document read in the headed browser."""

    url: str
    status: int
    html: str


class BrowserRetrievalError(RuntimeError):
    """Browser retrieval failed, optionally with an upstream HTTP status."""

    def __init__(self, message: str, status: int | None = None, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = 500 <= status < 600 if status is not None else retryable


def validate_search_document(html: str, status: int) -> None:
    """Reject challenges and unknown documents instead of reporting no results."""
    soup = BeautifulSoup(html, "html.parser")
    if (
        status == 403
        or soup.select_one("#challenge-form, #cf-challenge-running")
        or (soup.title and soup.title.get_text(strip=True) in {"Just a moment...", "Attention Required! | Cloudflare"})
    ):
        raise BrowserRetrievalError("Browser verification required; manual browser access is needed.", status=403)
    if status >= 400:
        raise BrowserRetrievalError(f"HTTP Error {status}: Browser search failed", status=status)
    if not _is_area_not_found(soup, html) and not soup.select_one(
        ".list-rst, .js-rst-cassette-wrap, .rstlist-notfound, .c-page-count"
    ):
        raise RuntimeError("Browser did not return a recognizable restaurant search document")


def _is_transient_navigation_error(error: Exception, timeout_error: type[Exception]) -> bool:
    return isinstance(error, timeout_error) or any(
        code in str(error)
        for code in (
            "net::ERR_CONNECTION_RESET",
            "net::ERR_CONNECTION_CLOSED",
            "net::ERR_CONNECTION_REFUSED",
            "net::ERR_CONNECTION_ABORTED",
            "net::ERR_CONNECTION_TIMED_OUT",
            "net::ERR_TIMED_OUT",
            "net::ERR_NETWORK_CHANGED",
            "net::ERR_INTERNET_DISCONNECTED",
            "net::ERR_NAME_NOT_RESOLVED",
            "net::ERR_ADDRESS_UNREACHABLE",
        )
    )


def _launch_options() -> dict[str, Any]:
    """Launch an installed Chrome or Chromium as an ordinary browser.

    Cloudflare answers Tabelog pages with a managed challenge that an ordinary browser clears by itself within
    seconds, but keeps challenging a browser that reports `navigator.webdriver`, which Playwright's default
    `--enable-automation` flag and the `AutomationControlled` Blink feature set. Without an installed browser,
    Playwright falls back to its own Chromium.
    """
    executable = os.getenv("GURUME_BROWSER_EXECUTABLE") or next(filter(None, map(shutil.which, BROWSER_COMMANDS)), None)
    return {
        "headless": False,
        "executable_path": executable,
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled"],
    }


def _is_challenge(response: Any) -> bool:
    return response.headers.get("cf-mitigated") == "challenge"


async def _wait_for_challenge(page: Any, api: Any, navigation_timeout: float) -> None:
    """Wait for the browser to clear the challenge and load the requested document; a timeout leaves the challenge."""
    with contextlib.suppress(api.TimeoutError):
        await page.wait_for_function(CHALLENGE_CLEARED, timeout=navigation_timeout * 1000)


async def _navigate_search_document(page: Any, api: Any, url: str, navigation_timeout: float) -> tuple[str, str]:
    """Recover a timed-out navigation only with current main-frame response evidence."""
    document_status: int | None = None

    def remember_response(response: Any) -> None:
        nonlocal document_status
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            document_status = response.status

    page.on("response", remember_response)
    try:
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout * 1000)
        except api.TimeoutError as timeout:
            if document_status is None:
                raise
            html = await page.content()
            try:
                validate_search_document(html, document_status)
            except BrowserRetrievalError:
                raise
            except RuntimeError:
                raise timeout from None
            return html, page.url
        if response is None:
            raise RuntimeError("Browser navigation returned no document response")
        if _is_challenge(response):
            await _wait_for_challenge(page, api, navigation_timeout)
        html = await page.content()
    except api.Error as error:
        raise BrowserRetrievalError(
            f"Browser navigation failed: {error}",
            retryable=_is_transient_navigation_error(error, api.TimeoutError),
        ) from error
    # After a cleared challenge the latest main-frame response, not the challenge, describes the document.
    validate_search_document(html, document_status if document_status is not None else response.status)
    return html, page.url


async def _read_document(page: Any, api: Any, url: str, navigation_timeout: float) -> BrowserDocument:
    document_status: int | None = None

    def remember_response(response: Any) -> None:
        nonlocal document_status
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            document_status = response.status

    page.on("response", remember_response)
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout * 1000)
        if response is None:
            raise RuntimeError("Browser navigation returned no document response")
        if _is_challenge(response):
            await _wait_for_challenge(page, api, navigation_timeout)
        html = await page.content()
    except api.Error as error:
        raise BrowserRetrievalError(
            f"Browser navigation failed: {error}",
            retryable=_is_transient_navigation_error(error, api.TimeoutError),
        ) from error
    finally:
        page.remove_listener("response", remember_response)
    return BrowserDocument(page.url, document_status if document_status is not None else response.status, html)


def _import_playwright() -> Any:
    try:
        return import_module("playwright.async_api")
    except ImportError as error:
        raise RuntimeError(
            "Browser support is not installed: install gurume[browser]. Gurume uses an installed Chrome or "
            "Chromium; without one, run playwright install chromium"
        ) from error


def _prepare_profile() -> Path:
    try:
        profile = Path.home() / ".cache" / "gurume" / "browser"
        profile.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BrowserRetrievalError(f"Cannot prepare Gurume browser profile: {error}") from error
    return profile


async def fetch_search_document(url: str, navigation_timeout: float) -> tuple[str, str]:
    """Use a dedicated local profile, preserving cookies only within that browser."""
    api = _import_playwright()
    profile = _prepare_profile()
    try:
        async with api.async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(str(profile), **_launch_options())
            try:
                page = await context.new_page()
                return await _navigate_search_document(page, api, url, navigation_timeout)
            finally:
                await context.close()
    except api.Error as error:
        raise RuntimeError(f"Headed browser retrieval failed: {error}") from error


async def fetch_documents(urls: list[str], navigation_timeout: float) -> list[BrowserDocument]:
    """Read documents in order in one page of the dedicated profile, then close the browser."""
    api = _import_playwright()
    profile = _prepare_profile()
    try:
        async with api.async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(str(profile), **_launch_options())
            try:
                page = await context.new_page()
                return [await _read_document(page, api, url, navigation_timeout) for url in urls]
            finally:
                await context.close()
    except api.Error as error:
        raise RuntimeError(f"Headed browser retrieval failed: {error}") from error
