"""Черновики ответов через Claude API плюс локальная проверка безопасности."""

import json
import logging
import re
from dataclasses import dataclass

import anthropic

from .wb import Item

log = logging.getLogger(__name__)

# Модели, для которых Claude API умеет сам перезапускать отклонённый запрос на другой модели.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """Ты помогаешь продавцу на Wildberries отвечать на отзывы и вопросы покупателей. Ты пишешь ЧЕРНОВИК, который продавец проверит перед публикацией.

Как писать:
- От лица магазина, вежливо и по-человечески, 1–4 коротких предложения. Без канцелярита и без штампов вроде «Благодарим за ваш отзыв!» в каждом ответе — отвечай на то, что человек написал.
- Если даны примеры ответов продавца, пиши в их стиле и тоне.
- Не обращайся к покупателю по имени (ты его не знаешь).
- На негатив (1–3 звезды) признай конкретную претензию своими словами, без оправданий и спора.
- Не рекламируй другие товары, не сравнивай с конкурентами, не используй слова «лучший», «номер один».

Факты:
- Используй только факты из блока «Карточка товара» и из самого отзыва/вопроса. Не выдумывай размеры, состав, совместимость, сроки, цифры.
- Если для ответа на вопрос не хватает данных карточки — needs_seller = true, reply оставь пустым.

Обязательно needs_seller = true (продавец решит сам), если:
- покупатель пишет о браке, повреждении, некомплекте, подделке, просит возврат, обмен, замену, компенсацию или деньги;
- речь о здоровье, лечебном эффекте, аллергии, беременности, детях и безопасности товара для них, о БАДах и лекарствах;
- отзыв содержит угрозы, оскорбления, юридические претензии или жалобу в контролирующие органы;
- ты не уверен в ответе.
В этих случаях всё равно напиши аккуратный нейтральный черновик без обещаний (извинение и признание проблемы), если это уместно.
Никогда не обещай возврат, замену, компенсацию, скидку или промокод — это решает только продавец.

Верни JSON: reply — текст черновика, needs_seller — нужно ли решение продавца, reason — коротко почему (по-русски, до 10 слов, пусто если всё в порядке)."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "needs_seller": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["reply", "needs_seller", "reason"],
    "additionalProperties": False,
}

# Слова, при которых черновик в любом случае уходит на решение продавца.
RISKY_REPLY = re.compile(
    r"верн[её]м|возврат|обмен|замен[иуы]|компенсац|скидк|промокод|бесплатно|деньги|"
    r"леч[иа]т|вылечи|медицин|гарантир|безопасн[оа] для дет|гипоаллерген|сертифик",
    re.I,
)
RISKY_SOURCE = re.compile(
    r"аллерг|беремен|безопасн|токсичн|проглот|мелкие детал|здоровь|врач|лекарств|"
    r"суд\b|суда\b|роспотреб|прокурат|юрист|подделк|контрафакт|отравил|ожог",
    re.I,
)
# Категории, где любой ответ — зона риска (обещания пользы для здоровья).
RISKY_PRODUCT = re.compile(r"\bбад\b|витамин|биодобав|пищевая добавк|лекарств", re.I)
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class Draft:
    reply: str
    needs_seller: bool
    reason: str


class DraftError(Exception):
    pass


def build_prompt(item: Item, card_text: str, style_examples: str) -> str:
    parts = []
    if style_examples.strip():
        parts.append(f"<примеры_ответов_продавца>\n{style_examples.strip()[:4000]}\n</примеры_ответов_продавца>")
    product = item.product
    card = card_text.strip() or "(данных карточки нет — используй только название товара)"
    parts.append(
        "<карточка_товара>\n"
        f"Товар: {product.name} ({product.brand}), артикул {product.article}\n{card}\n"
        "</карточка_товара>"
    )
    if item.kind == "feedback":
        body = [f"Оценка: {item.rating} из 5"]
        if product.size:
            body.append(f"Купленный размер: {product.size}")
        if item.pros:
            body.append(f"Достоинства: {item.pros}")
        if item.cons:
            body.append(f"Недостатки: {item.cons}")
        body.append(f"Текст отзыва: {item.text or '(без текста)'}")
        parts.append("<отзыв>\n" + "\n".join(body) + "\n</отзыв>\nНапиши ответ на этот отзыв.")
    else:
        parts.append(f"<вопрос>\n{item.text}\n</вопрос>\nНапиши ответ на этот вопрос покупателя.")
    return "\n\n".join(parts)


def safety_check(item: Item, card_text: str, draft: Draft) -> Draft:
    """Последний рубеж: даже если ИИ не пометил ответ, рискованное уходит продавцу."""
    reasons = [draft.reason] if draft.needs_seller and draft.reason else []
    reply = draft.reply.strip()
    source = " ".join([item.text, item.pros, item.cons])

    if not reply:
        reasons.append("нет черновика")
    if m := RISKY_REPLY.search(reply):
        reasons.append(f"в ответе «{m.group(0)}»")
    if m := RISKY_SOURCE.search(source):
        reasons.append(f"чувствительная тема: «{m.group(0)}»")
    if RISKY_PRODUCT.search(item.product.name):
        reasons.append("категория БАД/лекарства")
    known = set(NUMBER.findall(" ".join([card_text, source, item.product.name, item.product.size, item.product.article])))
    unknown = [n for n in NUMBER.findall(reply) if n not in known]
    if unknown:
        reasons.append(f"число {unknown[0]} не из карточки")

    if reasons:
        return Draft(reply=reply, needs_seller=True, reason="; ".join(dict.fromkeys(r for r in reasons if r)))
    return Draft(reply=reply, needs_seller=draft.needs_seller, reason=draft.reason)


class ClaudeDrafter:
    def __init__(self, client: anthropic.AsyncAnthropic, model: str, effort: str):
        self.client = client
        self.model = model
        self.effort = effort

    async def draft(self, item: Item, card_text: str, style_examples: str) -> Draft:
        kwargs = dict(
            model=self.model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(item, card_text, style_examples)}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
        try:
            if self.model in FALLBACK_MODELS:
                response = await self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                response = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise DraftError("Claude API: превышен лимит запросов") from e
        except anthropic.APIStatusError as e:
            raise DraftError(f"Claude API {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise DraftError("Claude API недоступен") from e

        if response.stop_reason == "refusal":
            raw = Draft("", True, "ИИ отказался отвечать")
        elif response.stop_reason == "max_tokens":
            raw = Draft("", True, "ИИ не уложился в лимит")
        else:
            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                data = json.loads(text)
                raw = Draft(str(data["reply"]), bool(data["needs_seller"]), str(data["reason"]))
            except (ValueError, KeyError) as e:
                raise DraftError(f"Не удалось разобрать ответ ИИ: {text[:200]}") from e
        return safety_check(item, card_text, raw)
