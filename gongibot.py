import os
import argparse
from datetime import datetime, timedelta, timezone
from html import escape

import calendar_schedule
from deadline_schedule import fetch_article, parse_schedule, render_messages, ScheduleError
from deadline_delivery import DeadlineQueue, recipient_key, save_json
from telegram_delivery import send_message, DeliveryError
import json
import re
import requests
import time
from urllib.parse import unquote_plus

# ── 설정 로드 ──────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
RAW_CHATS = os.environ.get("TELEGRAM_CHAT", "")
TARGET_CHATS = [c.strip() for c in RAW_CHATS.split(",") if c.strip()]

# ── 네이버 카페 설정 ──────────────────────
CAFE_ID = 21160703

CALENDAR_MENU_ID = 2402
CALENDAR_STATE_FILE = "calendar_state.json"
KST = timezone(timedelta(hours=9))

BOARDS = {
    "마감":      {"menu_id": 2696, "header": "⏳ 마감"},
    "종합":      {"menu_id": 2510, "header": "🔴 종합"},
}

# ── 네이버 블로그 설정 ────────────────────
BLOG_TARGETS = [
    {
        "name":        "최신채용공고",
        "blog_id":     "ekfzhaduddj",
        "category_no": 15,
        "header":      "🟢 정리",
    },
]

# ── 키워드 필터 ───────────────────────────
ALLOW_KEYWORDS = [
    "정규직", "인턴", "행정", "사무", "경영", "기획", "청년", "채용형", "체험형",
    "신입", "공개채용", "공채", "일반", "일경험", "통합", "공공기관", "대졸"
]
EXCLUDE_KEYWORDS = [
    "환경관리", "치과위생사", "의사직", "간호직", "응급구조사", "의료직", "간호사",
    "의사", "약사", "방사선사", "정비보조", "촉탁", "임상병리사", "치과기공사",
    "물리치료사", "임상교수", "교수", "약무직", "영양사", "연구원", "조리사",
    "공공급식", "생산관리", "조리원", "시간강사", "수영강습", "강사", "장애",
    "경력", "위촉", "단기노무원", "보훈", "별정직", "전기분야", "식당", "제한",
    "작업원", "순찰", "기계", "선수", "전문의", "연구직", "연구위원", "의무직", 
    "개방형", "안전요원", "전문계약", "전문경력", "음식조리", "전문감사", "위원",
    "전문인력", "변호사"
]

def should_send(title: str) -> bool:
    """
    True  → 발송
    False → 차단
    규칙:
      1. ALLOW 포함 → 발송 (EXCLUDE 무관)
      2. ALLOW 없고 EXCLUDE 포함 → 차단
      3. 둘 다 없음 → 발송
    """
    has_allow   = any(kw in title for kw in ALLOW_KEYWORDS)
    has_exclude = any(kw in title for kw in EXCLUDE_KEYWORDS)

    if has_allow:
        return True
    if has_exclude:
        return False
    return True


SEEN_FILE       = "seen_posts.json"
ALL_SOURCE_KEYS = list(BOARDS.keys()) + [b["name"] for b in BLOG_TARGETS]


# ── seen 관리 ────────────────────

def load_seen() -> dict:
    if not os.path.exists(SEEN_FILE):
        return {k: [] for k in ALL_SOURCE_KEYS}
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        raise ValueError("seen_posts 파일이 비어 있습니다. 발송 이력을 복구해야 합니다")
    data = json.loads(content)
    if isinstance(data, list):
        data = {"종합": data}
    if not isinstance(data, dict) or any(
        not isinstance(value, list) or any(not isinstance(aid, str) for aid in value)
        for value in data.values()
    ):
        raise ValueError("seen_posts 파일 형식 오류")
    for key in ALL_SOURCE_KEYS:
        data.setdefault(key, [])
    return data


def save_seen(seen: dict):
    save_json(SEEN_FILE, seen)


# ── 텔레그램 (다중 전송) ──────────────────────

def send_telegram(text: str) -> bool:
    success = True
    for chat_id in dict.fromkeys(TARGET_CHATS):
        try:
            send_message(TELEGRAM_TOKEN, chat_id, text)
        except DeliveryError as exc:
            print(f"[오류] {exc}")
            success = False
    return success and bool(TARGET_CHATS)


# ── 네이버 카페 크롤링 ────────────────────

def fetch_cafe_articles(menu_id: int) -> list:
    url = "https://apis.naver.com/cafe-web/cafe2/ArticleListV2dot1.json"
    params = {
        "search.clubid":    CAFE_ID,
        "search.menuid":    menu_id,
        "search.boardtype": "L",
        "search.page":      1,
        "search.perPage":   20,
        "ad":               "false",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Referer": f"https://cafe.naver.com/f-e/cafes/{CAFE_ID}/menus/{menu_id}",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("result", {}).get("articleList", [])
    except Exception as e:
        print(f"[오류] 카페 게시판 {menu_id} 조회 실패: {e}")
        return []


# ── 네이버 블로그 크롤링 ──────────────────

def fetch_blog_posts(blog_id: str, category_no: int) -> list:
    url = "https://blog.naver.com/PostTitleListAsync.naver"
    params = {
        "blogId":       blog_id,
        "categoryNo":   category_no,
        "currentPage":  1,
        "countPerPage": 20,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Referer":    f"https://blog.naver.com/{blog_id}",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        cleaned = re.sub(r'\\([^"\\/bfnrtu0-9])', r'\1', resp.text)
        data = json.loads(cleaned)
        posts = []
        for item in data.get("postList", []):
            post_id = str(item.get("logNo", ""))
            title   = unquote_plus(item.get("title", "(제목 없음)")).strip()
            link    = f"https://blog.naver.com/{blog_id}/{post_id}"
            if post_id:
                posts.append({"post_id": post_id, "title": title, "link": link})
        return posts
    except Exception as e:
        print(f"[오류] 블로그 {blog_id} 조회 실패: {e}")
        return []


# ── 모니터링 ──────────────────────────────

def monitor_boards():
    seen         = load_seen()
    queue        = DeadlineQueue()
    queue.remove_completed(set(seen["마감"]))
    is_first_run = all(len(v) == 0 for v in seen.values()) and not queue.data["articles"]
    had_errors   = False
    total_new    = 0
    total_skip   = 0

    # 카페 확인
    for board_name, board_info in BOARDS.items():
        articles = fetch_cafe_articles(board_info["menu_id"])
        articles = list({str(a["articleId"]): a for a in articles}.values())
        if is_first_run:
            seen[board_name] = [str(a["articleId"]) for a in articles]
            continue

        seen_ids     = set(seen.get(board_name, []))
        new_articles = [a for a in articles if str(a["articleId"]) not in seen_ids]
        new_articles.reverse()

        if board_info["menu_id"] == 2696:
            for article in new_articles:
                queue.register(str(article["articleId"]))
            for aid in sorted(queue.data["articles"], key=int):
                item = queue.data["articles"][aid]
                if item["messages"] is None:
                    try:
                        article = fetch_article(aid)
                        jobs = parse_schedule(article.html, article.published, article.url)
                        messages = render_messages(jobs, article.url)
                    except Exception as exc:
                        # Third-party exceptions can contain page/connection details.
                        detail = str(exc) if isinstance(exc, ScheduleError) else type(exc).__name__
                        print(f"[오류] 마감 게시글 {aid} 본문 처리 실패: {detail}")
                        had_errors = True
                        continue
                    queue.freeze(aid, messages)
                if queue.deliver(
                    aid, TARGET_CHATS,
                    lambda chat, text: send_message(TELEGRAM_TOKEN, chat, text),
                ):
                    seen[board_name].append(aid)
                    save_seen(seen)
                    total_new += len(item["messages"])
                else:
                    had_errors = True
            queue.remove_completed(set(seen[board_name]))
            continue

        for a in new_articles:
            aid   = str(a["articleId"])
            title = a.get("subject", "(제목 없음)")

            if not should_send(title):
                seen[board_name].append(aid)
                print(f"[필터] 차단: [{board_name}] {title}")
                total_skip += 1
                continue

            url  = f"https://cafe.naver.com/ca-fe/cafes/{CAFE_ID}/articles/{aid}"
            text = f"{board_info['header']}\n★ {escape(title)}\n<a href=\"{url}\">바로가기</a>"
            if send_telegram(text):
                seen[board_name].append(aid)
                save_seen(seen)
                total_new += 1
            else:
                had_errors = True
            time.sleep(3)

    # 블로그 확인
    for target in BLOG_TARGETS:
        name  = target["name"]
        posts = fetch_blog_posts(target["blog_id"], target["category_no"])

        if is_first_run:
            seen[name] = [p["post_id"] for p in posts]
            continue

        seen_ids  = set(seen.get(name, []))
        new_posts = [p for p in posts if p["post_id"] not in seen_ids]
        new_posts.reverse()

        for p in new_posts:
            if not should_send(p["title"]):
                seen[name].append(p["post_id"])
                print(f"[필터] 차단: [{name}] {p['title']}")
                total_skip += 1
                continue

            text = f"{target['header']}\n★ {escape(p['title'])}\n<a href=\"{escape(p['link'], quote=True)}\">바로가기</a>"
            if send_telegram(text):
                seen[name].append(p["post_id"])
                save_seen(seen)
                total_new += 1
            else:
                had_errors = True
            time.sleep(3)

    save_seen(seen)
    if is_first_run:
        print("✅ 초기 데이터 등록 완료.")
    else:
        print(f"✅ 모니터링 완료 — 전송 {total_new}개 / 차단 {total_skip}개")
    return 1 if had_errors else 0


# ── 채용 달력: 서류 접수 시작 ────────────────

def load_calendar_state() -> dict:
    if not os.path.exists(CALENDAR_STATE_FILE):
        return {"version": 1, "sent": {}, "deadlines": {}}
    with open(CALENDAR_STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != 1 or not isinstance(data.get("sent"), dict) or any(
        not isinstance(value, list) for value in data["sent"].values()
    ):
        raise ValueError("채용 달력 상태 파일 형식 오류")
    data.setdefault("deadlines", {})
    if not isinstance(data["deadlines"], dict):
        raise ValueError("채용 달력 상태 파일 형식 오류")
    return data


def collect_openings(article_list: list) -> list:
    calendars = []
    for article in calendar_schedule.select_articles(article_list):
        html = calendar_schedule.fetch_calendar_html(article.article_id)
        calendars.append((article, calendar_schedule.parse_calendar(html, article)))
    return calendar_schedule.build_openings(calendars)


def monitor_calendar(today=None) -> int:
    today = today or datetime.now(KST).date()
    state = load_calendar_state()
    sent = state["sent"]
    articles = fetch_cafe_articles(CALENDAR_MENU_ID)
    if not articles:
        raise ScheduleError("채용 달력 게시판 목록을 불러오지 못했습니다")
    openings = [item for item in collect_openings(articles)
                if calendar_schedule.is_open(item, today)]
    # Keys older than any possible open posting can no longer be re-sent.
    cutoff = (today - timedelta(days=120)).isoformat()
    deadlines = state["deadlines"]
    for key in [key for key in sent if key.split("|", 1)[0] < cutoff]:
        del sent[key]
        deadlines.pop(key, None)

    by_key = {item.key: item for item in openings}
    had_errors = False
    total = 0
    for chat in dict.fromkeys(TARGET_CHATS):
        recipient = recipient_key(chat)
        notified = {key: deadlines.get(key, {}).get(recipient) for key, recipients in sent.items()
                    if recipient in recipients}
        changes = [(item, datetime.strptime(notified[item.key], "%Y-%m-%d").date()) for item in openings
                   if notified.get(item.key) and item.deadline
                   and item.deadline.isoformat() != notified[item.key]]
        pending = [item for item in openings if item.key not in notified]
        announced = {key.split("|", 1)[0] for key in notified}
        messages = calendar_schedule.render_messages(pending, announced)
        if changes:
            messages.insert(0, calendar_schedule.render_deadline_changes(changes))
        for message in messages:
            try:
                send_message(TELEGRAM_TOKEN, chat, message["text"])
            except DeliveryError as exc:
                print(f"[오류] 채용 달력 알림: {exc}")
                had_errors = True
                break  # Keep date order for this destination; the rest follows next run.
            for key in message["keys"]:
                if recipient not in sent.setdefault(key, []):
                    sent[key].append(recipient)
                deadline = by_key[key].deadline
                deadlines.setdefault(key, {})[recipient] = deadline.isoformat() if deadline else None
            save_json(CALENDAR_STATE_FILE, state)
            total += 1
            time.sleep(3)
    save_json(CALENDAR_STATE_FILE, state)
    print(f"✅ 채용 달력 확인 완료 — 전송 {total}개")
    return 1 if had_errors else 0


def preview_calendar(article_id: str, today=None) -> int:
    today = today or datetime.now(KST).date()
    item = next((a for a in fetch_cafe_articles(CALENDAR_MENU_ID) if str(a["articleId"]) == article_id), None)
    if item is None:
        raise ScheduleError("채용 달력 게시판 목록에서 해당 글을 찾을 수 없습니다")
    openings = [o for o in collect_openings([item]) if calendar_schedule.is_open(o, today)]
    messages = calendar_schedule.render_messages(openings, set())
    for message in messages:
        print(message["text"])
        print()
    print(f"미리보기: 접수 중·예정 {len(openings)}건 / 메시지 {len(messages)}개")
    return 0


def main():
    parser = argparse.ArgumentParser(description="공기봇 채용 알림")
    parser.add_argument("--preview-article-id", help="발송 및 이력 변경 없이 일정표를 출력합니다")
    parser.add_argument("--calendar", action="store_true", help="채용 달력의 서류 접수 시작 알림을 보냅니다")
    parser.add_argument("--preview-calendar-id", help="발송 및 이력 변경 없이 채용 달력 알림을 출력합니다")
    args = parser.parse_args()
    if args.preview_calendar_id:
        return preview_calendar(args.preview_calendar_id)
    if args.preview_article_id:
        article = fetch_article(args.preview_article_id)
        jobs = parse_schedule(article.html, article.published, article.url)
        messages = render_messages(jobs, article.url)
        for message in messages:
            print(message["text"])
            print()
        print(f"미리보기: 채용 {len(jobs)}건 / 메시지 {len(messages)}개")
        return 0
    if not TELEGRAM_TOKEN or not TARGET_CHATS:
        raise ValueError("TELEGRAM_TOKEN과 TELEGRAM_CHAT 설정이 필요합니다")
    if args.calendar:
        return monitor_calendar()
    return monitor_boards()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[오류] 실행을 완료하지 못했습니다 ({type(exc).__name__})")
        raise SystemExit(1) from None

