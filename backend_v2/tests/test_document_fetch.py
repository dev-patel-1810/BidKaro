from __future__ import annotations

import base64
import io
import threading
import uuid
import zipfile
from datetime import date

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.discovery import sessions
from app.discovery.answering import _portal_took_it
from app.discovery.browser import ChallengeMarker, DownloadResult, PageSnapshot
from app.discovery.documents import (
    DocumentFetchService,
    missing_covers,
    parse_covers,
)
from app.discovery.handlers import handle_fetch_documents
from app.discovery.sessions import FetchSession
from app.discovery.service import DiscoveryService
from app.models.corpus import TenderCover
from app.services.job_types import JobCancelled, JobParked
from tests.conftest import make_pdf
from tests.helpers import auth_header, verify

NOTICE_TEXT = "\n".join(
    [
        "Tender ID 2027_NHAI_555555_1",
        "Tender Reference Number NHAI/2027/55",
        "Bid Submission End Date 31-Dec-2027 05:00 PM",
        "Published Date 01-Oct-2027",
        "Location Maharashtra",
        "Organisation Chain National Highways Authority of India||"
        "Ministry of Road Transport and Highways",
        "Construction of four lane road on NH-48.",
        "Cover Details",
        "S.No Cover No Document Type Description",
        "1 1 Tender Notice Main notice",
        "2 2 BOQ Bill of quantities",
        "3 3 Drawings Drawings and plans",
        "4 4 Integrity Pact Integrity undertaking",
        "Tender Documents",
    ]
)

CAPTCHA_HTML = (
    '<html><body>'
    '<form action="/eprocure/app">'
    '<input type="hidden" name="seedids" value="s">'
    '<input type="hidden" name="session" value="t">'
    '<input name="captchaText" id="captchaText">'
    '<img id="captchaImage" src="data:image/png;base64,'
    + base64.b64encode(b"\x89PNG\r\n\x1a\ncaptcha").decode("ascii")
    + '">'
    "</form></body></html>"
)

LINKS = [
    {"url": "https://example.test/files/TenderNotice.pdf", "label": "Tender Notice", "kind": "pdf"},
    {"url": "https://example.test/files/BOQ.zip", "label": "Documents bundle", "kind": "zip"},
    {"url": "https://example.test/files/BOQ.xls", "label": "BOQ sheet", "kind": "file"},
]


def _notice(**overrides):
    notice = {
        "title": "Construction of four lane road on NH-48",
        "notice_url": "https://example.test/notice/555555",
        "listing_url": "https://example.test/list",
        "page_text": NOTICE_TEXT,
        "authority": "National Highways Authority of India",
        "organisation_chain": "National Highways Authority of India||"
        "Ministry of Road Transport and Highways",
        "tender_id": "2027_NHAI_555555_1",
        "reference_number": "NHAI/2027/55",
        "closing_date": "31-Dec-2027 05:00 PM",
        "publishing_date": "01-Oct-2027",
        "location": "Maharashtra",
        "value": "10,00,00,000",
        "document_links": LINKS,
        "extracted_evidence": {},
    }
    notice.update(overrides)
    return notice


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeDriver:
    def __init__(self, *, captcha: bool = True) -> None:
        self.captcha = captcha
        self.watching = False
        self.downloads: list[bytes] = []
        self.requested: list[str] = []
        self.open_url = ""
        self.current = PageSnapshot(
            url="https://example.test/notice/555555",
            title="Notice",
            text=NOTICE_TEXT,
            html="<html><body>notice</body></html>",
        )
        self.closed = False
        self.events: list[str] = []
        self._page = type("Page", (), {"evaluate": lambda _self, _script: []})()

    def begin_work(self) -> None:
        self.events.append("begin")

    def reveal(self) -> None:
        self.events.append("reveal")

    def open(self, url: str) -> PageSnapshot:
        self.open_url = url
        if self.captcha and "secret.pdf" in url:
            self.current = PageSnapshot(
                url=url, title="Captcha", text="Enter the captcha", html=CAPTCHA_HTML
            )
        else:
            self.current = PageSnapshot(
                url=url, title="Notice", text=NOTICE_TEXT, html="<html>notice</html>"
            )
        return self.current

    def snapshot(self) -> PageSnapshot:
        return self.current

    def find_challenge(self):
        if self.captcha and "secret.pdf" in (self.current.url or ""):
            return ChallengeMarker(
                kind="captcha",
                selector="#captchaText",
                message="The portal asks for the characters in the picture.",
            )
        return None

    def download_in_page(self, url: str, *, max_bytes: int = 25 * 1024 * 1024):
        self.requested.append(url)
        if "secret.pdf" in url:
            return DownloadResult(
                ok=False,
                is_page=True,
                html=CAPTCHA_HTML,
                content_type="text/html",
            )
        if url.endswith("TenderNotice.pdf"):
            return DownloadResult(ok=True, content=make_pdf(["Main notice"]), size=100)
        if url.endswith("BOQ.zip"):
            return DownloadResult(
                ok=True,
                content=_zip_bytes(
                    {
                        "BOQ.pdf": make_pdf(["Bill of quantities"]),
                        "Drawings.pdf": make_pdf(["Drawings"]),
                        "notes.txt": b"not a pdf",
                    }
                ),
                size=200,
            )
        if url.endswith("BOQ.xls"):
            return DownloadResult(ok=True, content=b"\xd0\xcf\x11\xe0sheet", size=8)
        return DownloadResult(ok=False, error="not found")

    def watch_for_download(self) -> None:
        self.watching = True

    def wait_for_download(self, timeout_seconds: float = 5.0) -> bool:
        return bool(self.downloads)

    def take_download(self):
        if self.downloads:
            return DownloadResult(ok=True, content=self.downloads.pop(0))
        return None

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\nscreen"

    def submit_answer(self, answer: str) -> PageSnapshot:
        self.submitted = answer
        # A right answer: the portal hands the file over and draws no picture.
        self.downloads.append(make_pdf(["The secret document"]))
        self.current = PageSnapshot(
            url="https://example.test/files/secret.pdf",
            title="Notice",
            text=NOTICE_TEXT,
            html="<html>notice</html>",
        )
        return self.current

    def has_pending_download(self) -> bool:
        return bool(self.downloads)

    def close(self) -> None:
        self.closed = True


class PageAfterAnswerDriver(FakeDriver):
    """A portal that answers a correct captcha with its own page.

    The page carries a download link (or nothing). It is not the original
    captcha URL, so requesting that URL again would draw a new captcha.
    """

    def __init__(
        self,
        *,
        page_links: list[dict] | None = None,
        search_captcha: bool = False,
    ) -> None:
        super().__init__()
        self.page_links = page_links or []
        # A notice or download page can show the portal's ordinary search
        # captcha. It is not the document challenge and must not reject an
        # answer that already moved the page on.
        self.search_captcha = search_captcha
        # The notice itself has no extra links; only the page the answer
        # produced does. Returning the links everywhere would make the test
        # fetch the same file twice for the wrong reason.
        self._page = type(
            "Page",
            (),
            {
                "evaluate": lambda _self, _script: (
                    self.page_links
                    if "after-captcha" in (self.current.url or "")
                    else []
                )
            },
        )()

    def submit_answer(self, answer: str) -> PageSnapshot:
        self.submitted = answer
        self.current = PageSnapshot(
            url="https://example.test/after-captcha",
            title="Document ready",
            text="Your document is ready. Download",
            html=(
                '<html><body><a href="https://example.test/files/final.pdf">'
                "Download</a></body></html>"
            ),
        )
        return self.current

    def find_challenge(self):
        if self.search_captcha and "after-captcha" in (self.current.url or ""):
            return ChallengeMarker(
                kind="captcha",
                selector="#searchCaptcha",
                message="The portal's search form asks for a captcha.",
            )
        return super().find_challenge()

    def download_in_page(self, url: str, *, max_bytes: int = 25 * 1024 * 1024):
        self.requested.append(url)
        if url.endswith("secret.pdf"):
            return DownloadResult(
                ok=False,
                is_page=True,
                html=CAPTCHA_HTML,
                content_type="text/html",
            )
        if url.endswith("final.pdf"):
            return DownloadResult(ok=True, content=make_pdf(["The final document"]), size=100)
        return DownloadResult(ok=False, error="not found")


def _create_tender(**overrides) -> uuid.UUID:
    with SessionLocal() as session:
        tender, created, reason = DiscoveryService(session).ingest_notice(
            _notice(**overrides)
        )
        assert reason is None and created and tender is not None
        return tender.id


def test_parse_covers_and_name_the_missing_one():
    rows = parse_covers(NOTICE_TEXT)
    assert [row["cover_no"] for row in rows] == [1, 2, 3, 4]
    assert rows[3]["doc_type"] == "Integrity Pact"

    with SessionLocal() as session:
        tender_id = _create_tender()
        service = DocumentFetchService(session)
        from app.models.corpus import TenderDocument

        # A file list that matches three of the four covers, so the fourth is
        # named rather than reported as an anonymous count.
        documents = [
            TenderDocument(filename="TenderNotice.pdf", document_key="tendernotice"),
            TenderDocument(filename="BOQ.pdf", document_key="boq"),
            TenderDocument(filename="Drawings.pdf", document_key="drawings"),
        ]
        covers = [
            type(
                "Cover",
                (),
                {"cover_no": row["cover_no"], "doc_type": row["doc_type"],
                 "description": row["description"]},
            )()
            for row in rows
        ]
        assert missing_covers(covers, documents) == [4]
        assert service.session is not None


def test_fetch_attaches_pdfs_and_reports_a_non_pdf_and_a_missing_cover():
    tender_id = _create_tender()
    with SessionLocal() as session:
        driver = FakeDriver(captcha=False)
        run = FetchSession(job_id=uuid.uuid4(), tender_id=tender_id, driver=driver)
        service = DocumentFetchService(session)
        outcome = service.begin(run)

        assert outcome.challenge is None
        assert outcome.state == "partial"
        # The individual file came through, so the bundle was never requested:
        # asking for it would cost a second captcha and attach the same files.
        assert len(outcome.documents) == 1
        assert not any(url.endswith("BOQ.zip") for url in driver.requested)
        assert any("not a PDF" in error for error in outcome.errors)
        assert outcome.metrics["covers_expected"] == 4
        assert outcome.metrics["covers_missing"] == [2, 3, 4]
        assert outcome.metrics["documents"] == 1

        from app.repositories.corpus import TenderAuxRepository

        state = TenderAuxRepository(session).fetch_state(tender_id)
        assert state is not None
        assert state.expected_covers == 4
        assert state.documents_present == 1
        assert state.missing_covers == [2, 3, 4]
        assert state.last_error
        assert len(list(session.scalars(select(TenderCover)).all())) == 4


def test_the_bundle_is_the_fallback_when_no_pdf_arrives():
    tender_id = _create_tender(
        document_links=[
            {
                "url": "https://example.test/files/missing.pdf",
                "label": "Notice",
                "kind": "pdf",
            },
            {
                "url": "https://example.test/files/BOQ.zip",
                "label": "Documents bundle",
                "kind": "zip",
            },
        ]
    )
    with SessionLocal() as session:
        driver = FakeDriver(captcha=False)
        run = FetchSession(job_id=uuid.uuid4(), tender_id=tender_id, driver=driver)
        outcome = DocumentFetchService(session).begin(run)

        assert outcome.challenge is None
        # The PDF did not arrive, so the bundle was read and unpacked instead.
        assert any(url.endswith("BOQ.zip") for url in driver.requested)
        assert len(outcome.documents) == 2
        assert any("not a PDF" in error for error in outcome.errors)


def test_captcha_is_one_handoff_and_the_download_is_attached():
    tender_id = _create_tender(
        document_links=[
            {"url": "https://example.test/files/secret.pdf", "label": "Secret", "kind": "pdf"}
        ]
    )
    with SessionLocal() as session:
        driver = FakeDriver()
        run = FetchSession(job_id=uuid.uuid4(), tender_id=tender_id, driver=driver)
        service = DocumentFetchService(session)

        first = service.begin(run)
        assert first.challenge is not None
        assert first.challenge.image
        assert first.challenge.kind == "captcha"
        assert run.pending_link is not None
        assert driver.watching is True
        assert driver.downloads == []

        # The human answered in the portal window; the portal's own response
        # became a download, and the service attaches those bytes.
        driver.downloads.append(make_pdf(["The secret document"]))
        second = service.resume(run)
        assert second.challenge is None
        assert second.state == "done"
        assert second.metrics["documents"] == 1
        assert run.answered_challenges == 1


def test_a_rejected_answer_shows_the_new_picture_and_is_bounded():
    tender_id = _create_tender(
        document_links=[
            {"url": "https://example.test/files/secret.pdf", "label": "Secret", "kind": "pdf"}
        ]
    )
    with SessionLocal() as session:
        driver = FakeDriver()
        run = FetchSession(job_id=uuid.uuid4(), tender_id=tender_id, driver=driver)
        service = DocumentFetchService(session)
        assert service.begin(run).challenge is not None

        # No download arrived: the portal drew a new picture. The same file
        # request stays pending, with the new picture, instead of being lost.
        second = service.resume(run)
        assert second.challenge is not None
        assert "not accepted" in second.challenge.message
        assert second.challenge.image
        assert second.challenge.screenshot
        assert run.pending_link is not None
        assert run.index == 0
        assert run.rejected_answers == 1

        # A portal that never accepts an answer is reported, not looped on.
        for _ in range(2):
            third = service.resume(run)
            assert third.challenge is not None
        final = service.resume(run)
        assert final.challenge is None
        assert final.state == "partial"
        assert any("did not accept" in error for error in final.errors)
        assert run.index == 1
        assert run.pending_link is None


def test_an_accepted_answer_follows_the_pages_own_link_instead_of_looping():
    tender_id = _create_tender(
        document_links=[
            {"url": "https://example.test/files/secret.pdf", "label": "Secret", "kind": "pdf"}
        ]
    )
    with SessionLocal() as session:
        driver = PageAfterAnswerDriver(
            page_links=[
                {
                    "url": "https://example.test/files/final.pdf",
                    "label": "Download",
                    "kind": "pdf",
                }
            ]
        )
        run = FetchSession(job_id=uuid.uuid4(), tender_id=tender_id, driver=driver)
        service = DocumentFetchService(session)

        assert service.begin(run).challenge is not None
        driver.submit_answer("right")

        outcome = service.resume(run)
        assert outcome.challenge is None
        assert outcome.state == "done"
        assert len(outcome.documents) == 1
        # The original captcha URL was requested once, by the first attempt.
        # Following the portal's own page must not request it again.
        assert driver.requested.count("https://example.test/files/secret.pdf") == 1
        assert "https://example.test/files/final.pdf" in driver.requested
        assert run.answered_challenges == 1
        assert run.index == 1
        assert not any("did not hand over" in error for error in outcome.errors)


def test_an_accepted_answer_with_no_file_is_reported_not_requested_again():
    tender_id = _create_tender(
        document_links=[
            {"url": "https://example.test/files/secret.pdf", "label": "Secret", "kind": "pdf"}
        ]
    )
    with SessionLocal() as session:
        driver = PageAfterAnswerDriver(page_links=[])
        run = FetchSession(job_id=uuid.uuid4(), tender_id=tender_id, driver=driver)
        service = DocumentFetchService(session)

        assert service.begin(run).challenge is not None
        driver.submit_answer("right")

        outcome = service.resume(run)
        assert outcome.challenge is None
        assert outcome.state == "partial"
        assert outcome.documents == []
        assert driver.requested.count("https://example.test/files/secret.pdf") == 1
        assert run.index == 1
        assert run.pending_link is None
        assert any("did not hand over" in error for error in outcome.errors)


def test_a_cancelled_fetch_stops_at_its_next_step():
    tender_id = _create_tender()
    with SessionLocal() as session:
        driver = FakeDriver(captcha=False)
        run = FetchSession(job_id=uuid.uuid4(), tender_id=tender_id, driver=driver)

        def on_step() -> None:
            raise JobCancelled("cancelled by the user")

        with pytest.raises(JobCancelled):
            DocumentFetchService(session).begin(run, on_step=on_step)


def test_fetch_route_is_one_global_job_and_manual_upload_is_refused(
    client, fake_email
):
    owner = verify(client, fake_email, email="fetch@example.com")
    headers = auth_header(owner["access_token"])
    tender_id = _create_tender()

    first = client.post(f"/api/tenders/{tender_id}/documents/fetch", headers=headers)
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["created"] is True
    assert body["job"]["kind"] == "fetch_documents"
    assert body["job"]["company_id"] is None

    again = client.post(f"/api/tenders/{tender_id}/documents/fetch", headers=headers)
    assert again.status_code == 202, again.text
    assert again.json()["created"] is False
    assert again.json()["job"]["id"] == body["job"]["id"]

    documents = client.get(f"/api/tenders/{tender_id}/documents", headers=headers)
    assert documents.status_code == 200, documents.text
    assert documents.json()["fetch_state"]["last_requested_at"] is not None

    manual = client.post(
        "/api/tenders",
        headers=headers,
        files={"file": ("manual.pdf", make_pdf(["Manual tender"]), "application/pdf")},
        data={"work_title": "Manually uploaded work"},
    )
    assert manual.status_code == 201, manual.text
    refused = client.post(
        f"/api/tenders/{manual.json()['id']}/documents/fetch", headers=headers
    )
    assert refused.status_code == 422, refused.text
    assert "manually uploaded" in refused.json()["detail"]


def test_cancelling_a_fetch_closes_its_browser_session(client, fake_email):
    owner = verify(client, fake_email, email="cancel-fetch@example.com")
    headers = auth_header(owner["access_token"])
    tender_id = _create_tender()

    response = client.post(
        f"/api/tenders/{tender_id}/documents/fetch", headers=headers
    )
    assert response.status_code == 202, response.text
    job = response.json()["job"]
    job_id = uuid.UUID(job["id"])

    driver = FakeDriver()
    sessions.put(
        FetchSession(job_id=job_id, tender_id=tender_id, driver=driver)
    )

    cancel = client.post(f"/api/jobs/{job['id']}/cancel", headers=headers)
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["state"] == "cancelled"
    assert driver.closed is True
    assert sessions.get(job_id) is None


def test_closing_a_job_closes_its_browser_session():
    from app.discovery.handlers import close_job_session

    driver = FakeDriver()
    job_id = uuid.uuid4()
    sessions.put(
        FetchSession(job_id=job_id, tender_id=uuid.uuid4(), driver=driver)
    )
    close_job_session("fetch_documents", job_id)
    assert driver.closed is True
    assert sessions.get(job_id) is None


class FakeFetchContext:
    """The part of JobContext the fetch handler uses, without a real worker."""

    def __init__(self, job_id: uuid.UUID, tender_id: uuid.UUID) -> None:
        self.job_id = job_id
        self.run_id = None
        self.payload = {"tender_id": str(tender_id)}
        self.parked: dict | None = None

    def heartbeat(self) -> None:
        return None

    def cancelled(self) -> bool:
        return False

    def latest_challenge(self):
        return None

    def park(self, *, kind, context=None, image_path=None, expires_at=None):
        self.parked = {
            "kind": kind,
            "context": context or {},
            "image_path": image_path,
        }
        raise JobParked(uuid.uuid4())


def _on_another_thread(fn):
    """Run ``fn`` on a thread that is provably not this one."""

    outcome: dict = {}

    def run() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            outcome["error"] = exc

    thread = threading.Thread(target=run, name="not-the-browser-thread")
    thread.start()
    thread.join(timeout=60)
    assert not thread.is_alive(), "the job did not finish on its own thread"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class ThreadBoundDriver(FakeDriver):
    """A driver that refuses to be used from any thread but its own.

    This is the rule Playwright enforces with greenlets. A fake that enforces
    it too means the test fails on the bug instead of on the library's error
    message, and it fails even when the job happens to resume on the same
    worker thread by luck.
    """

    def __init__(self) -> None:
        super().__init__()
        self.created_on = threading.get_ident()

    def _check(self) -> None:
        assert threading.get_ident() == self.created_on, (
            "the browser was used from a thread that does not own it"
        )

    def open(self, url: str):
        self._check()
        return super().open(url)

    def snapshot(self):
        self._check()
        return super().snapshot()

    def find_challenge(self):
        self._check()
        return super().find_challenge()

    def download_in_page(self, url: str, *, max_bytes: int = 25 * 1024 * 1024):
        self._check()
        return super().download_in_page(url, max_bytes=max_bytes)

    def watch_for_download(self) -> None:
        self._check()
        super().watch_for_download()

    def wait_for_download(self, timeout_seconds: float = 5.0) -> bool:
        self._check()
        return super().wait_for_download(timeout_seconds)

    def take_download(self):
        self._check()
        return super().take_download()

    def screenshot(self):
        self._check()
        return super().screenshot()

    def close(self) -> None:
        self._check()
        super().close()


class TargetClosedError(Exception):
    """The shape of Playwright's error when the window has been closed."""


class WindowClosedDriver(FakeDriver):
    """A driver whose window the user closed while the job was parked."""

    def __init__(self) -> None:
        super().__init__()
        self.window_closed = False

    def _fail(self):
        self.lost = True
        raise TargetClosedError(
            "Target page, context or browser has been closed"
        )

    def _guard(self) -> None:
        if self.window_closed:
            self._fail()

    def open(self, url: str):
        self._guard()
        return super().open(url)

    def snapshot(self):
        self._guard()
        return super().snapshot()

    def find_challenge(self):
        self._guard()
        return super().find_challenge()

    def download_in_page(self, url: str, *, max_bytes: int = 25 * 1024 * 1024):
        self._guard()
        return super().download_in_page(url, max_bytes=max_bytes)

    def wait_for_download(self, timeout_seconds: float = 5.0) -> bool:
        self._guard()
        return super().wait_for_download(timeout_seconds)

    def take_download(self):
        self._guard()
        return super().take_download()

    def screenshot(self):
        self._guard()
        return super().screenshot()


class BufferedDownloadDriver(FakeDriver):
    """A driver whose answered download is still in the wire when resumed."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: list[bytes] = []
        self.pumped = False

    def take_download(self):
        if not self.pumped:
            return None
        return super().take_download()

    def wait_for_download(self, timeout_seconds: float = 5.0) -> bool:
        self.pumped = True
        self.downloads.extend(self.pending)
        self.pending.clear()
        return bool(self.downloads)


class SwallowedClosedDriver(FakeDriver):
    """A driver that catches the closed error and returns a file error.

    This is the shape of the live failure: ``download_in_page`` reported
    "Cannot switch to a different thread" as a per-link error, so the job
    looked like a document problem instead of a dead session.
    """

    def __init__(self) -> None:
        super().__init__()
        self.window_closed = False

    def download_in_page(self, url: str, *, max_bytes: int = 25 * 1024 * 1024):
        if self.window_closed:
            self.lost = True
            return DownloadResult(
                ok=False,
                error="Target page, context or browser has been closed",
            )
        return super().download_in_page(url, max_bytes=max_bytes)

    def snapshot(self):
        if self.window_closed:
            # The real driver swallows a dead page into an empty snapshot.
            self.lost = True
            return PageSnapshot(url="", title="", text="", html="")
        return super().snapshot()

    def find_challenge(self):
        if self.window_closed:
            self.lost = True
            return None
        return super().find_challenge()


def _secret_tender() -> uuid.UUID:
    return _create_tender(
        document_links=[
            {
                "url": "https://example.test/files/secret.pdf",
                "label": "Secret",
                "kind": "pdf",
            }
        ]
    )


def test_a_parked_fetch_resumes_on_the_thread_that_owns_the_browser(monkeypatch):
    tender_id = _secret_tender()
    created: list[ThreadBoundDriver] = []

    def factory(*_args, **_kwargs):
        driver = ThreadBoundDriver()
        created.append(driver)
        return driver

    monkeypatch.setattr("app.discovery.handlers.build_driver", factory)
    monkeypatch.setattr(
        "app.discovery.handlers._save_challenge", lambda *args: "captcha.png"
    )

    job_id = uuid.uuid4()
    with pytest.raises(JobParked):
        handle_fetch_documents(FakeFetchContext(job_id, tender_id))

    driver = created[0]
    assert driver.closed is False
    assert sessions.get(job_id) is not None
    # Covered and small while working, and it stays that way: the picture is
    # answered in the app, so there is no window to uncover.
    assert driver.events[0] == "begin"
    assert "reveal" not in driver.events

    # The human answered in the portal window, and a different worker claims
    # the resumed job. The body must still run on the thread that owns the
    # browser: this is the bug that produced "Cannot switch to a different
    # thread" and lost the file.
    driver.downloads.append(make_pdf(["The secret document"]))
    result = _on_another_thread(
        lambda: handle_fetch_documents(FakeFetchContext(job_id, tender_id))
    )

    assert result.state == "done"
    assert result.metrics["documents"] == 1
    assert sessions.get(job_id) is None
    assert driver.closed is True
    # The resume returned the window to work mode before fetching again.
    assert driver.events.count("begin") == 2


def test_a_closed_window_is_reported_once_and_never_retried(monkeypatch):
    from app.discovery.browser import SESSION_LOST_NOTE
    from app.repositories.corpus import TenderAuxRepository

    tender_id = _secret_tender()
    driver = WindowClosedDriver()
    monkeypatch.setattr(
        "app.discovery.handlers.build_driver", lambda *args, **kwargs: driver
    )
    monkeypatch.setattr(
        "app.discovery.handlers._save_challenge", lambda *args: "captcha.png"
    )

    job_id = uuid.uuid4()
    with pytest.raises(JobParked):
        handle_fetch_documents(FakeFetchContext(job_id, tender_id))

    # The user closed the window instead of answering. The next attempt finds
    # a dead session and says so; it does not ask for a second captcha and it
    # does not raise (a raise would be retried by the worker).
    driver.window_closed = True
    result = handle_fetch_documents(FakeFetchContext(job_id, tender_id))

    assert result.state == "partial"
    assert result.notes == SESSION_LOST_NOTE
    assert sessions.get(job_id) is None
    assert driver.closed is True
    with SessionLocal() as session:
        state = TenderAuxRepository(session).fetch_state(tender_id)
        assert state is not None
        assert state.last_error == SESSION_LOST_NOTE


def test_a_closed_window_that_the_driver_swallowed_is_still_reported(monkeypatch):
    from app.discovery.browser import SESSION_LOST_NOTE

    tender_id = _secret_tender()
    driver = SwallowedClosedDriver()
    monkeypatch.setattr(
        "app.discovery.handlers.build_driver", lambda *args, **kwargs: driver
    )
    monkeypatch.setattr(
        "app.discovery.handlers._save_challenge", lambda *args: "captcha.png"
    )

    job_id = uuid.uuid4()
    with pytest.raises(JobParked):
        handle_fetch_documents(FakeFetchContext(job_id, tender_id))

    # The window is gone, but the file call swallowed the error and returned a
    # per-link message. The job must still say the session ended, not that one
    # PDF failed, and it must not be retried.
    driver.window_closed = True
    result = handle_fetch_documents(FakeFetchContext(job_id, tender_id))

    assert result.state == "partial"
    assert result.notes == SESSION_LOST_NOTE
    assert sessions.get(job_id) is None


def test_a_download_delivered_while_parked_is_read_on_resume():
    tender_id = _secret_tender()
    with SessionLocal() as session:
        driver = BufferedDownloadDriver()
        run = FetchSession(
            job_id=uuid.uuid4(), tender_id=tender_id, driver=driver
        )
        service = DocumentFetchService(session)
        assert service.begin(run).challenge is not None

        # The portal handed the file over while nobody was running the page's
        # event loop. Reading the queue before pumping would miss it and ask
        # for the file again, which issues a new captcha.
        driver.pending.append(make_pdf(["Delivered while parked"]))
        outcome = service.resume(run)

        assert driver.pumped is True
        assert outcome.challenge is None
        assert outcome.state == "done"
        assert outcome.metrics["documents"] == 1


def test_the_app_carries_the_answer_and_never_keeps_it(client, fake_email):
    from datetime import timedelta

    from sqlalchemy import text

    from app.core.time import utcnow
    from app.repositories.work import JobRepository

    owner = verify(client, fake_email, email="captcha@example.com")
    headers = auth_header(owner["access_token"])
    tender_id = _secret_tender()

    started = client.post(
        f"/api/tenders/{tender_id}/documents/fetch", headers=headers
    )
    assert started.status_code == 202, started.text
    job_id = uuid.UUID(started.json()["job"]["id"])

    with SessionLocal() as session:
        jobs = JobRepository(session)
        job = jobs.get(job_id, for_update=True)
        job.state = "running"
        session.flush()
        jobs.park(
            job.id,
            challenge_kind="captcha",
            context={"document": "Secret", "screenshot_url": None},
            image_path=None,
            expires_at=utcnow() + timedelta(minutes=10),
        )
        session.commit()

    driver = FakeDriver()
    sessions.put(
        FetchSession(job_id=job_id, tender_id=tender_id, driver=driver)
    )
    try:
        answered = client.post(
            f"/api/jobs/{job_id}/challenge/answer",
            headers=headers,
            json={"answer": "ab12cd"},
        )
        assert answered.status_code == 200, answered.text
        body = answered.json()
        assert body["accepted"] is True
        assert driver.submitted == "ab12cd"
        assert body["job"]["state"] == "queued"
        assert "ab12cd" not in answered.text

        # The answer is not in a challenge row, a job error or a run metric.
        with SessionLocal() as session:
            dump = " ".join(
                str(row)
                for row in session.execute(
                    text(
                        "select context::text from challenges "
                        "union all select last_error from jobs "
                        "union all select metrics::text from runs"
                    )
                ).scalars()
                if row is not None
            )
        assert "ab12cd" not in dump
    finally:
        sessions.close(job_id)


def test_the_app_refreshes_the_picture_when_the_answer_is_rejected(
    client, fake_email
):
    from datetime import timedelta

    from app.core.time import utcnow
    from app.repositories.work import JobRepository

    owner = verify(client, fake_email, email="captcha2@example.com")
    headers = auth_header(owner["access_token"])
    tender_id = _secret_tender()

    started = client.post(
        f"/api/tenders/{tender_id}/documents/fetch", headers=headers
    )
    job_id = uuid.UUID(started.json()["job"]["id"])
    with SessionLocal() as session:
        jobs = JobRepository(session)
        job = jobs.get(job_id, for_update=True)
        job.state = "running"
        session.flush()
        jobs.park(
            job.id,
            challenge_kind="captcha",
            context={"document": "Secret"},
            image_path=None,
            expires_at=utcnow() + timedelta(minutes=10),
        )
        session.commit()

    class RejectingDriver(FakeDriver):
        def submit_answer(self, answer: str) -> PageSnapshot:
            self.submitted = answer
            self.current = PageSnapshot(
                url="https://example.test/files/secret.pdf",
                title="Captcha",
                text="Enter the captcha",
                html=CAPTCHA_HTML,
            )
            return self.current

        def has_pending_download(self) -> bool:
            return False

    driver = RejectingDriver()
    sessions.put(
        FetchSession(job_id=job_id, tender_id=tender_id, driver=driver)
    )
    try:
        answered = client.post(
            f"/api/jobs/{job_id}/challenge/answer",
            headers=headers,
            json={"answer": "wrong"},
        )
        assert answered.status_code == 200, answered.text
        body = answered.json()
        assert body["accepted"] is False
        assert body["job"]["state"] == "needs_you"
        assert body["job"]["challenge"]["state"] == "open"
        assert body["job"]["challenge"]["image_url"]
        assert body["job"]["challenge"]["context"]["answer_rejected"] is True
    finally:
        sessions.close(job_id)


def test_manual_upload_accepts_a_zip_and_attaches_each_pdf(client, fake_email):
    owner = verify(client, fake_email, email="zipupload@example.com")
    headers = auth_header(owner["access_token"])
    tender_id = _create_tender()

    archive = _zip_bytes(
        {
            "TenderNotice.pdf": make_pdf(["Main notice"]),
            "nested/BOQ.pdf": make_pdf(["Bill of quantities"]),
            "readme.txt": b"not a pdf",
        }
    )
    uploaded = client.post(
        f"/api/tenders/{tender_id}/documents/archive",
        headers=headers,
        files={"file": ("documents.zip", archive, "application/zip")},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert [item["filename"] for item in body["documents"]] == [
        "TenderNotice.pdf",
        "BOQ.pdf",
    ]
    assert any("not a PDF" in note for note in body["skipped"])

    listed = client.get(f"/api/tenders/{tender_id}/documents", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 2
