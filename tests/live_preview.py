"""Explicit network smoke test. Never sends Telegram messages or updates state."""

from datetime import date
import json
from pathlib import Path

from deadline_schedule import fetch_article, parse_schedule, render_messages


def test_live_example():
    article = fetch_article("3178545")
    assert article.published == date(2026, 9, 11)
    jobs = parse_schedule(article.html, article.published, article.url)
    expected = json.loads(Path(__file__).with_name("example_expected.json").read_text(encoding="utf-8"))
    actual = [[job.deadline.isoformat(), job.company, job.url.rsplit("/", 1)[-1],
               job.duties, job.employment] for job in jobs]
    assert actual == expected
    messages = render_messages(jobs, article.url)
    assert len(messages) == 5
    assert [message["text"].split("건", 1)[0].rsplit("· ", 1)[-1] for message in messages] == ["4", "5", "4", "7", "2"]

