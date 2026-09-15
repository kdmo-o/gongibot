from datetime import date
from html import escape
import json
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from deadline_schedule import Job, ScheduleError, article_url, parse_deadline, parse_schedule, render_messages, telegram_length

SOURCE = article_url("3178545")
PUBLISHED = date(2026, 9, 11)


def table(rows, headers=("마감기간", "기업명", "직무", "고용형태")):
    return "<table><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>" + "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows
    ) + "</table>"


def test_captured_example():
    root = Path(__file__).parent
    html = (root / "fixtures/example_3178545.html").read_text(encoding="utf-8")
    jobs = parse_schedule(html, PUBLISHED, SOURCE)
    expected = json.loads((root / "example_expected.json").read_text(encoding="utf-8"))
    assert [[j.deadline.isoformat(), j.company, j.url.rsplit("/", 1)[-1], j.duties, j.employment]
            for j in jobs] == expected
    messages = render_messages(jobs, SOURCE)
    assert len(messages) == 5
    assert [BeautifulSoup(m["text"], "html.parser").find("b").get_text()
            for m in messages] == [
        "⏳ 9/14(월) 마감 · 4건", "⏳ 9/15(화) 마감 · 5건", "⏳ 9/16(수) 마감 · 4건",
        "⏳ 9/17(목) 마감 · 7건", "⏳ 9/18(금) 마감 · 2건",
    ]


def test_reordered_headers_blank_date_and_missing_fields():
    html = table([
        ["정규직", '<a href="/dakchi/123">가 기업</a>', "~9/14(월)", "경영<br>IT"],
        ["", "나 기업", "\u200b", ""],
        ["", "\u200b", "~9/15(화)", ""],
    ], ("고용형태", "기업명", "마감기간", "직무"))
    jobs = parse_schedule(html, PUBLISHED, SOURCE)
    assert len(jobs) == 2
    assert jobs[0] == Job(date(2026, 9, 14), "가 기업", "https://cafe.naver.com/dakchi/123", "경영 IT", "정규직")
    assert jobs[1] == Job(date(2026, 9, 14), "나 기업", None, "원문 미기재", "원문 미기재")


def test_merged_cells_and_repeated_headers():
    html = '''<table><tr><th>마감기간</th><th>기업명</th><th>직무</th><th>고용형태</th></tr>
    <tr><td rowspan="2">~9/14(월)</td><td>가</td><td>경영</td><td rowspan="2">정규직</td></tr>
    <tr><td>나</td><td>IT</td></tr>
    <tr><th>마감기간</th><th>기업명</th><th>직무</th><th>고용형태</th></tr>
    <tr><td>~9/15(화)</td><td>다</td><td colspan="2">원문 미기재</td></tr></table>'''
    jobs = parse_schedule(html, PUBLISHED, SOURCE)
    assert [j.company for j in jobs] == ["가", "나", "다"]
    assert jobs[1].deadline == date(2026, 9, 14)
    assert jobs[1].employment == "정규직"
    assert jobs[2].duties == jobs[2].employment == "원문 미기재"


@pytest.mark.parametrize("raw,published,expected", [
    ("~1/2(토)", date(2026, 12, 30), date(2027, 1, 2)),
    ("12/31(목)", date(2027, 1, 2), date(2026, 12, 31)),
    ("2028.2.29(화)", date(2028, 2, 1), date(2028, 2, 29)),
    ("~9/14(월)", date(2026, 9, 20), date(2026, 9, 14)),
])
def test_dates(raw, published, expected):
    assert parse_deadline(raw, published) == expected


@pytest.mark.parametrize("raw", ["미정", "~2/30", "~9/14(화)", "13/1", "2026/2/29", "~1/1"])
def test_invalid_dates_fail(raw):
    with pytest.raises(ScheduleError):
        parse_deadline(raw, PUBLISHED)


@pytest.mark.parametrize("html", [
    "<p>로그인이 필요합니다</p>",
    table([["", "회사", "사무", "정규직"]]),
    table([["~9/14", "", "사무", "정규직"]]),
    table([["~9/14", "회사", "사무"]]),
    table([["~9/14", '<a href="https://a.test">가</a><a href="https://b.test">나</a>', "IT", "정규직"]]),
])
def test_incomplete_tables_fail_as_a_whole(html):
    with pytest.raises(ScheduleError):
        parse_schedule(html, PUBLISHED, SOURCE)


def test_ignore_advertisement_and_escape_unsafe_markup():
    html = '<a href="https://ads.test">광고</a>' + table([
        ["~9/14", '<a href="javascript:alert(1)">A &amp; B &lt;기관&gt;</a>', "개발 &amp; 기획", "정규직"],
    ])
    jobs = parse_schedule(html, PUBLISHED, SOURCE)
    assert jobs[0].url is None
    text = render_messages(jobs, SOURCE)[0]["text"]
    assert "A &amp; B &lt;기관&gt;" in text
    assert "javascript" not in text and "ads.test" not in text


def test_sort_and_split_only_between_employers():
    jobs = [Job(date(2026, 9, 15), "다", None, "사무", "정규직")]
    jobs += [Job(date(2026, 9, 14), f"기업{i}", f"https://example.test/{i}?a=1&b=2", "💼" * 500, "정규직") for i in range(8)]
    messages = render_messages(jobs, SOURCE)
    assert len(messages) > 2
    assert messages[-1]["deadline"] == "2026-09-15"
    companies = []
    for message in messages:
        assert telegram_length(message["text"]) <= 4096
        soup = BeautifulSoup(message["text"], "html.parser")
        companies += [anchor.get_text() for anchor in soup.select("b a")]
        assert soup.find("a", string="출처: 공취모 주간 채용 일정표")
    assert companies == [f"기업{i}" for i in range(8)]
    assert "(1/" in messages[0]["text"]


def test_one_oversize_employer_is_not_truncated():
    with pytest.raises(ScheduleError):
        render_messages([Job(date(2026, 9, 14), "가", None, "X" * 4096, "정규직")], SOURCE)


def test_exact_length_boundary_and_empty_schedule():
    job = Job(date(2026, 9, 14), "가", None, "IT", "정규직")
    message = render_messages([job], SOURCE)[0]["text"]
    assert render_messages([job], SOURCE, limit=telegram_length(message))
    assert render_messages([], SOURCE) == []

