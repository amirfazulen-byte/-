import asyncio
import json
from types import SimpleNamespace

import pytest

from otvetbot.drafter import ClaudeDrafter, Draft, DraftError, build_prompt, safety_check
from otvetbot.wb import Item, Product

CARD = "Название: Платье миди\nРазмеры: M / 46, L / 48\nРост модели: 170"


def item(text="", rating=5, kind="feedback", name="Платье", cons=""):
    return Item(kind=kind, wb_id="1", text=text, rating=rating if kind == "feedback" else None,
                cons=cons, product=Product(nm_id=1, name=name, size="M", article="PL-1"))


def test_safe_draft_passes():
    d = safety_check(item("Отличное платье!"), CARD, Draft("Рады, что платье понравилось!", False, ""))
    assert not d.needs_seller


@pytest.mark.parametrize("reply", [
    "Вернём деньги, напишите нам",
    "Сделаем замену товара",
    "Дарим промокод на скидку",
    "Этот крем лечит акне",
])
def test_risky_words_in_reply_go_to_seller(reply):
    d = safety_check(item("Не понравилось", rating=2), CARD, Draft(reply, False, ""))
    assert d.needs_seller and "в ответе" in d.reason


@pytest.mark.parametrize("text", [
    "У ребёнка аллергия после этой игрушки",
    "Безопасно ли для беременных?",
    "Пишу жалобу в Роспотребнадзор",
])
def test_sensitive_topics_go_to_seller(text):
    d = safety_check(item(text, rating=1), CARD, Draft("Нам очень жаль", False, ""))
    assert d.needs_seller and "чувствительная тема" in d.reason


def test_children_mention_alone_is_fine():
    d = safety_check(item("Купила ребёнку, всё отлично", name="Детская футболка"), CARD, Draft("Спасибо, рады!", False, ""))
    assert not d.needs_seller


def test_supplements_always_to_seller():
    d = safety_check(item("Хорошие", name="БАД Омега-3"), CARD, Draft("Спасибо за отзыв!", False, ""))
    assert d.needs_seller and "БАД" in d.reason


def test_invented_number_goes_to_seller():
    q = item("Какой рост у модели?", kind="question")
    assert not safety_check(q, CARD, Draft("Рост модели на фото — 170 см.", False, "")).needs_seller
    d = safety_check(q, CARD, Draft("Рост модели на фото — 175 см.", False, ""))
    assert d.needs_seller and "175" in d.reason


def test_empty_draft_goes_to_seller():
    d = safety_check(item("Есть ли размер XXL?", kind="question"), CARD, Draft("", True, "нет данных в карточке"))
    assert d.needs_seller and "нет данных в карточке" in d.reason


def test_prompt_contains_card_and_no_buyer_name():
    p = build_prompt(item("Маломерит", rating=3, cons="размер"), CARD, "Спасибо, что выбрали нас!")
    assert "Рост модели: 170" in p and "Купленный размер: M" in p and "Недостатки: размер" in p
    assert "примеры_ответов_продавца" in p


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def fake_client(payload=None, stop_reason="end_turn"):
    content = [SimpleNamespace(type="thinking", thinking=""),
               SimpleNamespace(type="text", text=json.dumps(payload or {}, ensure_ascii=False))]
    resp = SimpleNamespace(stop_reason=stop_reason, content=content)
    client = SimpleNamespace(messages=FakeMessages(resp), beta=SimpleNamespace(messages=FakeMessages(resp)))
    return client


def test_claude_request_shape_with_fallbacks():
    client = fake_client({"reply": "Спасибо!", "needs_seller": False, "reason": ""})
    d = asyncio.run(ClaudeDrafter(client, "claude-opus-5-5", "low").draft(item("Супер"), CARD, ""))
    assert d == Draft("Спасибо!", False, "")
    call = client.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert not client.messages.calls


def test_model_without_fallbacks_uses_plain_endpoint():
    client = fake_client({"reply": "Спасибо!", "needs_seller": False, "reason": ""})
    asyncio.run(ClaudeDrafter(client, "claude-haiku-5-5", "low").draft(item("Супер"), CARD, ""))
    assert client.messages.calls and not client.beta.messages.calls
    assert "fallbacks" not in client.messages.calls[0]


def test_refusal_goes_to_seller():
    client = fake_client(stop_reason="refusal")
    d = asyncio.run(ClaudeDrafter(client, "claude-opus-5-5", "low").draft(item("..."), CARD, ""))
    assert d.needs_seller and "отказался" in d.reason


def test_unparseable_output_raises():
    client = fake_client()
    client.beta.messages.response.content[1].text = "не json"
    with pytest.raises(DraftError):
        asyncio.run(ClaudeDrafter(client, "claude-opus-5-5", "low").draft(item("..."), CARD, ""))
