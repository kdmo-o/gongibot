import json

import pytest
import requests

from deadline_delivery import DeadlineQueue, recipient_key
from telegram_delivery import DeliveryError, send_message


MESSAGES = [
    {"key": "2026-09-14:1", "deadline": "2026-09-14", "text": "first"},
    {"key": "2026-09-15:1", "deadline": "2026-09-15", "text": "second"},
]


def test_partial_delivery_restart_and_frozen_content(tmp_path):
    path = tmp_path / "state.json"
    queue = DeadlineQueue(path)
    queue.register("12")
    queue.freeze("12", MESSAGES)
    calls = []

    def sender(chat, text):
        calls.append((chat, text))
        if chat == "private-chat-a" and text == "second":
            raise DeliveryError("temporary")

    assert not queue.deliver("12", ["private-chat-a", "private-chat-b"], sender, sleep=lambda _: None)
    saved = path.read_text(encoding="utf-8")
    assert "private-chat-a" not in saved and "private-chat-b" not in saved
    assert recipient_key("private-chat-a") in saved
    queue = DeadlineQueue(path)
    queue.freeze("12", [{"text": "edited article"}])
    resumed = []
    assert queue.deliver("12", ["private-chat-a", "private-chat-b"],
                         lambda chat, text: resumed.append((chat, text)), sleep=lambda _: None)
    assert resumed == [("private-chat-a", "second")]
    assert queue.deliver("12", ["private-chat-a", "private-chat-b"],
                         lambda *_: pytest.fail("already sent"), sleep=lambda _: None)
    queue.remove_completed({"12"})
    assert not DeadlineQueue(path).data["articles"]


def test_no_recipients_is_not_success_and_queue_survives_before_fetch(tmp_path):
    path = tmp_path / "state.json"
    queue = DeadlineQueue(path)
    queue.register("12")
    assert DeadlineQueue(path).data["articles"]["12"]["messages"] is None
    queue.freeze("12", MESSAGES)
    assert not queue.deliver("12", [], lambda *_: None)


def test_corrupt_state_is_not_silently_reset(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"version": 90}', encoding="utf-8")
    with pytest.raises(ValueError):
        DeadlineQueue(path)


def test_save_failure_propagates(tmp_path, monkeypatch):
    queue = DeadlineQueue(tmp_path / "state.json")
    queue.register("12")
    queue.freeze("12", MESSAGES)
    monkeypatch.setattr(queue, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        queue.deliver("12", ["chat"], lambda *_: None, sleep=lambda _: None)


def test_rate_limit_backoff_survives_restart(tmp_path, monkeypatch):
    import deadline_delivery
    monkeypatch.setattr(deadline_delivery.time, "time", lambda: 1000)
    path = tmp_path / "state.json"
    queue = DeadlineQueue(path)
    queue.register("12")
    queue.freeze("12", MESSAGES)
    assert not queue.deliver("12", ["chat"],
                             lambda *_: (_ for _ in ()).throw(DeliveryError("rate limit", retry_after=90)),
                             sleep=lambda _: None)
    queue = DeadlineQueue(path)
    assert not queue.deliver("12", ["chat"], lambda *_: pytest.fail("early retry"))
    monkeypatch.setattr(deadline_delivery.time, "time", lambda: 1090)
    assert queue.deliver("12", ["chat"], lambda *_: None, sleep=lambda _: None)


class Response:
    def __init__(self, status, data):
        self.status_code = status
        self.data = data

    def json(self):
        return self.data


def test_rate_limit_then_success_and_payload():
    responses = [Response(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}),
                 Response(200, {"ok": True, "result": {"message_id": 9}})]
    calls, waits = [], []

    def post(url, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    assert send_message("token", "chat", "<b>test</b>", post=post, sleep=waits.append) == 9
    assert waits == [7]
    assert calls[0]["json"]["parse_mode"] == "HTML"
    assert calls[0]["json"]["disable_web_page_preview"] is True


def test_long_rate_limit_is_deferred_without_early_retry():
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        return Response(429, {"error_code": 429, "parameters": {"retry_after": 1000}})

    with pytest.raises(DeliveryError):
        send_message("secret", "chat", "hi", post=post, sleep=lambda _: pytest.fail("should defer"))
    assert len(calls) == 1


def test_network_failure_retries_without_exposing_token():
    calls, waits = [], []

    def post(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError("https://api.telegram.org/botTOPSECRET/sendMessage")

    with pytest.raises(DeliveryError) as error:
        send_message("TOPSECRET", "chat", "hi", post=post, sleep=waits.append)
    assert len(calls) == 3 and waits == [1, 2]
    assert "TOPSECRET" not in str(error.value)


@pytest.mark.parametrize("status,data", [(400, {"ok": False, "error_code": 400}),
                                         (200, {"ok": False, "error_code": 403}),
                                         (200, {"ok": True, "result": {}})])
def test_false_or_malformed_success_is_failure(status, data):
    with pytest.raises(DeliveryError):
        send_message("token", "chat", "hi", post=lambda *a, **kw: Response(status, data), sleep=lambda _: None)

