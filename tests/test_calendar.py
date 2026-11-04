from datetime import date
from pathlib import Path
from urllib.parse import quote

import pytest

import calendar_schedule as cal
import gongibot
from deadline_schedule import ScheduleError
from telegram_delivery import DeliveryError

ROOT = Path(__file__).parent.parent
NOVEMBER = cal.CalendarArticle("3207876", 2026, 11, "2026년 11월 공취모 공기업 채용 달력",
                               "https://cafe.naver.com/ca-fe/cafes/21160703/articles/3207876")
LIST_ITEM = {"articleId": 3207876, "subject": "[필독] 2026년 11월 공취모 공기업 채용 달력"}


def label_row(name):
    src = f"https://postfiles.pstatic.net/x/{quote(name + '.png')}?type=w1"
    return "<tr>" + f'<td><img src="{src}"></td>' * 2 + "</tr>"


def table(days, starts, deadlines, interviews=None):
    def content(columns):
        cells = ""
        for column in columns:
            cells += "<td>" + "".join(
                f'<p><a href="https://cafe.naver.com/dakchi/{link}">{name}</a></p>' for name, link in column
            ) + "</td>"
        return f"<tr>{cells}</tr>"

    weekday = '<tr><td><img src="https://x/a/%EC%9B%94.jpg"></td><td><img src="https://x/a/%ED%99%94.jpg"></td></tr>'
    dates = "<tr>" + "".join(f"<td><p>{day}</p></td>" for day in days) + "</tr>"
    rows = [weekday, dates, label_row("서류접수_시작"), content(starts),
            label_row("서류접수_마감"), content(deadlines), label_row("면접일"),
            content(interviews or [[("면접기관", 9)], []])]
    return "<table>" + "".join(rows) + "</table>"


def test_real_calendar_groups_openings_with_deadlines():
    html = (ROOT / "tests/fixtures/calendar_3207876.html").read_text(encoding="utf-8")
    entries = cal.parse_calendar(html, NOVEMBER)
    assert {label for label, *_ in entries} == {cal.START_LABEL, cal.DEADLINE_LABEL}
    openings = [o for o in cal.build_openings([(NOVEMBER, entries)]) if cal.is_open(o, date(2026, 11, 4))]
    summary = [(o.start.isoformat(), o.company, o.deadline and o.deadline.isoformat()) for o in openings]
    assert summary == [
        ("2026-11-02", "한국산업인력공단", "2026-11-10"),
        ("2026-11-03", "서울교통공사", "2026-11-09"),
        ("2026-11-03", "인천교통공사", "2026-11-11"),
        ("2026-11-03", "한국관광공사", "2026-11-11"),
        ("2026-11-03", "한국남부발전", "2026-11-11"),
        ("2026-11-09", "서울도시철도그린환경", None),  # Deadline is in week 3, outside the fixture.
        ("2026-11-11", "서울물재생시설공단", None),
    ]
    messages = cal.render_messages(openings, set())
    assert len(messages) == 4
    assert messages[1]["text"].startswith("<b>⏳ 11/3(화) 서류 접수 시작 · 4건</b>")
    assert '<a href="https://cafe.naver.com/dakchi/3199565">서울교통공사</a> (~11/9 마감)' in messages[1]["text"]
    assert "면접" not in messages[1]["text"]
    assert messages[1]["text"].endswith('">출처: 2026년 11월 공취모 공기업 채용 달력</a>')


def test_closed_and_undated_past_openings_are_skipped():
    html = table(["11/02", "11/03"], [[("가공사", 1)], [("나공단", 2)]], [[], [("가공사", 1)]])
    openings = cal.build_openings([(NOVEMBER, cal.parse_calendar(html, NOVEMBER))])
    assert [o.company for o in openings if cal.is_open(o, date(2026, 11, 3))] == ["가공사", "나공단"]
    assert [o.company for o in openings if cal.is_open(o, date(2026, 11, 4))] == []


def test_dates_crossing_the_year_follow_the_article_month():
    december = cal.CalendarArticle("1", 2026, 12, "t", NOVEMBER.url)
    january = cal.CalendarArticle("2", 2027, 1, "t", NOVEMBER.url)
    assert cal.cell_date("01/02", december) == date(2027, 1, 2)
    assert cal.cell_date("12/29", january) == date(2026, 12, 29)


def test_unknown_row_label_stops_instead_of_guessing():
    html = table(["11/02", "11/03"], [[], []], [[], []]).replace(quote("면접일"), quote("기타일정"))
    with pytest.raises(ScheduleError):
        cal.parse_calendar(html, NOVEMBER)


def test_select_articles_keeps_latest_two_monthly_calendars():
    items = [{"articleId": 3, "subject": "[필독] 2026년 10월 공취모 공기업 채용 달력"},
             {"articleId": 9, "subject": "공지사항"},
             LIST_ITEM,
             {"articleId": 1, "subject": "[필독] 2026년 9월 공취모 공기업 채용 달력"}]
    selected = cal.select_articles(items)
    assert [(a.article_id, a.month) for a in selected] == [("3207876", 11), ("3", 10)]
    assert selected[0].title == "2026년 11월 공취모 공기업 채용 달력"


@pytest.fixture
def calendar_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gongibot, "TARGET_CHATS", ["chat-one", "chat-two"])
    monkeypatch.setattr(gongibot, "TELEGRAM_TOKEN", "test-token")
    monkeypatch.setattr(gongibot.time, "sleep", lambda _: None)
    monkeypatch.setattr(gongibot, "fetch_cafe_articles", lambda menu: [LIST_ITEM] if menu == 2402 else [])
    pages = {"html": table(["11/09", "11/10"], [[("가공사", 1)], [("나공단", 2)]],
                           [[], [("가공사", 1), ("나공단", 2)]])}
    monkeypatch.setattr(cal, "fetch_calendar_html", lambda _: pages["html"])
    calls = []
    monkeypatch.setattr(gongibot, "send_message", lambda token, chat, text: calls.append((chat, text)))
    return pages, calls


def test_only_new_openings_are_sent_once(calendar_run):
    pages, calls = calendar_run
    assert gongibot.monitor_calendar(date(2026, 11, 9)) == 0
    assert len(calls) == 4
    calls.clear()
    assert gongibot.monitor_calendar(date(2026, 11, 9)) == 0
    assert calls == []
    pages["html"] = table(["11/09", "11/10"], [[("가공사", 1), ("다재단", 3)], [("나공단", 2)]],
                          [[], [("가공사", 1), ("나공단", 2), ("다재단", 3)]])
    assert gongibot.monitor_calendar(date(2026, 11, 9)) == 0
    assert len(calls) == 2
    assert all("11/9(월) 서류 접수 시작 · 추가 1건" in text and "다재단" in text and "가공사" not in text
               for _, text in calls)


def test_changed_deadline_is_announced_once(calendar_run):
    pages, calls = calendar_run
    assert gongibot.monitor_calendar(date(2026, 11, 9)) == 0
    calls.clear()
    pages["html"] = table(["11/09", "11/10"], [[("가공사", 1)], [("나공단", 2)]],
                          [[("나공단", 2)], [("가공사", 1)]])
    assert gongibot.monitor_calendar(date(2026, 11, 9)) == 0
    assert len(calls) == 0  # 나공단 now closes before it starts; no matching deadline remains.
    pages["html"] = table(["11/12", "11/13"], [[], []], [[], [("가공사", 1)]]) + table(
        ["11/09", "11/10"], [[("가공사", 1)], [("나공단", 2)]], [[], [("나공단", 2)]])
    assert gongibot.monitor_calendar(date(2026, 11, 9)) == 0
    assert len(calls) == 2
    assert all(text.startswith("<b>⏳ 마감일 변경 · 1건</b>") and "가공사</a> (~11/10 → ~11/13)" in text
               for _, text in calls)
    calls.clear()
    assert gongibot.monitor_calendar(date(2026, 11, 9)) == 0
    assert calls == []


def test_failed_destination_resumes_without_resending(calendar_run, monkeypatch):
    _, calls = calendar_run

    def send(token, chat, text):
        if chat == "chat-one" and "11/10" in text.split("\n", 1)[0]:
            raise DeliveryError("temporary")
        calls.append((chat, text))

    monkeypatch.setattr(gongibot, "send_message", send)
    assert gongibot.monitor_calendar(date(2026, 11, 9)) == 1
    assert len(calls) == 3
    calls.clear()
    monkeypatch.setattr(gongibot, "send_message", lambda token, chat, text: calls.append((chat, text)))
    assert gongibot.monitor_calendar(date(2026, 11, 9)) == 0
    assert len(calls) == 1 and calls[0][0] == "chat-one" and "11/10(화)" in calls[0][1]
    assert "추가" not in calls[0][1]
