from datetime import date
import json
from pathlib import Path
import sys

import pytest

import gongibot
from deadline_delivery import DeadlineQueue
from deadline_schedule import Article, ScheduleError, article_url
from telegram_delivery import DeliveryError

ROOT = Path(__file__).parent.parent


@pytest.fixture
def monitor(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gongibot, "TARGET_CHATS", ["chat-one", "chat-two"])
    monkeypatch.setattr(gongibot, "TELEGRAM_TOKEN", "test-token")
    monkeypatch.setattr(gongibot.time, "sleep", lambda _: None)
    monkeypatch.setattr(gongibot, "fetch_cafe_articles", lambda _: [])
    monkeypatch.setattr(gongibot, "fetch_blog_posts", lambda *_: [])
    gongibot.save_seen({name: ["old"] for name in gongibot.ALL_SOURCE_KEYS})
    return tmp_path


def example():
    return Article((ROOT / "tests/fixtures/example_3178545.html").read_text(encoding="utf-8"),
                   date(2026, 9, 11), article_url("3178545"))


def test_new_deadline_bypasses_filter_and_duplicate_list_rows(monitor, monkeypatch):
    item = {"articleId": 3178545, "subject": "연구원 경력 일정표"}
    monkeypatch.setattr(gongibot, "fetch_cafe_articles", lambda menu: [item, item] if menu == 2696 else [])
    monkeypatch.setattr(gongibot, "fetch_article", lambda _: example())
    calls = []
    monkeypatch.setattr(gongibot, "send_message", lambda token, chat, text: calls.append((chat, text)))
    assert gongibot.monitor_boards() == 0
    assert len(calls) == 10
    assert sum("9/14(월)" in text for _, text in calls) == 2
    assert gongibot.load_seen()["마감"].count("3178545") == 1
    calls.clear()
    monkeypatch.setattr(gongibot, "fetch_article", lambda _: pytest.fail("seen article fetched"))
    assert gongibot.monitor_boards() == 0
    assert not calls


def test_pending_article_survives_list_rolloff_and_partial_send(monitor, monkeypatch):
    monkeypatch.setattr(gongibot, "fetch_cafe_articles", lambda menu: [{"articleId": 3178545}] if menu == 2696 else [])
    monkeypatch.setattr(gongibot, "fetch_article", lambda _: example())
    calls = []

    def send(token, chat, text):
        calls.append((chat, text))
        if chat == "chat-one" and "9/16(수)" in text:
            raise DeliveryError("temporary")

    monkeypatch.setattr(gongibot, "send_message", send)
    assert gongibot.monitor_boards() == 1
    assert "3178545" not in gongibot.load_seen()["마감"]
    assert len(calls) == 8  # Two successful, one failed, five to the other recipient.
    monkeypatch.setattr(gongibot, "fetch_cafe_articles", lambda _: [])
    monkeypatch.setattr(gongibot, "fetch_article", lambda _: pytest.fail("frozen article fetched again"))
    calls.clear()
    monkeypatch.setattr(gongibot, "send_message", lambda token, chat, text: calls.append((chat, text)))
    assert gongibot.monitor_boards() == 0
    assert len(calls) == 3 and all(chat == "chat-one" for chat, _ in calls)
    assert "3178545" in gongibot.load_seen()["마감"]


def test_fetch_failure_is_retained_without_sending_title(monitor, monkeypatch):
    monkeypatch.setattr(gongibot, "fetch_cafe_articles", lambda menu: [{"articleId": 123, "subject": "일정표"}] if menu == 2696 else [])
    monkeypatch.setattr(gongibot, "fetch_article", lambda _: (_ for _ in ()).throw(ScheduleError("broken")))
    monkeypatch.setattr(gongibot, "send_message", lambda *_: pytest.fail("must not send"))
    assert gongibot.monitor_boards() == 1
    assert "123" not in gongibot.load_seen()["마감"]
    assert DeadlineQueue().data["articles"]["123"]["messages"] is None


def test_real_legacy_history_does_not_replay_example(monitor, monkeypatch):
    history = (ROOT / "seen_posts.json").read_bytes()
    Path("seen_posts.json").write_bytes(history)
    monkeypatch.setattr(gongibot, "fetch_cafe_articles", lambda menu: [{"articleId": 3178545}] if menu == 2696 else [])
    monkeypatch.setattr(gongibot, "fetch_article", lambda _: pytest.fail("history ignored"))
    monkeypatch.setattr(gongibot, "send_message", lambda *_: pytest.fail("history replayed"))
    assert gongibot.monitor_boards() == 0
    assert json.loads(history) == gongibot.load_seen()


def test_other_sources_keep_filters_and_message_format(monitor, monkeypatch):
    monkeypatch.setattr(gongibot, "fetch_cafe_articles", lambda menu: [
        {"articleId": 201, "subject": "경력 연구원"},
        {"articleId": 202, "subject": "정규직 & 채용"},
    ] if menu == 2510 else [])
    monkeypatch.setattr(gongibot, "fetch_blog_posts", lambda *_: [
        {"post_id": "301", "title": "일반 <공고>", "link": "https://blog.naver.com/example/301"},
        {"post_id": "302", "title": "경력 연구원", "link": "https://blog.naver.com/example/302"},
    ])
    calls = []
    monkeypatch.setattr(gongibot, "send_message", lambda token, chat, text: calls.append(text))
    assert gongibot.monitor_boards() == 0
    assert len(calls) == 4
    assert calls[0].startswith("🔴 종합\n★ 정규직 &amp; 채용")
    assert "일반 &lt;공고&gt;" in calls[-1]
    assert set(gongibot.load_seen()["종합"]) >= {"201", "202"}


def test_preview_requires_no_token_and_never_updates_history(monitor, monkeypatch, capsys):
    before = Path("seen_posts.json").read_bytes()
    monkeypatch.setattr(gongibot, "TELEGRAM_TOKEN", "")
    monkeypatch.setattr(gongibot, "TARGET_CHATS", [])
    monkeypatch.setattr(sys, "argv", ["gongibot.py", "--preview-article-id", "3178545"])
    monkeypatch.setattr(gongibot, "fetch_article", lambda _: example())
    monkeypatch.setattr(gongibot, "send_message", lambda *_: pytest.fail("preview sent"))
    assert gongibot.main() == 0
    assert "채용 22건 / 메시지 5개" in capsys.readouterr().out
    assert Path("seen_posts.json").read_bytes() == before
    assert not Path("deadline_state.json").exists()


def test_first_run_seeds_without_sending(monitor, monkeypatch):
    Path("seen_posts.json").unlink()
    monkeypatch.setattr(gongibot, "fetch_cafe_articles", lambda _: [{"articleId": 123}])
    monkeypatch.setattr(gongibot, "send_message", lambda *_: pytest.fail("first run sent"))
    assert gongibot.monitor_boards() == 0
    assert gongibot.load_seen()["마감"] == ["123"]


@pytest.mark.parametrize("content", ["", "{broken", '{"종합": 12}'])
def test_corrupt_history_stops_instead_of_resetting(monitor, content):
    Path("seen_posts.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        gongibot.load_seen()

