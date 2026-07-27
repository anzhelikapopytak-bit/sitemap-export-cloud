"""
Cloud-only recursive sitemap exporter for GitHub Actions.

Input:
    sitemaps.txt — one root sitemap URL per line.

Output:
    output/summary.csv
    output/<root-sitemap>.csv
    output/_debug/* — failed HTML/XML responses.

Fetch strategy:
1. requests.Session
2. real Chromium navigation through Playwright
3. wait for an automatic Cloudflare challenge to resolve
4. capture the exact response body returned to page.goto()

No CAPTCHA solving or manual interaction is performed.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = Path(os.getenv("SITEMAPS_FILE", str(BASE_DIR / "sitemaps.txt")))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output")))
DEBUG_DIR = OUTPUT_DIR / "_debug"

HEADLESS = os.getenv("HEADLESS", "false").strip().lower() in {
    "1", "true", "yes", "y"
}
AUTOMATIC_CHALLENGE_WAIT_SECONDS = int(
    os.getenv("AUTOMATIC_CHALLENGE_WAIT_SECONDS", "35")
)
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
BROWSER_TIMEOUT_SECONDS = int(os.getenv("BROWSER_TIMEOUT_SECONDS", "120"))
MAX_DEPTH = int(os.getenv("MAX_DEPTH", "12"))
MAX_SITEMAPS_PER_ROOT = int(os.getenv("MAX_SITEMAPS_PER_ROOT", "5000"))

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,text/plain,text/html,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}


@dataclass
class FetchResult:
    url: str
    status: Optional[int]
    content_type: str
    text: str
    method: str
    error: str = ""


@dataclass
class UrlRow:
    url: str
    last_modified: str
    change_frequency: str
    priority: str
    source_sitemap: str


@dataclass
class RootSummary:
    root_sitemap: str
    status: str
    discovered_sitemaps: int
    successful_sitemaps: int
    failed_sitemaps: int
    exported_urls: int
    output_file: str
    message: str


def compact(value: object, limit: int = 1500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def detect_document_type(text: str) -> str:
    value = (text or "").lstrip("\ufeff \r\n\t")

    if re.search(r"<(?:[\w.-]+:)?sitemapindex[\s>]", value, re.I):
        return "sitemapindex"

    if re.search(r"<(?:[\w.-]+:)?urlset[\s>]", value, re.I):
        return "urlset"

    if re.search(r"<!doctype\s+html|<html[\s>]", value[:3000], re.I):
        return "html"

    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if lines and any(re.match(r"^https?://", line, re.I) for line in lines):
        return "txt"

    return "unknown"


def challenge_present(status: Optional[int], text: str) -> bool:
    lower = (text or "").lower()
    markers = (
        "just a moment",
        "checking your browser",
        "challenge-platform",
        "cf-chl-",
        "verify you are human",
    )
    return (
        status in {403, 429, 503}
        or any(marker in lower for marker in markers)
    ) and any(marker in lower for marker in markers)


def decode_body(body: bytes, content_type: str, url: str) -> str:
    if body[:2] == b"\x1f\x8b" or url.lower().endswith(".gz"):
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            pass

    charset_match = re.search(
        r"charset\s*=\s*[\"']?([^;\"'\s]+)",
        content_type or "",
        re.I,
    )

    encodings: list[str] = []
    if charset_match:
        encodings.append(charset_match.group(1).strip())

    encodings.extend(["utf-8-sig", "utf-8", "latin-1"])

    for encoding in encodings:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue

    return body.decode("utf-8", errors="replace")


def safe_filename(url: str, extension: str = ".csv") -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "sitemap").replace("www.", "")
    path = parsed.path.strip("/") or "root"
    readable = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{host}_{path}")[:135]
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{readable}_{digest}{extension}"


def save_debug(url: str, text: str, extension: str = ".html") -> str:
    if not text:
        return ""

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / safe_filename(url, extension)
    path.write_text(text, encoding="utf-8", errors="replace")
    return str(path)


def read_input_urls() -> list[str]:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Input file not found: {INPUT_FILE}")

    urls: list[str] = []
    seen: set[str] = set()

    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if not re.match(r"^https?://", line, re.I):
            print(f"Skipping invalid line: {line}")
            continue

        if line not in seen:
            seen.add(line)
            urls.append(line)

    if not urls:
        raise RuntimeError("sitemaps.txt contains no valid URLs.")

    return urls


def child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_sitemap(
    text: str,
) -> tuple[str, list[str], list[UrlRow]]:
    doc_type = detect_document_type(text)

    if doc_type == "txt":
        urls = [
            line.strip()
            for line in text.splitlines()
            if re.match(r"^https?://", line.strip(), re.I)
        ]
        return (
            "txt",
            [],
            [
                UrlRow(url, "", "", "", "")
                for url in urls
            ],
        )

    if doc_type not in {"sitemapindex", "urlset"}:
        raise ValueError(f"Unsupported document type: {doc_type}")

    root = ET.fromstring(text.lstrip("\ufeff"))

    if local_name(root.tag) == "sitemapindex":
        child_sitemaps: list[str] = []

        for item in root:
            if local_name(item.tag) != "sitemap":
                continue
            loc = child_text(item, "loc")
            if loc:
                child_sitemaps.append(loc)

        return "sitemapindex", child_sitemaps, []

    rows: list[UrlRow] = []

    for item in root:
        if local_name(item.tag) != "url":
            continue

        loc = child_text(item, "loc")
        if not loc:
            continue

        rows.append(
            UrlRow(
                url=loc,
                last_modified=child_text(item, "lastmod"),
                change_frequency=child_text(item, "changefreq"),
                priority=child_text(item, "priority"),
                source_sitemap="",
            )
        )

    return "urlset", [], rows


class Fetcher:
    def __init__(self, playwright: Playwright) -> None:
        self.playwright = playwright
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)

        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    @staticmethod
    def usable(result: FetchResult) -> bool:
        return (
            result.status is not None
            and 200 <= result.status < 300
            and detect_document_type(result.text)
            in {"sitemapindex", "urlset", "txt"}
        )

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
        if self.browser is not None:
            self.browser.close()

    def fetch(self, url: str) -> FetchResult:
        direct = self.fetch_requests(url)

        if self.usable(direct):
            return direct

        print(
            f"Requests did not return a sitemap: "
            f"HTTP={direct.status}, type={detect_document_type(direct.text)}, "
            f"error={direct.error or '-'}"
        )

        browser_result = self.fetch_browser(url)

        if self.usable(browser_result):
            self.copy_browser_cookies_to_requests()

        return browser_result

    def fetch_requests(self, url: str) -> FetchResult:
        try:
            response = self.session.get(
                url,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            return FetchResult(
                url=url,
                status=None,
                content_type="",
                text="",
                method="requests",
                error=compact(exc),
            )

        content_type = response.headers.get("Content-Type", "")
        text = decode_body(response.content, content_type, url)

        return FetchResult(
            url=url,
            status=response.status_code,
            content_type=content_type,
            text=text,
            method="requests",
        )

    def ensure_browser(self) -> None:
        if self.context is not None:
            return

        self.browser = self.playwright.chromium.launch(
            headless=HEADLESS,
        )
        self.context = self.browser.new_context(
            locale="es-ES",
            viewport={"width": 1440, "height": 900},
        )
        self.page = self.context.new_page()
        self.page.set_default_navigation_timeout(
            BROWSER_TIMEOUT_SECONDS * 1000
        )

    def navigate(self, url: str) -> FetchResult:
        assert self.page is not None

        try:
            response = self.page.goto(
                url,
                wait_until="domcontentloaded",
            )
        except Exception as exc:
            return FetchResult(
                url=url,
                status=None,
                content_type="",
                text="",
                method="playwright-browser-response",
                error=f"Navigation failed: {compact(exc)}",
            )

        if response is None:
            try:
                visible_html = self.page.content()
            except Exception as exc:
                visible_html = ""
                error = f"No Response object; page.content failed: {compact(exc)}"
            else:
                error = "Browser returned no Response object."

            return FetchResult(
                url=url,
                status=None,
                content_type="text/html",
                text=visible_html,
                method="playwright-browser-response",
                error=error,
            )

        try:
            content_type = response.headers.get("content-type", "")
            body = response.body()
            text = decode_body(body, content_type, url)
        except Exception as exc:
            return FetchResult(
                url=url,
                status=response.status,
                content_type=response.headers.get("content-type", ""),
                text="",
                method="playwright-browser-response",
                error=f"Could not read response: {compact(exc)}",
            )

        return FetchResult(
            url=url,
            status=response.status,
            content_type=content_type,
            text=text,
            method="playwright-browser-response",
        )

    def fetch_browser(self, url: str) -> FetchResult:
        self.ensure_browser()
        assert self.page is not None

        print(f"Opening Chromium: {url}")
        first = self.navigate(url)

        if self.usable(first):
            return first

        first_type = detect_document_type(first.text)
        is_challenge = challenge_present(first.status, first.text)

        print(
            f"First browser response: HTTP={first.status}, "
            f"type={first_type}, challenge={is_challenge}"
        )

        if not is_challenge:
            return first

        print(
            f"Waiting up to {AUTOMATIC_CHALLENGE_WAIT_SECONDS}s "
            "for an automatic challenge to finish..."
        )

        deadline = time.time() + AUTOMATIC_CHALLENGE_WAIT_SECONDS

        while time.time() < deadline:
            try:
                title = self.page.title()
                html = self.page.content()
            except Exception:
                title = ""
                html = ""

            combined = f"{title}\n{html}".lower()
            still_blocked = any(
                marker in combined
                for marker in (
                    "just a moment",
                    "checking your browser",
                    "challenge-platform",
                    "cf-chl-",
                    "verify you are human",
                )
            )

            if not still_blocked:
                self.page.wait_for_timeout(2000)
                break

            self.page.wait_for_timeout(1500)

        print("Repeating navigation in the same browser context...")
        return self.navigate(url)

    def copy_browser_cookies_to_requests(self) -> None:
        if self.context is None:
            return

        try:
            cookies = self.context.cookies()
        except Exception:
            return

        for cookie in cookies:
            try:
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            except Exception:
                continue


def export_root(root_url: str, fetcher: Fetcher) -> RootSummary:
    queue: deque[tuple[str, int]] = deque([(root_url, 0)])
    queued: set[str] = {root_url}
    processed: set[str] = set()

    rows_by_url: dict[str, UrlRow] = {}
    successful = 0
    failed = 0
    messages: list[str] = []

    while queue:
        sitemap_url, depth = queue.popleft()

        if sitemap_url in processed:
            continue

        if len(processed) >= MAX_SITEMAPS_PER_ROOT:
            messages.append(
                f"Stopped at MAX_SITEMAPS_PER_ROOT="
                f"{MAX_SITEMAPS_PER_ROOT}"
            )
            break

        processed.add(sitemap_url)
        print(
            f"[{len(processed)}] depth={depth}: {sitemap_url}"
        )

        result = fetcher.fetch(sitemap_url)

        if not fetcher.usable(result):
            failed += 1
            doc_type = detect_document_type(result.text)
            extension = ".html" if doc_type == "html" else ".txt"
            debug_path = save_debug(
                sitemap_url,
                result.text,
                extension,
            )

            message = (
                f"{sitemap_url}: HTTP={result.status}, "
                f"type={doc_type}, method={result.method}"
            )

            if result.error:
                message += f", error={result.error}"
            if debug_path:
                message += f", debug={debug_path}"

            messages.append(message)
            print(f"FAILED: {message}")
            continue

        try:
            kind, children, parsed_rows = parse_sitemap(result.text)
        except (ET.ParseError, ValueError) as exc:
            failed += 1
            debug_path = save_debug(
                sitemap_url,
                result.text,
                ".xml",
            )
            message = (
                f"{sitemap_url}: parse error={compact(exc)}, "
                f"debug={debug_path}"
            )
            messages.append(message)
            print(f"FAILED: {message}")
            continue

        successful += 1
        print(
            f"OK: method={result.method}, HTTP={result.status}, "
            f"type={kind}, children={len(children)}, "
            f"urls={len(parsed_rows)}"
        )

        for row in parsed_rows:
            row.source_sitemap = sitemap_url
            rows_by_url.setdefault(row.url, row)

        if kind == "sitemapindex":
            if depth >= MAX_DEPTH:
                messages.append(
                    f"{sitemap_url}: skipped children at "
                    f"MAX_DEPTH={MAX_DEPTH}"
                )
                continue

            for child_url in children:
                if child_url not in queued:
                    queued.add(child_url)
                    queue.append((child_url, depth + 1))

    output_path = OUTPUT_DIR / safe_filename(root_url)

    with output_path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "URL",
                "Last Modified",
                "Change Frequency",
                "Priority",
                "Source Sitemap",
            ]
        )

        for row in rows_by_url.values():
            writer.writerow(
                [
                    row.url,
                    row.last_modified,
                    row.change_frequency,
                    row.priority,
                    row.source_sitemap,
                ]
            )

    if rows_by_url and failed == 0:
        status = "success"
    elif rows_by_url:
        status = "partial"
    else:
        status = "failed"

    return RootSummary(
        root_sitemap=root_url,
        status=status,
        discovered_sitemaps=len(processed),
        successful_sitemaps=successful,
        failed_sitemaps=failed,
        exported_urls=len(rows_by_url),
        output_file=str(output_path),
        message=" | ".join(messages)[:30000],
    )


def write_summary(items: list[RootSummary]) -> Path:
    path = OUTPUT_DIR / "summary.csv"

    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "Root Sitemap",
                "Status",
                "Discovered Sitemaps",
                "Successful Sitemaps",
                "Failed Sitemaps",
                "Exported URLs",
                "Output File",
                "Message",
            ]
        )

        for item in items:
            writer.writerow(
                [
                    item.root_sitemap,
                    item.status,
                    item.discovered_sitemaps,
                    item.successful_sitemaps,
                    item.failed_sitemaps,
                    item.exported_urls,
                    item.output_file,
                    item.message,
                ]
            )

    return path


def main() -> int:
    root_urls = read_input_urls()

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 90)
    print("Cloud sitemap export")
    print(f"Input: {INPUT_FILE}")
    print(f"Output: {OUTPUT_DIR}")
    print(f"Headless: {HEADLESS}")
    print(f"Root sitemap count: {len(root_urls)}")
    print("=" * 90)

    summaries: list[RootSummary] = []

    with sync_playwright() as playwright:
        fetcher = Fetcher(playwright)

        try:
            for index, root_url in enumerate(root_urls, start=1):
                print()
                print("#" * 90)
                print(f"ROOT {index}/{len(root_urls)}: {root_url}")
                print("#" * 90)

                summary = export_root(root_url, fetcher)
                summaries.append(summary)

                print(
                    f"ROOT RESULT: status={summary.status}, "
                    f"urls={summary.exported_urls}, "
                    f"failed={summary.failed_sitemaps}"
                )
        finally:
            fetcher.close()

    summary_path = write_summary(summaries)

    print()
    print("=" * 90)
    print(f"Finished: {summary_path}")

    for item in summaries:
        print(
            f"{item.status.upper():7} | "
            f"{item.exported_urls:8} URLs | "
            f"{item.root_sitemap}"
        )

    # Individual blocked sites do not fail the whole workflow.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
