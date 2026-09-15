"""Read a weekly deadline table and render one Telegram message per date."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from html import escape
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

CAFE_ID = 21160703
HEADERS = ("마감기간", "기업명", "직무", "고용형태")
WEEKDAYS = "월화수목금토일"
MESSAGE_LIMIT = 4096


class ScheduleError(ValueError):
    """The complete schedule could not be read safely."""


@dataclass(frozen=True)
class Job:
    deadline: date
    company: str
    url: str | None
    duties: str
    employment: str


@dataclass(frozen=True)
class Article:
    html: str
    published: date
    url: str


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\u200b-\u200d\ufeff]", "", text)).strip()


def article_url(article_id: str) -> str:
    if not re.fullmatch(r"[1-9]\d*", str(article_id)):
        raise ScheduleError("게시글 번호가 올바르지 않습니다")
    return f"https://cafe.naver.com/ca-fe/cafes/{CAFE_ID}/articles/{article_id}"


def safe_url(value: str, base: str) -> str | None:
    value = urljoin(base, value.strip())
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return value


def parse_deadline(value: str, published: date) -> date:
    normalized = clean(value).replace(" ", "")
    match = re.fullmatch(
        r"[~～∼]?(?:(\d{4})[./-])?(\d{1,2})[./-](\d{1,2})"
        r"(?:\(([월화수목금토일])(?:요일)?\))?(?:마감)?", normalized
    )
    if not match:
        raise ScheduleError(f"마감일 형식을 확인할 수 없습니다: {value}")
    year, month, day, weekday = match.groups()
    years = [int(year)] if year else range(published.year - 1, published.year + 2)
    candidates = []
    for candidate_year in years:
        try:
            candidates.append(date(candidate_year, int(month), int(day)))
        except ValueError:
            pass
    if not candidates:
        raise ScheduleError(f"존재하지 않는 마감일: {value}")
    result = min(candidates, key=lambda candidate: abs((candidate - published).days))
    if not year and abs((result - published).days) > 90:
        raise ScheduleError(f"게시일과 마감일의 연도를 확인해야 합니다: {value}")
    if weekday and WEEKDAYS[result.weekday()] != weekday:
        raise ScheduleError(f"마감일과 요일이 일치하지 않습니다: {value}")
    return result


def table_grid(table):
    """Expand rowspan/colspan while preserving the original cell/link nodes."""
    occupied = {}
    rows = table.find_all("tr")
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            while (row_index, column) in occupied:
                column += 1
            try:
                rowspan = int(cell.get("rowspan", 1))
                colspan = int(cell.get("colspan", 1))
            except ValueError as exc:
                raise ScheduleError("잘못된 표 병합 값") from exc
            if not 1 <= rowspan <= len(rows) - row_index or not 1 <= colspan <= 30:
                raise ScheduleError("잘못된 표 병합 범위")
            for row_offset in range(rowspan):
                for column_offset in range(colspan):
                    key = (row_index + row_offset, column + column_offset)
                    if key in occupied:
                        raise ScheduleError("겹치는 표 병합 셀")
                    occupied[key] = cell
            column += colspan
    width = max((column for _, column in occupied), default=-1) + 1
    return [[occupied.get((row, column)) for column in range(width)]
            for row in range(len(rows))]


def parse_schedule(html: str, published: date, source_url: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    found_table = False
    for table in soup.find_all("table"):
        if table.find_parent("table"):
            continue
        # Only the schedule table is authoritative; ignore banners and other tables.
        if not all(header in clean(table.get_text(" ")) for header in HEADERS):
            continue
        grid = table_grid(table)
        header_index = None
        columns = None
        for index, row in enumerate(grid):
            labels = [clean(cell.get_text(" ")) if cell else "" for cell in row]
            if all(label in labels for label in HEADERS):
                if any(labels.count(label) != 1 for label in HEADERS):
                    raise ScheduleError("중복된 일정표 열 이름")
                header_index, columns = index, {label: labels.index(label) for label in HEADERS}
                break
        if columns is None:
            raise ScheduleError("일정표 열을 식별할 수 없습니다")
        found_table = True
        current_date = None
        for row in grid[header_index + 1:]:
            if any(row[index] is None for index in columns.values()):
                raise ScheduleError("일정표에 누락된 셀이 있습니다")
            cells = {name: row[index] for name, index in columns.items()}
            values = {name: clean(cell.get_text(" ")) for name, cell in cells.items()}
            if all(values[name] == name for name in HEADERS):
                continue  # Repeated page header.
            if values["마감기간"]:
                current_date = parse_deadline(values["마감기간"], published)
            if not values["기업명"]:
                if values["직무"] or values["고용형태"]:
                    raise ScheduleError("기업명이 없는 채용 항목")
                continue
            if current_date is None:
                raise ScheduleError("기업에 연결할 마감일이 없습니다")
            links = []
            for anchor in cells["기업명"].find_all("a", href=True):
                link = safe_url(anchor["href"], source_url)
                if link and link not in links:
                    links.append(link)
            if len(links) > 1:
                raise ScheduleError("한 기업 셀에 서로 다른 공고 링크가 있습니다")
            jobs.append(Job(current_date, values["기업명"], links[0] if links else None,
                            values["직무"] or "원문 미기재", values["고용형태"] or "원문 미기재"))
    if not found_table:
        raise ScheduleError("채용 일정표를 찾을 수 없습니다")
    return jobs


def telegram_length(html: str) -> int:
    # UTF-16 units are conservative for Telegram's 4096-character ceiling.
    text = BeautifulSoup(html, "html.parser").get_text()
    return len(text.encode("utf-16-le")) // 2


def render_messages(jobs: list[Job], source_url: str, limit: int = MESSAGE_LIMIT) -> list[dict]:
    if not safe_url(source_url, source_url):
        raise ScheduleError("출처 링크가 올바르지 않습니다")
    groups = defaultdict(list)
    for job in jobs:
        name = escape(job.company)
        if job.url:
            name = f'<a href="{escape(job.url, quote=True)}">{name}</a>'
        groups[job.deadline].append(
            f"<b>{name}</b>\n직무: {escape(job.duties)}\n고용: {escape(job.employment)}"
        )
    footer = f'<a href="{escape(source_url, quote=True)}">출처: 공취모 주간 채용 일정표</a>'
    messages = []
    for deadline, blocks in sorted(groups.items()):
        title = f"⏳ {deadline.month}/{deadline.day}({WEEKDAYS[deadline.weekday()]}) 마감 · {len(blocks)}건"

        def message(parts, suffix=""):
            return f"<b>{title}{suffix}</b>\n\n" + "\n\n".join(parts) + f"\n\n{footer}"

        if telegram_length(message(blocks)) <= limit:
            chunks = [blocks]
        else:
            # At most one chunk per employer: reserve sufficient digits for all labels.
            reserve = f" ({len(blocks)}/{len(blocks)})"
            chunks, chunk = [], []
            for block in blocks:
                if telegram_length(message([block], reserve)) > limit:
                    raise ScheduleError("한 기업의 설명이 텔레그램 메시지 한도를 초과합니다")
                if chunk and telegram_length(message(chunk + [block], reserve)) > limit:
                    chunks.append(chunk)
                    chunk = []
                chunk.append(block)
            if chunk:
                chunks.append(chunk)
        for index, chunk in enumerate(chunks, 1):
            suffix = f" ({index}/{len(chunks)})" if len(chunks) > 1 else ""
            messages.append({"key": f"{deadline.isoformat()}:{index}",
                             "deadline": deadline.isoformat(), "text": message(chunk, suffix)})
    return messages


def fetch_article(article_id: str, timeout_ms: int = 45000) -> Article:
    """Render the public article; no login cookies, private API or OCR required."""
    from playwright.sync_api import sync_playwright

    url = article_url(article_id)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="ko-KR", timezone_id="Asia/Seoul")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            end = time.monotonic() + timeout_ms / 1000
            while time.monotonic() < end:
                for frame in page.frames:
                    try:
                        tables = frame.locator("table").all()
                        schedule_tables = []
                        for table in tables:
                            if all(header in clean(table.inner_text(timeout=1000)) for header in HEADERS):
                                schedule_tables.append(table.evaluate("element => element.outerHTML"))
                        if not schedule_tables:
                            continue
                        # Restrict the date lookup to the article's publication metadata.
                        metadata = frame.locator(".article_info .date, .ArticleWriter .date, time[datetime]").all()
                        for node in metadata:
                            raw = node.get_attribute("datetime") or node.inner_text(timeout=1000)
                            match = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw)
                            if match:
                                published = date(*(int(value) for value in match.groups()))
                                return Article("\n".join(schedule_tables), published, url)
                        raise ScheduleError("게시글 작성일을 찾을 수 없습니다")
                    except ScheduleError:
                        raise
                    except Exception:
                        # Navigation may replace an iframe while it is being inspected.
                        continue
                page.wait_for_timeout(500)
            raise ScheduleError("공개 본문 또는 채용 일정표를 불러오지 못했습니다")
        finally:
            browser.close()

