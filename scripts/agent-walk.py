"""Walk the CPPP tender directory in a real browser and read one notice.

Drives a TinyFish browser session over CDP the way the interactive agent mode
does: open the organisation directory, find the organisation, open its tender
list, shortlist a row, open the notice and read it. Used to check the live
behaviour of the portal before it is wired into the service.

    python scripts/agent-walk.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.integrations.browser_driver import CdpBrowserDriver  # noqa: E402
from app.integrations.tinyfish_agent import TinyFishBrowserClient  # noqa: E402

DIRECTORY = (
    "https://etenders.gov.in/eprocure/app?page=FrontEndTendersByOrganisation&service=page"
)

ORG_ROWS_JS = """
() => Array.from(document.querySelectorAll('table.list_table tr')).map(tr => {
    const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim());
    const a = tr.querySelector('a[id^=DirectLink]');
    return {
        cells,
        name: cells[1] || '',
        count: cells[2] || '',
        linkId: a ? a.id : null,
    };
}).filter(r => r.name && r.linkId)
"""

LIST_ROWS_JS = """
() => Array.from(document.querySelectorAll('table.list_table tr')).map(tr => {
    const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim());
    const a = tr.querySelector('td a[href*="FrontEndViewTender"], td a[href*="FrontEndTenderById"]');
    return {
        cells,
        publishing: cells[1] || '',
        closing: cells[2] || '',
        opening: cells[3] || '',
        title: cells[4] || '',
        chain: cells[5] || '',
        href: a ? a.getAttribute('href') : null,
        linkText: a ? (a.textContent || '').trim() : null,
    };
}).filter(r => r.cells.length > 3)
"""


def read_key() -> str:
    for candidate in (BACKEND.parent / ".env", BACKEND / ".env"):
        if candidate.exists():
            match = re.search(r"^TINYFISH_API_KEY=(.*)$", candidate.read_text(), re.M)
            if match:
                value = match.group(1).strip().strip('"').strip("'")
                if value:
                    return value
    raise SystemExit("TINYFISH_API_KEY is not set")


def main() -> int:
    client = TinyFishBrowserClient(read_key())
    session = client.create_session(None, timeout_seconds=900, proxy_country_code="IN")
    print(f"session: {session.session_id}")
    print(f"cdp    : {session.cdp_url}")

    driver = CdpBrowserDriver(session.cdp_url, settle_ms=5000)
    with driver:
        page = driver._page
        driver.open(DIRECTORY)
        print(f"directory title: {driver.snapshot().title}")

        orgs = page.evaluate(ORG_ROWS_JS)
        print(f"\norganisations on the page: {len(orgs)}")
        wanted = [
            row
            for row in orgs
            if "National Highways Authority" in row["name"]
            or "Maharashtra" in row["name"]
            or "Public Works Department" in row["name"]
        ]
        for row in wanted[:12]:
            print(f"  {row['name'][:70]:<70} count={row['count']:<6} {row['linkId']}")

        target = next(
            (row for row in orgs if "National Highways Authority" in row["name"]), None
        ) or next((row for row in orgs if "Maharashtra" in row["name"]), None)
        if not target:
            print("no matching organisation on this page")
            return 1
        print(f"\nchosen organisation: {target['name']} (count {target['count']})")

        page.click(f"#{target['linkId']}")
        page.wait_for_timeout(6000)
        print(f"tender list url: {page.url[:140]}")
        print(f"tender list title: {driver.snapshot().title}")

        rows = page.evaluate(LIST_ROWS_JS)
        print(f"\nrows on the list page: {len(rows)}")
        for row in rows[:12]:
            print(f"  pub={row['publishing']:<12} close={row['closing']:<12} {row['title'][:70]}")
            if row["href"]:
                print(f"      link: {row['linkText']}  ->  {row['href'][:110]}")

        # The title cell holds the link to the notice, in square brackets.
        linked = next((row for row in rows if row["href"]), None)
        if not linked:
            print("no linked row found")
            return 1
        print(f"\nopening notice: {linked['title'][:80]}")
        page.goto(
            "https://etenders.gov.in" + linked["href"]
            if linked["href"].startswith("/")
            else linked["href"],
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(5000)
        notice = driver.snapshot()
        print(f"notice url  : {notice.url[:160]}")
        print(f"notice title: {notice.title}")
        print(f"notice text : {len(notice.text)} chars")
        print("--- notice text ---")
        print(notice.text[:4000])
        Path("/tmp/cppp-notice.txt").write_text(notice.text, encoding="utf-8")
        Path("/tmp/cppp-notice-url.txt").write_text(notice.url, encoding="utf-8")
        print("\n(saved to /tmp/cppp-notice.txt and /tmp/cppp-notice-url.txt)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
