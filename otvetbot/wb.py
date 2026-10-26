"""Клиент API Wildberries: отзывы, вопросы и (по возможности) карточки товаров.

Методы взяты из документации WB «Вопросы и отзывы» (feedbacks-api.wildberries.ru).
Лимит категории — около 3 запросов в секунду на аккаунт, поэтому запросы одного
токена идут последовательно с паузой.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger(__name__)

FEEDBACKS_URL = "https://feedbacks-api.wildberries.ru"
CONTENT_URL = "https://content-api.wildberries.ru"
MIN_INTERVAL = 0.4  # секунд между запросами одного токена (< 3 rps)


class WBError(Exception):
    pass


class WBAuthError(WBError):
    """Токен недействителен, истёк или не имеет нужной категории доступа."""


@dataclass
class Product:
    nm_id: int
    name: str = ""
    brand: str = ""
    article: str = ""
    size: str = ""


@dataclass
class Item:
    """Отзыв или вопрос покупателя."""
    kind: str               # feedback | question
    wb_id: str
    text: str
    product: Product
    rating: int | None = None   # только у отзывов
    pros: str = ""
    cons: str = ""
    created_at: str = ""
    has_photo: bool = False


@dataclass
class Card:
    """Данные карточки товара, на которых ИИ строит ответ."""
    nm_id: int
    title: str = ""
    description: str = ""
    characteristics: dict[str, str] = field(default_factory=dict)
    sizes: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        lines = [f"Название: {self.title}"] if self.title else []
        if self.sizes:
            lines.append("Размеры: " + ", ".join(self.sizes))
        for k, v in self.characteristics.items():
            lines.append(f"{k}: {v}")
        if self.description:
            lines.append(f"Описание: {self.description[:2000]}")
        return "\n".join(lines)


class WBClient:
    def __init__(self, token: str, session: aiohttp.ClientSession):
        self._token = token
        self._session = session
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _request(self, method: str, url: str, **kwargs):
        async with self._lock:
            wait = self._last + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            for attempt in range(3):
                self._last = time.monotonic()
                async with self._session.request(
                    method, url, headers={"Authorization": self._token},
                    timeout=aiohttp.ClientTimeout(total=30), **kwargs,
                ) as resp:
                    if resp.status in (401, 403):
                        raise WBAuthError(f"WB отклонил токен ({resp.status})")
                    if resp.status == 429:
                        retry = float(resp.headers.get("X-Ratelimit-Retry", 2 ** attempt))
                        log.info("WB 429, жду %.1f с", retry)
                        await asyncio.sleep(retry)
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        raise WBError(f"WB {method} {url} → {resp.status}: {body[:300]}")
                    raw = await resp.text()
                    if resp.status == 204 or not raw.strip():
                        return None
                    data = json.loads(raw)
                    if isinstance(data, dict) and data.get("error"):
                        raise WBError(f"WB ошибка: {data.get('errorText')}")
                    return data
            raise WBError("WB: превышен лимит запросов")

    # --- отзывы и вопросы ---

    async def unanswered_feedbacks(self, take: int = 100) -> list[Item]:
        data = await self._request(
            "GET", f"{FEEDBACKS_URL}/api/v1/feedbacks",
            params={"isAnswered": "false", "take": take, "skip": 0, "order": "dateDesc"},
        )
        return [_feedback(f) for f in ((data or {}).get("data") or {}).get("feedbacks") or []]

    async def unanswered_questions(self, take: int = 100) -> list[Item]:
        data = await self._request(
            "GET", f"{FEEDBACKS_URL}/api/v1/questions",
            params={"isAnswered": "false", "take": take, "skip": 0, "order": "dateDesc"},
        )
        return [_question(q) for q in ((data or {}).get("data") or {}).get("questions") or []]

    async def answer(self, item_kind: str, wb_id: str, text: str) -> None:
        if item_kind == "feedback":
            await self._request("POST", f"{FEEDBACKS_URL}/api/v1/feedbacks/answer", json={"id": wb_id, "text": text})
        else:
            await self._request(
                "PATCH", f"{FEEDBACKS_URL}/api/v1/questions",
                json={"id": wb_id, "answer": {"text": text}, "state": "wbRu"},
            )

    # --- карточки (нужна категория токена «Контент»; без неё работаем по данным из отзыва) ---

    async def card(self, nm_id: int) -> Card | None:
        try:
            data = await self._request(
                "POST", f"{CONTENT_URL}/content/v2/get/cards/list",
                json={"settings": {"cursor": {"limit": 10}, "filter": {"textSearch": str(nm_id), "withPhoto": -1}}},
            )
        except WBAuthError:
            return None
        for c in (data or {}).get("cards") or []:
            if c.get("nmID") == nm_id:
                return _card(c)
        return None


def _product(d: dict) -> Product:
    return Product(
        nm_id=int(d.get("nmId") or 0),
        name=d.get("productName") or "",
        brand=d.get("brandName") or "",
        article=d.get("supplierArticle") or "",
        size=d.get("size") or "",
    )


def _feedback(f: dict) -> Item:
    return Item(
        kind="feedback",
        wb_id=str(f["id"]),
        text=(f.get("text") or "").strip(),
        pros=(f.get("pros") or "").strip(),
        cons=(f.get("cons") or "").strip(),
        rating=f.get("productValuation"),
        product=_product(f.get("productDetails") or {}),
        created_at=f.get("createdDate") or "",
        has_photo=bool(f.get("photoLinks")),
    )


def _question(q: dict) -> Item:
    return Item(
        kind="question",
        wb_id=str(q["id"]),
        text=(q.get("text") or "").strip(),
        product=_product(q.get("productDetails") or {}),
        created_at=q.get("createdDate") or "",
    )


def _card(c: dict) -> Card:
    chars = {}
    for ch in c.get("characteristics") or []:
        value = ch.get("value")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if ch.get("name") and value not in (None, ""):
            chars[ch["name"]] = str(value)
    sizes = []
    for s in c.get("sizes") or []:
        label = " / ".join(x for x in (s.get("techSize"), s.get("wbSize")) if x)
        if label:
            sizes.append(label)
    return Card(
        nm_id=c.get("nmID") or 0,
        title=c.get("title") or "",
        description=c.get("description") or "",
        characteristics=chars,
        sizes=sizes,
    )
