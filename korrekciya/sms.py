"""Отправка SMS через SMS.ru. Без SMS_RU_API_ID SMS отключены: бот попросит мастера напомнить вручную."""

import logging

import aiohttp

log = logging.getLogger(__name__)


class SmsRu:
    URL = "https://sms.ru/sms/send"

    def __init__(self, api_id: str, sender: str | None = None):
        self.api_id = api_id
        self.sender = sender

    async def send(self, phone: str, text: str) -> bool:
        params = {"api_id": self.api_id, "to": phone.lstrip("+"), "msg": text, "json": 1}
        if self.sender:
            params["from"] = self.sender
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.URL, data=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("SMS.ru недоступен: %s", e)
            return False
        sms = (data.get("sms") or {}).get(phone.lstrip("+"), {})
        if data.get("status") == "OK" and sms.get("status") == "OK":
            return True
        log.warning("SMS не отправлено: %s", data)
        return False
