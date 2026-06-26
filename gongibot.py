import os
import json
import re
import requests
import time
from urllib.parse import unquote_plus

# ── 설정 로드 ──────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
RAW_CHATS = os.environ.get("TELEGRAM_CHAT", "")
TARGET_CHATS = [c.strip() for c in RAW_CHATS.split(",") if c.strip()]

# ── 네이버 카페 설정 ──────────────────────
CAFE_ID = 21160703

BOARDS = {
    "중앙공기업": {"menu_id": 861,  "header": "🏢 중앙"},
    "지방공기업": {"menu_id": 2486, "header": "🏛 지방"},
    "인턴계약직": {"menu_id": 2488, "header": "📄 인턴"},
    "학교병원":  {"menu_id": 2487, "header": "🏥 학병"},
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
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {k: [] for k in ALL_SOURCE_KEYS}
                data = json.loads(content)
                if isinstance(data, list):
                    new_data = {k: [] for k in ALL_SOURCE_KEYS}
                    new_data["종합"] = data
                    return new_data
                for k in ALL_SOURCE_KEYS:
                    data.setdefault(k, [])
                return data
        except Exception as e:
            print(f"[경고] seen_posts 읽기 실패: {e}")
    return {k: [] for k in ALL_SOURCE_KEYS}

def save_seen(seen: dict):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


# ── 텔레그램 (다중 전송) ──────────────────────

def send_telegram(text: str):
    for chat_id in TARGET_CHATS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id":                  chat_id,
                    "text":                     text,
                    "parse_mode":               "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            ).raise_for_status()
        except Exception as e:
            print(f"[오류] 텔레그램 전송 실패 (대상: {chat_id}): {e}")


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
    is_first_run = all(len(v) == 0 for v in seen.values())
    total_new    = 0
    total_skip   = 0

    # 카페 확인
    for board_name, board_info in BOARDS.items():
        articles = fetch_cafe_articles(board_info["menu_id"])
        if is_first_run:
            seen[board_name] = [str(a["articleId"]) for a in articles]
            continue

        seen_ids     = set(seen.get(board_name, []))
        new_articles = [a for a in articles if str(a["articleId"]) not in seen_ids]
        new_articles.reverse()

        for a in new_articles:
            aid   = str(a["articleId"])
            title = a.get("subject", "(제목 없음)")

            # seen에 먼저 등록 (필터 결과와 무관하게 재처리 방지)
            seen[board_name].append(aid)

            if not should_send(title):
                print(f"[필터] 차단: [{board_name}] {title}")
                total_skip += 1
                continue

            url  = f"https://cafe.naver.com/ca-fe/cafes/{CAFE_ID}/articles/{aid}"
            text = f"{board_info['header']}\n★ {title}\n<a href=\"{url}\">바로가기</a>"
            send_telegram(text)
            total_new += 1
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
            seen[name].append(p["post_id"])

            if not should_send(p["title"]):
                print(f"[필터] 차단: [{name}] {p['title']}")
                total_skip += 1
                continue

            text = f"{target['header']}\n★ {p['title']}\n<a href=\"{p['link']}\">바로가기</a>"
            send_telegram(text)
            total_new += 1
            time.sleep(3)

    save_seen(seen)
    if is_first_run:
        print("✅ 초기 데이터 등록 완료.")
    else:
        print(f"✅ 모니터링 완료 — 전송 {total_new}개 / 차단 {total_skip}개")


def main():
    monitor_boards()

if __name__ == "__main__":
    main()
