"""Persist a frozen schedule and successful deliveries between daily runs."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from telegram_delivery import DeliveryError


def save_json(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     suffix=".tmp", delete=False) as handle:
        temporary = handle.name
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def recipient_key(chat_id):
    return hashlib.sha256(chat_id.encode("utf-8")).hexdigest()


class DeadlineQueue:
    def __init__(self, path="deadline_state.json"):
        self.path = Path(path)
        self.data = {"version": 1, "articles": {}, "blocked_until": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("version") != 1 or not isinstance(self.data.get("articles"), dict):
                raise ValueError("마감 알림 상태 파일 형식 오류")
            self.data.setdefault("blocked_until", {})
            if not isinstance(self.data["blocked_until"], dict):
                raise ValueError("마감 알림 재시도 시간 기록 오류")
            for recipient, timestamp in self.data["blocked_until"].items():
                if len(recipient) != 64 or not isinstance(timestamp, (int, float)):
                    raise ValueError("마감 알림 재시도 시간 기록 오류")
            for aid, item in self.data["articles"].items():
                if not aid.isdigit() or not isinstance(item, dict):
                    raise ValueError("마감 알림 상태 항목 오류")
                if not isinstance(item.get("sent"), dict):
                    raise ValueError("마감 알림 전송 기록 오류")
                messages = item.get("messages")
                if messages is not None and not isinstance(messages, list):
                    raise ValueError("마감 알림 메시지 기록 오류")

    def save(self):
        save_json(self.path, self.data)

    def register(self, aid):
        if aid not in self.data["articles"]:
            self.data["articles"][aid] = {"messages": None, "sent": {}}
            self.save()

    def freeze(self, aid, messages):
        item = self.data["articles"][aid]
        if item["messages"] is None:
            item["messages"] = messages
            self.save()

    def deliver(self, aid, chats, sender, sleep=None):
        sleep = sleep or time.sleep
        item = self.data["articles"][aid]
        if item["messages"] is None or not chats:
            return False
        complete = True
        for chat in dict.fromkeys(chats):
            recipient = recipient_key(chat)
            if self.data["blocked_until"].get(recipient, 0) > time.time():
                complete = False
                continue
            for message in item["messages"]:
                sent = item["sent"].setdefault(message["key"], [])
                if recipient in sent:
                    continue
                try:
                    sender(chat, message["text"])
                except DeliveryError as exc:
                    print(f"[오류] 마감 게시글 {aid}: {exc}")
                    if exc.retry_after is not None:
                        self.data["blocked_until"][recipient] = time.time() + exc.retry_after
                        self.save()
                    complete = False
                    break  # Keep chronological message order within this destination.
                sent.append(recipient)
                self.save()
                sleep(3)
        return complete

    def remove_completed(self, seen_ids):
        removed = False
        for aid in list(self.data["articles"]):
            if aid in seen_ids:
                del self.data["articles"][aid]
                removed = True
        if removed:
            self.save()

