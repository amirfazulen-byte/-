import asyncio

import aiohttp
import pytest
from aiohttp import web

from otvetbot import wb as wbmod
from otvetbot.wb import WBAuthError, WBClient

TOKEN = "test-token-" + "x" * 40

FEEDBACK = {
    "id": "fb1", "text": "Маломерит, взяла M", "pros": "ткань", "cons": "размер",
    "productValuation": 3, "createdDate": "2026-10-20T10:00:00Z", "photoLinks": [],
    "productDetails": {"nmId": 123, "productName": "Платье", "brandName": "Бренд",
                       "supplierArticle": "PL-1", "size": "M"},
}
QUESTION = {"id": "q1", "text": "Какой рост у модели?", "createdDate": "2026-10-20T11:00:00Z",
            "productDetails": {"nmId": 123, "productName": "Платье"}}
CARD = {"nmID": 123, "title": "Платье миди", "description": "Хлопок",
        "characteristics": [{"name": "Состав", "value": ["хлопок 95%", "эластан 5%"]}, {"name": "Рост модели", "value": "170"}],
        "sizes": [{"techSize": "M", "wbSize": "46"}]}


def run(coro):
    return asyncio.run(coro)


async def with_server(handlers, fn, monkeypatch):
    app = web.Application()
    calls = []

    def wrap(h):
        async def inner(request):
            body = await request.json() if request.can_read_body else None
            calls.append((request.method, request.path, dict(request.query), body, request.headers.get("Authorization")))
            return await h(request)
        return inner

    for method, path, h in handlers:
        app.router.add_route(method, path, wrap(h))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(wbmod, "FEEDBACKS_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(wbmod, "CONTENT_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(wbmod, "MIN_INTERVAL", 0)
    try:
        async with aiohttp.ClientSession() as session:
            return await fn(WBClient(TOKEN, session)), calls
    finally:
        await runner.cleanup()


def test_list_and_answer(monkeypatch):
    async def feedbacks(_):
        return web.json_response({"data": {"feedbacks": [FEEDBACK]}, "error": False, "errorText": ""})

    async def questions(_):
        return web.json_response({"data": {"questions": [QUESTION]}, "error": False, "errorText": ""})

    async def answer(_):
        return web.Response(status=204)

    async def cards(_):
        return web.json_response({"cards": [CARD]})

    async def scenario(client):
        fbs = await client.unanswered_feedbacks(5)
        qs = await client.unanswered_questions(5)
        await client.answer("feedback", "fb1", "Спасибо!")
        await client.answer("question", "q1", "Рост модели 170 см")
        card = await client.card(123)
        return fbs, qs, card

    (fbs, qs, card), calls = run(with_server([
        ("GET", "/api/v1/feedbacks", feedbacks),
        ("GET", "/api/v1/questions", questions),
        ("POST", "/api/v1/feedbacks/answer", answer),
        ("PATCH", "/api/v1/questions", answer),
        ("POST", "/content/v2/get/cards/list", cards),
    ], scenario, monkeypatch))

    assert fbs[0].wb_id == "fb1" and fbs[0].rating == 3 and fbs[0].product.size == "M"
    assert fbs[0].product.name == "Платье" and fbs[0].cons == "размер"
    assert qs[0].kind == "question" and qs[0].product.nm_id == 123
    assert card.characteristics["Состав"] == "хлопок 95%, эластан 5%"
    assert "Рост модели: 170" in card.as_text() and "M / 46" in card.as_text()

    assert all(c[4] == TOKEN for c in calls)  # токен в заголовке Authorization как есть
    assert calls[0][2]["isAnswered"] == "false"
    assert calls[2][:2] == ("POST", "/api/v1/feedbacks/answer") and calls[2][3] == {"id": "fb1", "text": "Спасибо!"}
    assert calls[3][:2] == ("PATCH", "/api/v1/questions")
    assert calls[3][3] == {"id": "q1", "answer": {"text": "Рост модели 170 см"}, "state": "wbRu"}


def test_rate_limit_retry(monkeypatch):
    hits = {"n": 0}

    async def feedbacks(_):
        hits["n"] += 1
        if hits["n"] == 1:
            return web.Response(status=429, headers={"X-Ratelimit-Retry": "0"})
        return web.json_response({"data": {"feedbacks": []}, "error": False})

    result, _ = run(with_server([("GET", "/api/v1/feedbacks", feedbacks)],
                                lambda c: c.unanswered_feedbacks(), monkeypatch))
    assert result == [] and hits["n"] == 2


def test_auth_error(monkeypatch):
    async def denied(_):
        return web.Response(status=401)

    with pytest.raises(WBAuthError):
        run(with_server([("GET", "/api/v1/feedbacks", denied)], lambda c: c.unanswered_feedbacks(), monkeypatch))


def test_card_without_content_access_is_none(monkeypatch):
    async def denied(_):
        return web.Response(status=403)

    card, _ = run(with_server([("POST", "/content/v2/get/cards/list", denied)], lambda c: c.card(123), monkeypatch))
    assert card is None
