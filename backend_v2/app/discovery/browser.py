"""Playwright adapter for the portal walk.

The walk itself is deterministic code and is tested against a fake page. This
module is the only place that imports Playwright, and it imports it lazily so
the base can boot without the package installed.
"""

from __future__ import annotations

import logging
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from app.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class BrowserDriverUnavailable(RuntimeError):
    """Playwright or a browser binary is missing."""


class SessionLost(RuntimeError):
    """The browser session a job owned is gone.

    The portal window was closed, the process restarted, or the driver dropped
    the connection. A lost session is never retried: the page it was holding
    cannot come back, so the only honest next step is a new attempt started by
    the user.
    """


SESSION_LOST_NOTE = (
    "The browser window this job was using was closed, so its session is gone. "
    "What was already read or attached was kept, but nothing further could be "
    "fetched. Start it again to continue; a closed window is not retried "
    "automatically."
)

#: While the software is reading the portal the window sits in the taskbar, so
#: it neither steals the screen nor invites a stray click. ``_WORKING_VIEWPORT``
#: is only the fallback for a browser that has no window to minimize (headless,
#: or a CDP refusal); a challenge expands the window to ``_HUMAN_VIEWPORT``, a
#: size a person can actually read and answer in.
_WORKING_VIEWPORT = {"width": 480, "height": 360}
_HUMAN_VIEWPORT = {"width": 1280, "height": 920}

#: The cover is a pair of pseudo-elements, not a DOM node, so it never appears
#: in ``inner_text`` and cannot leak into field extraction or challenge
#: detection. ``pointer-events: none`` keeps the automation's own clicks
#: working through it.
_WORKING_STYLE = """
html::before {
  content: "";
  position: fixed;
  inset: 0;
  z-index: 2147483646;
  background-color: rgba(9, 13, 20, 0.55);
  background-image: linear-gradient(
    110deg,
    rgba(255, 255, 255, 0) 38%,
    rgba(124, 196, 255, 0.16) 50%,
    rgba(255, 255, 255, 0) 62%
  );
  background-size: 260% 100%;
  animation: bidkaro-sweep 2.2s linear infinite;
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  pointer-events: none;
}
html::after {
  content: "BidKaro is reading the portal. The window is minimized; it will open here if it needs an answer.";
  position: fixed;
  inset: 0;
  z-index: 2147483647;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 18px;
  box-sizing: border-box;
  text-align: center;
  font: 600 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  color: #e8eef8;
  pointer-events: none;
}
@keyframes bidkaro-sweep {
  from { background-position: 130% 0; }
  to { background-position: -130% 0; }
}
"""

#: Injected before every page script, so a navigation cannot uncover the portal
#: mid-walk. The flag lives in ``sessionStorage`` because it survives a
#: navigation in the same tab, which is exactly how long the cover must last.
_WORKING_OVERLAY_SCRIPT = """
(() => {
  if (window.top !== window) return;
  window.__bidkaroCover = function () {
    let covered = true;
    try {
      covered = sessionStorage.getItem('bidkaro-working') !== '0';
    } catch (e) {
      covered = true;
    }
    if (!covered) return;
    if (document.getElementById('bidkaro-working-style')) return;
    const style = document.createElement('style');
    style.id = 'bidkaro-working-style';
    style.textContent = `%s`;
    (document.head || document.documentElement).appendChild(style);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', window.__bidkaroCover);
  } else {
    window.__bidkaroCover();
  }
})();
""" % _WORKING_STYLE

_COVER_SCRIPT = """
() => {
  try { sessionStorage.setItem('bidkaro-working', '1'); } catch (e) {}
  if (window.__bidkaroCover) window.__bidkaroCover();
  return true;
}
"""

_UNCOVER_SCRIPT = """
() => {
  try { sessionStorage.setItem('bidkaro-working', '0'); } catch (e) {}
  const style = document.getElementById('bidkaro-working-style');
  if (style) style.remove();
  return true;
}
"""

#: Messages Playwright and greenlet use when the page, context, browser or
#: connection behind an object has ended. A session is only lost on one of
#: these; an ordinary page error is not a lost session.
_CLOSED_MARKERS = (
    "has been closed",
    "connection closed",
    "event loop is closed",
    "cannot switch to a different thread",
    "target closed",
)


def is_closed_error(exc: BaseException) -> bool:
    """True when ``exc`` says the browser behind the call no longer exists."""

    if type(exc).__name__ == "TargetClosedError":
        return True
    text = str(exc).casefold()
    return any(marker in text for marker in _CLOSED_MARKERS)


@dataclass(slots=True)
class PageSnapshot:
    url: str
    title: str
    text: str
    html: str
    screenshot: bytes | None = None

    @property
    def is_blank(self) -> bool:
        return not self.text.strip() and not self.html.strip()


@dataclass(slots=True)
class ChallengeMarker:
    kind: str
    selector: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "selector": self.selector,
            "message": self.message,
        }


@dataclass(slots=True)
class DownloadResult:
    ok: bool
    content: bytes = b""
    content_type: str = ""
    size: int = 0
    error: str | None = None
    is_page: bool = False
    text: str = ""
    html: str = ""


class BrowserDriver:
    """The browser surface the walk uses.

    A subclass or a test double provides ``_page`` (something with Playwright's
    ``evaluate``/``click``/``goto``/``go_back``/``wait_for_timeout`` methods),
    ``open``, ``snapshot`` and ``find_challenge``.
    """

    _page: Any | None = None
    #: Set by a driver whose session has ended, so the handler can report a
    #: closed window instead of a per-file error. A driver that swallows an
    #: error still has to say the session is gone.
    lost: bool = False
    lost_reason: str = ""

    def available(self) -> bool:
        """Whether this driver still has a page it can use."""

        return not self.lost and self._page is not None

    def open(self, url: str) -> PageSnapshot:  # pragma: no cover - interface
        raise NotImplementedError

    def snapshot(self) -> PageSnapshot:  # pragma: no cover - interface
        raise NotImplementedError

    def find_challenge(self) -> ChallengeMarker | None:  # pragma: no cover
        raise NotImplementedError

    def submit_answer(self, answer: str) -> PageSnapshot:  # pragma: no cover
        raise NotImplementedError

    def download_in_page(
        self, url: str, *, max_bytes: int = 25 * 1024 * 1024
    ) -> DownloadResult:  # pragma: no cover - interface
        raise NotImplementedError

    def watch_for_download(self) -> None:  # pragma: no cover - interface
        """Start watching the visible page for the file the human's answer returns."""

    def take_download(self) -> DownloadResult | None:  # pragma: no cover - interface
        """The file the human's answer produced, if it has arrived."""

    def has_pending_download(self) -> bool:  # pragma: no cover - interface
        """Whether the page has already handed over a file nothing has read."""

        return False

    def wait_for_download(self, timeout_seconds: float = 5.0) -> bool:
        """Let the browser deliver an event that arrived while it was idle.

        A parked session is not running the page's event loop, so a download
        the portal produced after the human answered is only handed over when
        the session is pumped again. Returns True when something is waiting.
        """

        return False

    def screenshot(self) -> bytes | None:  # pragma: no cover - interface
        raise NotImplementedError

    def bring_to_front(self) -> None:  # pragma: no cover - interface
        """Raise the visible window so the human can answer a challenge."""

    def begin_work(self) -> None:  # pragma: no cover - interface
        """Make the window unobtrusive while the software reads the portal."""

    def reveal(self) -> None:  # pragma: no cover - interface
        """Show the page and make the window usable for the human's answer."""

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


#: A challenge is only a challenge when the page actually asks for it. A login
#: form in the site chrome is not enough; otherwise every page looks blocked.
_CHALLENGE_SCRIPT = """
() => {
    const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 8 && r.height > 8 && s.visibility !== 'hidden' &&
            s.display !== 'none';
    };
    const text = (document.body ? document.body.innerText : '').toLowerCase();
    const picture = document.querySelector(
        'img#captchaImage, img[id*="captcha" i], img[src*="captcha" i]'
    );
    const answer = document.querySelector(
        'input[name*="captcha" i], input[id*="captcha" i], input[name*="otp" i]'
    );
    if (picture && answer && visible(picture) && visible(answer)) {
        return {
            kind: 'captcha',
            selector: answer.id ? '#' + answer.id : 'input[name="' + answer.name + '"]',
            message: 'The portal asks for the characters in the picture.'
        };
    }
    const blocked = text.includes('access denied') ||
        text.includes('request blocked') ||
        text.includes('unusual traffic') ||
        text.includes('complete the security check') ||
        text.includes('session in the client area has expired');
    if (blocked) {
        return {kind: 'blocked', selector: null,
                message: 'The portal stopped this request before the page was read.'};
    }
    return null;
}
"""


class LocalBrowserDriver(BrowserDriver):
    """A headed or headless local Chromium owned by this process."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        headless: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.headless = (
            self.settings.browser_headless if headless is None else bool(headless)
        )
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._cdp = None
        self._window_id: int | None = None
        self._watching = False
        self._downloads: deque[bytes] = deque()
        self._download_error: str | None = None
        self._bound_pages: set[int] = set()
        self.lost = False
        self.lost_reason = ""
        self._closing = False
        #: Other callers (a refresh) want the window as it always was. The walk
        #: and the fetch switch this off with ``begin_work``.
        self._human_mode = True

    def __enter__(self) -> "LocalBrowserDriver":
        self.start()
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()

    def start(self) -> None:
        self._closing = False
        self._human_mode = True
        self._cdp = None
        self._window_id = None
        self.lost = False
        self.lost_reason = ""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on install
            raise BrowserDriverUnavailable(
                "Playwright is not installed. Run 'pip install playwright' and "
                "'playwright install chromium'."
            ) from exc

        self._playwright = sync_playwright().start()
        launch: dict[str, Any] = {"headless": self.headless}
        if self.settings.browser_proxy_server.strip():
            launch["proxy"] = {"server": self.settings.browser_proxy_server.strip()}
        try:
            self._browser = self._playwright.chromium.launch(**launch)
        except Exception as exc:  # pragma: no cover - depends on install
            self._playwright.stop()
            self._playwright = None
            raise BrowserDriverUnavailable(
                "Chromium could not be started. Run 'playwright install chromium'."
            ) from exc
        self._context = self._browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.settings.browser_timeout_ms)
        # Re-applied on every navigation: a page load must not uncover the
        # portal while the walk is still reading it.
        self._page.add_init_script(_WORKING_OVERLAY_SCRIPT)
        self._context.on("page", self._bind_downloads)

    def _require_page(self):
        if self._page is None:
            raise BrowserDriverUnavailable("The browser session is not running.")
        return self._page

    def _note_lost(self, exc: BaseException) -> None:
        """Record that the session behind this driver has ended.

        The session is lost once, not on every later call, and never because
        the software itself closed the window.
        """

        if self._closing or self.lost or not is_closed_error(exc):
            return
        self.lost = True
        self.lost_reason = str(exc)[:300]
        logger.warning("Browser session lost: %s", self.lost_reason)

    def open(self, url: str) -> PageSnapshot:
        page = self._require_page()
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.settings.browser_timeout_ms,
            )
        except Exception as exc:
            self._note_lost(exc)
            raise
        # A headed browser can open behind the editor. Only raise it when the
        # page is for the human; while the software is working the window stays
        # out of the way.
        if self._human_mode:
            self.bring_to_front()
        return self.snapshot()

    def bring_to_front(self) -> None:
        page = self._page
        if page is None:
            return
        try:
            page.bring_to_front()
        except Exception as exc:
            # A headless browser has no window to raise, but a closed one is
            # a lost session.
            self._note_lost(exc)

    def begin_work(self) -> None:
        """Minimize the window while the software reads the portal.

        A walk can take minutes, so the browser is put in the taskbar instead
        of sitting on the screen. The page is also covered (a blur plus a
        moving sheen) so that a person who restores the window mid-walk sees
        the loading state, not the portal. If the window cannot be minimized,
        it is shrunk instead, so it never takes the screen either way.
        """

        self._human_mode = False
        if not self._minimize():
            self._resize(_WORKING_VIEWPORT)
        self._cover()

    def reveal(self) -> None:
        """Uncover the page and make the window readable for the human.

        Called only when the portal has asked for an answer, so the window is
        restored, enlarged and raised. The human types into the portal's own
        page; the software never sees the answer.
        """

        self._human_mode = True
        self._restore()
        self._resize(_HUMAN_VIEWPORT)
        self._uncover()
        self.bring_to_front()

    def _window(self) -> tuple[Any, int]:
        """The CDP handle for this window, created once per session."""

        if self._cdp is not None and self._window_id is not None:
            return self._cdp, self._window_id
        page = self._require_page()
        cdp = page.context.new_cdp_session(page)
        window_id = int(cdp.send("Browser.getWindowForTarget")["windowId"])
        self._cdp = cdp
        self._window_id = window_id
        return cdp, window_id

    def _minimize(self) -> bool:
        """Put the window in the taskbar. False when it cannot be done."""

        try:
            cdp, window_id = self._window()
            cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "minimized"}},
            )
            return True
        except Exception as exc:
            # A headless browser has no window to minimize; that is not a lost
            # session, it just means the fallback (shrink) is used.
            self._note_lost(exc)
            return False

    def _restore(self) -> None:
        """Bring the window back from the taskbar before showing it."""

        try:
            cdp, window_id = self._window()
            cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "normal"}},
            )
        except Exception as exc:
            self._note_lost(exc)

    def _resize(self, size: dict[str, int]) -> None:
        page = self._page
        if page is None:
            return
        try:
            page.set_viewport_size(size)
        except Exception as exc:
            self._note_lost(exc)

    def _cover(self) -> None:
        page = self._page
        if page is None:
            return
        try:
            page.evaluate(_COVER_SCRIPT)
        except Exception as exc:
            self._note_lost(exc)

    def _uncover(self) -> None:
        page = self._page
        if page is None:
            return
        try:
            page.evaluate(_UNCOVER_SCRIPT)
        except Exception as exc:
            self._note_lost(exc)

    def snapshot(self) -> PageSnapshot:
        page = self._require_page()
        try:
            text = page.inner_text("body")
        except Exception as exc:
            self._note_lost(exc)
            text = ""
        try:
            title = page.title()
        except Exception as exc:
            self._note_lost(exc)
            title = ""
        try:
            html = page.content()
        except Exception as exc:
            self._note_lost(exc)
            html = ""
        try:
            url = page.url
        except Exception as exc:
            self._note_lost(exc)
            url = ""
        return PageSnapshot(url=url, title=title, text=text, html=html)

    def find_challenge(self) -> ChallengeMarker | None:
        page = self._page
        if page is None:
            return None
        try:
            found = page.evaluate(_CHALLENGE_SCRIPT)
        except Exception as exc:
            self._note_lost(exc)
            return None
        if not isinstance(found, dict):
            return None
        kind = str(found.get("kind") or "").strip()
        if not kind:
            return None
        return ChallengeMarker(
            kind=kind,
            selector=found.get("selector"),
            message=str(found.get("message") or ""),
        )

    def submit_answer(self, answer: str) -> PageSnapshot:
        """Carry a human's answer into the portal's own form, once.

        The answer is typed into the page and submitted with the portal's own
        control, then dropped: it is never stored, logged or sent anywhere but
        the form. The method returns after giving the portal a few seconds to
        hand over the file or to draw a new picture.
        """

        marker = self.find_challenge()
        if marker is None or not marker.selector:
            raise BrowserDriverUnavailable("There is no challenge on this page.")
        page = self._require_page()
        try:
            page.fill(marker.selector, answer)
            submitted = False
            for selector in (
                "#Submit",
                "input[type=submit]",
                "button[type=submit]",
                "button:has-text('Submit')",
            ):
                try:
                    if page.locator(selector).count():
                        page.click(selector, no_wait_after=True)
                        submitted = True
                        break
                except Exception:  # noqa: BLE001 - try the next control
                    continue
            if not submitted:
                # The portal's form may have no button; Enter submits it.
                try:
                    page.press(marker.selector, "Enter")
                except Exception:
                    page.evaluate(
                        """() => {
                            const form = document.querySelector('form');
                            if (form) form.submit();
                        }"""
                    )
            # Pump the page so the answer's result (a download or a new
            # picture) is delivered before the caller decides what happened.
            # The portal prepares a document on its side, so four seconds was
            # short enough that a correct answer looked rejected and the user
            # was asked for a new picture. The queue is not cleared here: a
            # file that arrived after an earlier answer is still that answer's
            # file and must be attached, not discarded.
            self._watching = True
            self._bind_downloads(page)
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                if self._downloads or self._download_error:
                    break
                try:
                    page.wait_for_timeout(250)
                except Exception as exc:  # noqa: BLE001 - navigation is not loss
                    self._note_lost(exc)
                    if self.lost:
                        raise
                    break
        except Exception as exc:
            self._note_lost(exc)
            raise
        return self.snapshot()

    def download_in_page(
        self, url: str, *, max_bytes: int = 25 * 1024 * 1024
    ) -> DownloadResult:
        """Fetch a file through the page's own cookies and session."""

        page = self._require_page()
        absolute = urljoin(page.url, url)
        try:
            result = page.evaluate(
                """async (url) => {
                    const response = await fetch(url, {credentials: 'include'});
                    const type = response.headers.get('content-type') || '';
                    const buffer = await response.arrayBuffer();
                    const bytes = Array.from(new Uint8Array(buffer));
                    return {ok: response.ok, status: response.status,
                            type, bytes};
                }""",
                absolute,
            )
        except Exception as exc:  # noqa: BLE001 - report a failed fetch
            self._note_lost(exc)
            return DownloadResult(ok=False, error=str(exc))
        if not isinstance(result, dict):
            return DownloadResult(ok=False, error="The file request returned no data.")
        content = bytes(result.get("bytes") or [])
        content_type = str(result.get("type") or "")
        if len(content) > max_bytes:
            return DownloadResult(
                ok=False,
                error="The file is larger than the download limit.",
                content_type=content_type,
            )
        looks_like_page = "text/html" in content_type.lower()
        return DownloadResult(
            ok=bool(result.get("ok")) and not looks_like_page,
            content=content,
            content_type=content_type,
            size=len(content),
            is_page=looks_like_page,
            text="",
            html=content.decode("utf-8", errors="replace") if looks_like_page else "",
        )

    def watch_for_download(self) -> None:
        """Watch the page for the file the portal hands over after an answer.

        The human's answer is carried into the page by ``submit_answer`` and
        then dropped. This listener only notices the file the portal returns;
        the callback runs on the thread that owns the browser, so the bytes are
        copied out of the temporary file there.
        """

        self._downloads.clear()
        self._download_error = None
        self._watching = True
        if self._page is not None:
            self._bind_downloads(self._page)

    def has_pending_download(self) -> bool:
        """Whether a file is waiting in the queue or failed to be saved."""

        return bool(self._downloads) or self._download_error is not None

    def _bind_downloads(self, page) -> None:
        if id(page) in self._bound_pages:
            return
        self._bound_pages.add(id(page))
        try:
            page.on("download", self._on_download)
            page.on("response", self._on_response)
        except Exception as exc:
            self._note_lost(exc)
            return

    def _on_response(self, response) -> None:
        """Keep a file the portal returns inline instead of as a download."""

        try:
            headers = response.headers or {}
            content_type = str(headers.get("content-type") or "").lower()
            disposition = str(headers.get("content-disposition") or "").lower()
            if not (
                "application/pdf" in content_type
                or "application/zip" in content_type
                or "attachment" in disposition
            ):
                return
            body = response.body()
            if body:
                self._downloads.append(bytes(body))
        except Exception as exc:
            # A response body that cannot be read is not a document; the
            # download event, if any, is still handled separately. A closed
            # session is still a closed session.
            self._note_lost(exc)
            return

    def _on_download(self, download) -> None:
        target = Path(tempfile.gettempdir()) / f"bidkaro-download-{uuid.uuid4().hex}.bin"
        try:
            download.save_as(str(target))
            self._downloads.append(target.read_bytes())
        except Exception as exc:  # noqa: BLE001 - report a broken download
            self._note_lost(exc)
            self._download_error = str(exc)
        finally:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass

    def take_download(self) -> DownloadResult | None:
        if self._downloads:
            content = self._downloads.popleft()
            return DownloadResult(
                ok=True,
                content=content,
                size=len(content),
                content_type="",
            )
        if self._download_error:
            error = self._download_error
            self._download_error = None
            return DownloadResult(ok=False, error=error)
        return None

    def wait_for_download(self, timeout_seconds: float = 5.0) -> bool:
        """Pump the page until the download the answer produced is delivered.

        While the job was parked nobody was running this page's event loop, so
        the download the portal sent after the human submitted its own form is
        still in the wire. Reading the queue before pumping would miss it and
        then ask for the file again, which issues a new challenge.
        """

        page = self._page
        if page is None:
            return False
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            if self._downloads or self._download_error:
                return True
            if time.monotonic() >= deadline:
                return False
            try:
                page.wait_for_timeout(250)
            except Exception as exc:
                self._note_lost(exc)
                return False

    def screenshot(self) -> bytes | None:
        page = self._page
        if page is None:
            return None
        try:
            return page.screenshot(type="png", full_page=False)
        except Exception as exc:
            self._note_lost(exc)
            return None

    def close(self) -> None:
        # Closing is ours, not the portal's: it must never mark the session
        # lost, or every finished job would report a closed window.
        self._closing = True
        # This browser belongs to this process, so closing it is correct.
        for target in (self._context, self._browser):
            try:
                if target is not None:
                    target.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._cdp = None
        self._window_id = None
        self._watching = False
        self._downloads.clear()
        self._bound_pages.clear()


def build_driver(
    settings: Settings | None = None, *, headless: bool | None = None
) -> LocalBrowserDriver:
    driver = LocalBrowserDriver(settings, headless=headless)
    driver.start()
    return driver
