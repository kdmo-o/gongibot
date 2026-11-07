"""Read the monthly recruitment calendar and announce application openings."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from html import escape
import re
import time
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from deadline_schedule import WEEKDAYS, MESSAGE_LIMIT, ScheduleError, article_url, clean, safe_url, telegram_length

TITLE_PATTERN = re.compile(r"(\d{4})년\s*(\d{1,2})월.*채용\s*달력")
# Row labels are images; their file names are the only machine-readable label.
START_LABEL = "서류접수_시작"
DEADLINE_LABEL = "서류접수_마감"
IGNORED_LABELS = {"면접일", "필기시험일"}
KNOWN_LABELS = {START_LABEL, DEADLINE_LABEL} | IGNORED_LABELS


@dataclass(frozen=True)
class CalendarArticle:
    article_id: str
    year: int
    month: int
    title: str
    url: str


@dataclass(frozen=True)
class Opening:
    start: date
    deadline: date | None
    company: str
    url: str | None
    source: CalendarArticle

    @property
    def key(self) -> str:
        return f"{self.start.isoformat()}|{self.company}|{self.url or ''}"


def select_articles(articles: list) -> list[CalendarArticle]:
    """Latest two monthly calendars: the previous month still covers overlapping dates."""
    found = {}
    for item in articles:
        match = TITLE_PATTERN.search(item.get("subject", ""))
        if not match:
            continue
        aid = str(item["articleId"])
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            continue
        title = clean(re.sub(r"^\s*\[[^\]]*\]\s*", "", item["subject"]))
        found.setdefault((year, month), CalendarArticle(aid, year, month, title, article_url(aid)))
    if not found:
        raise ScheduleError("채용 달력 게시글을 찾을 수 없습니다")
    return [found[key] for key in sorted(found, reverse=True)[:2]]


def cell_date(value: str, article: CalendarArticle) -> date:
    month, day = (int(part) for part in value.split("/"))
    year = article.year
    if month - article.month > 6:
        year -= 1
    elif article.month - month > 6:
        year += 1
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ScheduleError(f"존재하지 않는 달력 날짜: {value}") from exc


def image_label(cell) -> str | None:
    image = cell.find("img")
    if image is None:
        return None
    source = image.get("src") or image.get("data-src") or ""
    name = unquote(urlsplit(source).path.rsplit("/", 1)[-1])
    return name.rsplit(".", 1)[0]


def parse_calendar(html: str, article: CalendarArticle) -> list[tuple[str, date, str, str | None]]:
    """Return (label, date, company, url) for start and deadline rows."""
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    labels_seen = set()
    for table in soup.find_all("table"):
        if table.find_parent("table"):
            continue
        dates = None
        label = None
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            names = [image_label(cell) for cell in cells]
            if cells and all(names):
                if len(set(names)) == 1:
                    if names[0] not in KNOWN_LABELS:
                        raise ScheduleError(f"알 수 없는 달력 행 라벨: {names[0]}")
                    label = names[0]
                    labels_seen.add(label)
                # Rows with differing images are the weekday header.
                continue
            texts = [clean(cell.get_text(" ")) for cell in cells]
            if cells and all(re.fullmatch(r"\d{1,2}/\d{1,2}", text) for text in texts):
                dates = [cell_date(text, article) for text in texts]
                label = None
                continue
            if label is None:
                continue
            if label in (START_LABEL, DEADLINE_LABEL):
                if dates is None or len(dates) != len(cells):
                    raise ScheduleError("달력 날짜와 기업 칸이 맞지 않습니다")
                for day, cell in zip(dates, cells):
                    for paragraph in cell.find_all("p") or [cell]:
                        company = clean(paragraph.get_text(" "))
                        if not company:
                            continue
                        anchor = paragraph.find("a", href=True)
                        link = safe_url(anchor["href"], article.url) if anchor else None
                        entries.append((label, day, company, link))
            label = None
    if not {START_LABEL, DEADLINE_LABEL} <= labels_seen:
        raise ScheduleError("달력에서 서류접수 시작·마감 행을 찾을 수 없습니다")
    return entries


def build_openings(calendars: list[tuple[CalendarArticle, list]]) -> list[Opening]:
    deadlines = []
    starts = {}
    for article, entries in calendars:
        for label, day, company, link in entries:
            if label == DEADLINE_LABEL:
                deadlines.append((day, company, link))
            elif label == START_LABEL:
                starts.setdefault((day, company, link), article)
    openings = []
    for (start, company, link), article in starts.items():
        candidates = [day for day, name, url in deadlines if day >= start and name == company]
        # Prefer the same posting when a company has several deadlines.
        same_link = [day for day, name, url in deadlines if day >= start and name == company and link and url == link]
        deadline = min(same_link or candidates, default=None)
        openings.append(Opening(start, deadline, company, link, article))
    return sorted(openings, key=lambda item: (item.start, item.company))


def is_open(opening: Opening, today: date) -> bool:
    # Without a deadline, only openings that have not started yet are certainly current.
    if opening.deadline is None:
        return opening.start >= today
    return opening.deadline >= today


EMPLOYMENT_PATTERN = re.compile(
    r"무기계약직|공무직|정규직|계약직|기간제|(?:채용형|체험형)\s*(?:청년\s*)?인턴|청년\s*인턴|인턴"
)
# Parenthesised periods such as "(1분기)" or "(2차)" are not job names.
PERIOD_PATTERN = re.compile(r"(?:\d+\s*(?:분기|차|회차?|년|월)|상반기|하반기)")
POSTING_PATTERN = re.compile(r"cafe\.naver\.com/(?:dakchi|ca-fe/cafes/21160703/articles)/(\d+)")
MAX_DUTIES = 5


@dataclass(frozen=True)
class PostDetails:
    employment: str | None
    duties: str | None


def parse_post_details(title: str, rows: dict) -> PostDetails:
    """Employment from the title and the 채용구분 row; duties from the title's parentheses."""
    kinds = []
    for match in EMPLOYMENT_PATTERN.finditer(title):
        kind = re.sub(r"\s+", " ", match.group(0))
        if kind not in kinds:
            kinds.append(kind)
    category = clean(rows.get("채용구분", ""))
    if category:
        kinds.append(category)
    duties = None
    for group in re.findall(r"\(([^()]*)\)", title):
        group = clean(group)
        # Skip deadline notes such as "(~11/16)".
        if (not group or group.startswith(("~", "～")) or PERIOD_PATTERN.fullmatch(group)
                or re.fullmatch(r"[\d/.~\s()월화수목금토일]+", group)):
            continue
        names = [clean(name) for name in re.split(r"[/,·]", group) if clean(name)]
        if names:
            shown = ", ".join(names[:MAX_DUTIES])
            duties = shown + (f" 외 {len(names) - MAX_DUTIES}개" if len(names) > MAX_DUTIES else "")
        break
    return PostDetails(" · ".join(kinds) or None, duties)


def posting_url(url: str | None) -> str | None:
    match = POSTING_PATTERN.search(url or "")
    return article_url(match.group(1)) if match else None


def fetch_post_details(urls: list[str], timeout_ms: int = 20000) -> dict:
    """Read every posting first with one browser; unreadable postings are simply omitted."""
    from playwright.sync_api import sync_playwright

    details = {}
    targets = {url: posting_url(url) for url in dict.fromkeys(urls)}
    targets = {url: target for url, target in targets.items() if target}
    if not targets:
        return details
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="ko-KR", timezone_id="Asia/Seoul")
            for url, target in targets.items():
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_selector(".title_text", timeout=timeout_ms)
                    title = page.locator(".title_text").first.inner_text()
                    rows = {}
                    try:
                        page.wait_for_selector(".se-main-container table", timeout=5000)
                        table = page.locator(".se-main-container table").first
                        for row in table.locator("tr").all():
                            cells = [clean(cell) for cell in row.locator("td").all_inner_texts()]
                            if len(cells) >= 2 and cells[0]:
                                rows[cells[0]] = cells[1]
                    except Exception:
                        pass  # Postings without the summary table still have a usable title.
                    details[url] = parse_post_details(clean(title), rows)
                except Exception:
                    print("[알림] 공고 글을 읽지 못해 고용·직무 없이 보냅니다")
        finally:
            browser.close()
    return details


def render_messages(openings: list[Opening], announced_dates: set[str], details: dict | None = None) -> list[dict]:
    details = details or {}
    groups = defaultdict(list)
    for opening in openings:
        groups[opening.start].append(opening)
    messages = []
    for start, items in sorted(groups.items()):
        added = " · 추가" if start.isoformat() in announced_dates else ""
        source = items[0].source
        icon = f'<a href="{escape(source.url, quote=True)}">⏳</a>'
        title = f"{icon} {start.month}/{start.day}({WEEKDAYS[start.weekday()]}) 시작{added}"
        lines = []
        for item in items:
            name = escape(item.company)
            if item.url:
                name = f'<a href="{escape(item.url, quote=True)}">{name}</a>'
            due = f"~{item.deadline.month}/{item.deadline.day} 마감" if item.deadline else "마감일 미기재"
            block = f"{name} ({due})"
            info = details.get(item.url)
            if info and info.employment:
                block += f"\n고용: {escape(info.employment)}"
            if info and info.duties:
                block += f"\n직무: {escape(info.duties)}"
            lines.append(block)
        text = f"<b>{title}</b>\n\n" + "\n\n".join(lines)
        if telegram_length(text) > MESSAGE_LIMIT:
            raise ScheduleError("서류 접수 시작 알림이 텔레그램 메시지 한도를 초과합니다")
        messages.append({"start": start.isoformat(), "keys": [item.key for item in items], "text": text})
    return messages


def render_deadline_changes(changes: list[tuple[Opening, date]]) -> dict:
    lines = []
    for item, old in sorted(changes, key=lambda change: (change[0].deadline, change[0].company)):
        name = escape(item.company)
        if item.url:
            name = f'<a href="{escape(item.url, quote=True)}">{name}</a>'
        new = item.deadline
        lines.append(f"{name} (~{old.month}/{old.day} → ~{new.month}/{new.day})")
    source = changes[0][0].source
    text = f'<b><a href="{escape(source.url, quote=True)}">⏳</a> 마감일 변경</b>\n\n' + "\n".join(lines)
    if telegram_length(text) > MESSAGE_LIMIT:
        raise ScheduleError("마감일 변경 알림이 텔레그램 메시지 한도를 초과합니다")
    return {"keys": [item.key for item, _ in changes], "text": text}


def fetch_calendar_html(article_id: str, timeout_ms: int = 45000) -> str:
    """Render the public article and return its calendar tables."""
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
                        tables = [table.evaluate("element => element.outerHTML")
                                  for table in frame.locator("table").all()]
                    except Exception:
                        # Navigation may replace an iframe while it is being inspected.
                        continue
                    if any(START_LABEL in unquote(table) for table in tables):
                        return "\n".join(tables)
                page.wait_for_timeout(500)
            raise ScheduleError("공개 본문 또는 채용 달력을 불러오지 못했습니다")
        finally:
            browser.close()
