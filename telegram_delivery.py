"""Small Telegram transport with bounded retries and credential-safe failures."""

import time
import requests


class DeliveryError(RuntimeError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def send_message(token, chat_id, text, *, post=requests.post, sleep=time.sleep, attempts=3):
    if not token or not chat_id:
        raise DeliveryError("텔레그램 설정이 비어 있습니다")
    for attempt in range(attempts):
        try:
            response = post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True}, timeout=15,
            )
            data = response.json()
        except (requests.RequestException, ValueError):
            # requests exceptions can contain the bot token in the URL. Never log them.
            if attempt + 1 == attempts:
                raise DeliveryError("텔레그램 응답을 확인하지 못했습니다") from None
            sleep(2 ** attempt)
            continue
        if not isinstance(data, dict):
            raise DeliveryError("텔레그램 응답 형식이 올바르지 않습니다")
        if response.status_code == 200 and data.get("ok") is True:
            message_id = data.get("result", {}).get("message_id")
            if isinstance(message_id, int):
                return message_id
            raise DeliveryError("텔레그램 성공 응답에 메시지 번호가 없습니다")
        code = data.get("error_code", response.status_code)
        if not isinstance(code, int):
            raise DeliveryError("텔레그램 응답 코드가 올바르지 않습니다")
        if code == 429:
            delay = data.get("parameters", {}).get("retry_after", 60)
            if not isinstance(delay, (int, float)) or delay < 0:
                delay = 60
            if attempt + 1 < attempts and delay <= 300:
                sleep(delay)
                continue
            raise DeliveryError("텔레그램 요청 제한으로 다음 실행에서 재시도합니다", retry_after=delay)
        if code >= 500 and attempt + 1 < attempts:
            sleep(2 ** attempt)
            continue
        raise DeliveryError(f"텔레그램 전송 실패 (응답 코드 {code})")
    raise DeliveryError("텔레그램 전송 시도 횟수 초과")

