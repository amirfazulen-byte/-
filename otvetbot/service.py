"""Логика «ОтветБота», не зависящая от Telegram: опрос WB, черновики, публикация, сводки."""

import difflib
import html
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable, Protocol

from .db import DB, TokenBox, now_ts
from .drafter import Draft, DraftError
from .wb import Item, Product, WBAuthError, WBClient, WBError

log = logging.getLogger(__name__)

DAY = 24 * 3600
CARD_TTL = DAY
FIRST_POLL_TAKE = 30     # при подключении показываем черновики на уже накопившихся отзывах
POLL_TAKE = 100
DIGEST_EVERY = 7 * DAY

Buttons = list[list[tuple[str, str]]]

COMPLAINTS = {
    "маломерит": r"маломер|мал[аоы]? в|меньше размер|не налез",
    "большемерит": r"большемер|велик[аоы]?\b|больше размер",
    "брак/дефект": r"брак|дефект|сломал|порвал|разошел|разошёл|дырк|трещин|не работает",
    "запах": r"запах|воня|пахнет",
    "цвет не тот": r"цвет не|другой цвет|не тот цвет|отличает.* от фото|не как на фото",
    "некомплект": r"некомплект|не хватает|не положили|отсутству",
    "упаковка/доставка": r"упаковк|помят|доставк|пришл[оа] разбит",
    "качество": r"качеств|тонк|дешев|дёшев|катышк|линя",
}


class Notifier(Protocol):
    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> int | None: ...
    async def edit(self, chat_id: int, message_id: int, text: str) -> None: ...


class DrafterLike(Protocol):
    async def draft(self, item: Item, card_text: str, style_examples: str) -> Draft: ...


WBFactory = Callable[[str], WBClient]


def h(value) -> str:
    return html.escape(str(value or ""), quote=False)


def stars(rating) -> str:
    r = int(rating or 0)
    return "⭐" * r + "☆" * (5 - r)


def row_to_item(row) -> Item:
    return Item(
        kind=row["kind"], wb_id=row["wb_id"], text=row["text"] or "", rating=row["rating"],
        pros=row["pros"] or "", cons=row["cons"] or "", created_at=row["created_wb"] or "",
        product=Product(nm_id=row["nm_id"] or 0, name=row["product_name"] or "", size=row["size"] or ""),
    )


@dataclass
class Service:
    db: DB
    box: TokenBox
    wb_factory: WBFactory
    drafter: DrafterLike
    notifier: Notifier

    def wb_for(self, seller) -> WBClient | None:
        token = self.box.open(seller["wb_token_enc"]) if seller["wb_token_enc"] else None
        return self.wb_factory(token) if token else None

    # --- подключение ---

    async def connect_token(self, seller, token: str) -> tuple[bool, str]:
        token = token.strip()
        if len(token) < 30 or " " in token:
            return False, "Это не похоже на токен WB. Скопируйте его целиком из личного кабинета и пришлите одним сообщением."
        wb = self.wb_factory(token)
        try:
            await wb.unanswered_feedbacks(take=1)
        except WBAuthError:
            return False, ("WB не принял токен. Проверьте, что при создании выбрана категория «Вопросы и отзывы» "
                    "и токен не «только на чтение» — иначе я не смогу публиковать ответы.")
        except WBError as e:
            log.warning("Проверка токена: %s", e)
            return False, "Не получилось связаться с WB. Попробуйте прислать токен ещё раз через пару минут."
        self.db.update_seller(seller["id"], wb_token_enc=self.box.seal(token), token_ok=1, awaiting=None)
        self.db.log("token_connected", seller_id=seller["id"])
        return True, ("✅ Токен подключён. Сейчас подготовлю черновики для неотвеченных отзывов и вопросов — "
                "пришлю их сюда в течение пары минут.\n\n"
                "Совет: пришлите /style и 10–20 своих прошлых ответов — черновики будут звучать как вы.")

    # --- основной цикл ---

    async def poll(self, seller, max_new_drafts: int = 50) -> None:
        wb = self.wb_for(seller)
        if wb is None:
            return
        first = seller["last_poll_at"] is None
        take = FIRST_POLL_TAKE if first else POLL_TAKE
        try:
            feedbacks = await wb.unanswered_feedbacks(take)
            questions = await wb.unanswered_questions(take)
        except WBAuthError:
            await self._token_broken(seller)
            return
        except WBError as e:
            log.warning("Опрос WB для продавца %s: %s", seller["id"], e)
            self.db.log("poll_failed", seller_id=seller["id"], error=str(e))
            return

        items = feedbacks + questions
        added = sum(1 for it in items if self.db.add_item(seller["id"], it))
        self.db.update_seller(seller["id"], last_poll_at=now_ts())
        # Если список пришёл целиком, то пропавшие из него уже отвечены в кабинете WB.
        for kind, got in (("feedback", feedbacks), ("question", questions)):
            if len(got) < take:
                self._close_answered_elsewhere(seller, kind, {it.wb_id for it in got})
        if added:
            self.db.log("items_added", seller_id=seller["id"], count=added)

        pending = self.db.items_with_status(seller["id"], "new", "draft_failed")[:max_new_drafts]
        if not pending:
            return
        used = self.db.answers_this_month(seller["id"], now_ts())
        if used >= seller["monthly_limit"]:
            if not self._limit_notified_this_month(seller):
                self.db.log("limit_reached", seller_id=seller["id"])
                await self.notifier.send(
                    seller["tg_id"],
                    f"Лимит тарифа — {seller['monthly_limit']} ответов в месяц — исчерпан. "
                    "Новые черновики появятся с начала месяца или после смены тарифа.",
                )
            return

        safe_sent = 0
        for row in pending:
            draft = await self._make_draft(seller, wb, row)
            if draft is None:
                continue
            row = self.db.item(row["id"])
            auto = (seller["autopublish_5"] and row["kind"] == "feedback" and row["rating"] == 5
                    and not draft.needs_seller and draft.reply)
            if auto and await self._publish(seller, wb, row, draft.reply, by="auto"):
                continue
            await self._send_draft(seller, row)
            if not draft.needs_seller and draft.reply:
                safe_sent += 1
        if safe_sent > 1:
            await self.notifier.send(
                seller["tg_id"],
                f"Черновиков без пометок: {safe_sent}. Проверили — можно опубликовать все сразу.",
                [[(f"✅ Опубликовать все без пометок ({safe_sent})", "pa")]],
            )

    def _close_answered_elsewhere(self, seller, kind: str, still_open: set[str]) -> None:
        for row in self.db.items_with_status(seller["id"], "new", "drafted", "draft_failed", "publish_failed"):
            if row["kind"] == kind and row["wb_id"] not in still_open:
                self.db.update_item(row["id"], status="answered_elsewhere")
                self.db.log("answered_elsewhere", seller_id=seller["id"], item_id=row["id"])

    async def _make_draft(self, seller, wb: WBClient, row) -> Draft | None:
        item = row_to_item(row)
        card_text = await self._card_text(seller, wb, item.product.nm_id)
        try:
            draft = await self.drafter.draft(item, card_text, seller["style_examples"])
        except DraftError as e:
            log.warning("Черновик для %s: %s", row["id"], e)
            self.db.update_item(row["id"], status="draft_failed", error=str(e))
            self.db.log("draft_failed", seller_id=seller["id"], item_id=row["id"], error=str(e))
            return None
        self.db.update_item(row["id"], status="drafted", draft=draft.reply,
                            needs_seller=int(draft.needs_seller), reason=draft.reason, error=None)
        self.db.log("drafted", seller_id=seller["id"], item_id=row["id"], needs_seller=draft.needs_seller)
        return draft

    async def _card_text(self, seller, wb: WBClient, nm_id: int) -> str:
        if not nm_id:
            return ""
        cached = self.db.cached_card(seller["id"], nm_id, CARD_TTL)
        if cached is not None:
            return cached
        try:
            card = await wb.card(nm_id)
        except WBError as e:
            log.info("Карточка %s недоступна: %s", nm_id, e)
            card = None
        text = card.as_text() if card else ""
        self.db.save_card(seller["id"], nm_id, text)
        return text

    def _limit_notified_this_month(self, seller) -> bool:
        t = time.gmtime()
        month = f"{t.tm_year}-{t.tm_mon:02d}"
        row = self.db.conn.execute(
            "SELECT 1 FROM events WHERE seller_id = ? AND kind = 'limit_reached' "
            "AND strftime('%Y-%m', ts, 'unixepoch') = ?", (seller["id"], month),
        ).fetchone()
        return row is not None

    async def _token_broken(self, seller) -> None:
        self.db.update_seller(seller["id"], token_ok=0, awaiting="token")
        self.db.log("token_broken", seller_id=seller["id"])
        await self.notifier.send(
            seller["tg_id"],
            "⚠️ WB перестал принимать ваш токен (истёк или удалён). Создайте новый в личном кабинете "
            "(категория «Вопросы и отзывы») и пришлите его сюда — я продолжу работу.",
        )

    # --- сообщения в Telegram ---

    def draft_text(self, row) -> str:
        head = f"{stars(row['rating'])} Отзыв" if row["kind"] == "feedback" else "❓ Вопрос"
        lines = [f"<b>{head}</b> · {h(row['product_name'])}"]
        if row["size"]:
            lines[0] += f" · размер {h(row['size'])}"
        if row["pros"]:
            lines.append(f"👍 {h(row['pros'])}")
        if row["cons"]:
            lines.append(f"👎 {h(row['cons'])}")
        lines.append(f"«{h(row['text'])}»" if row["text"] else "<i>(без текста)</i>")
        lines.append("")
        if row["draft"]:
            lines.append(f"<b>Черновик ответа:</b>\n{h(row['draft'])}")
        else:
            lines.append("<b>Черновика нет</b> — нужен ваш ответ.")
        if row["needs_seller"]:
            lines.append(f"\n⚠️ <b>Нужно ваше решение:</b> {h(row['reason'] or 'проверьте ответ')}")
        return "\n".join(lines)

    async def _send_draft(self, seller, row) -> None:
        if row["draft"]:
            buttons = [[("✅ Опубликовать", f"p:{row['id']}"), ("✏️ Изменить", f"e:{row['id']}")],
                       [("⏭ Пропустить", f"s:{row['id']}")]]
        else:
            buttons = [[("✏️ Написать ответ", f"e:{row['id']}"), ("⏭ Пропустить", f"s:{row['id']}")]]
        msg_id = await self.notifier.send(seller["tg_id"], self.draft_text(row), buttons)
        if msg_id:
            self.db.update_item(row["id"], tg_message_id=msg_id)

    # --- публикация ---

    async def _publish(self, seller, wb: WBClient, row, text: str, by: str) -> bool:
        text = text.strip()
        if not 2 <= len(text) <= 5000:
            await self.notifier.send(seller["tg_id"], "Ответ должен быть от 2 до 5000 символов.")
            return False
        try:
            await wb.answer(row["kind"], row["wb_id"], text)
        except WBAuthError:
            await self._token_broken(seller)
            return False
        except WBError as e:
            self.db.update_item(row["id"], status="publish_failed", error=str(e))
            self.db.log("publish_failed", seller_id=seller["id"], item_id=row["id"], error=str(e))
            await self.notifier.send(seller["tg_id"], f"Не получилось опубликовать ответ: {h(e)}")
            return False
        self.db.update_item(row["id"], status="published", published_text=text, published_at=now_ts(),
                            published_by=by, error=None)
        self.db.log("published", seller_id=seller["id"], item_id=row["id"], by=by,
                    edited=text != (row["draft"] or ""))
        if row["tg_message_id"]:
            label = "🤖 Опубликовано автоматически" if by == "auto" else "✅ Опубликовано"
            await self.notifier.edit(seller["tg_id"], row["tg_message_id"],
                                     self.draft_text(self.db.item(row["id"])) + f"\n\n<b>{label}:</b>\n{h(text)}")
        return True

    async def on_button(self, tg_id: int, data: str) -> str:
        seller = self.db.seller_by_tg(tg_id)
        if seller is None:
            return "Кнопка устарела"
        if data == "pa":
            return await self._publish_all_safe(seller)
        try:
            action, item_id = data.split(":")
            row = self.db.item(int(item_id))
        except ValueError:
            return "Кнопка устарела"
        if row is None or row["seller_id"] != seller["id"]:
            return "Кнопка устарела"
        if row["status"] in ("published", "skipped"):
            return "Уже обработано"
        if row["status"] == "answered_elsewhere":
            return "Уже отвечено в кабинете WB"

        if action == "p":
            if not row["draft"]:
                return "Черновика нет — нажмите «Написать ответ»"
            wb = self.wb_for(seller)
            ok = wb is not None and await self._publish(seller, wb, row, row["draft"], by="seller")
            return "Опубликовано ✅" if ok else "Не опубликовано"
        if action == "e":
            self.db.update_seller(seller["id"], awaiting=f"edit:{row['id']}")
            await self.notifier.send(
                tg_id, "Пришлите текст ответа одним сообщением — я опубликую его."
                + (f"\n\nЧерновик для копирования:\n<code>{h(row['draft'])}</code>" if row["draft"] else ""),
            )
            return "Жду ваш текст"
        if action == "s":
            self.db.update_item(row["id"], status="skipped")
            self.db.log("skipped", seller_id=seller["id"], item_id=row["id"])
            if row["tg_message_id"]:
                await self.notifier.edit(tg_id, row["tg_message_id"], self.draft_text(row) + "\n\n⏭ <i>Пропущено</i>")
            return "Пропущено"
        return "Кнопка устарела"

    async def _publish_all_safe(self, seller) -> str:
        rows = [r for r in self.db.items_with_status(seller["id"], "drafted") if not r["needs_seller"] and r["draft"]]
        if not rows:
            return "Нет черновиков без пометок"
        wb = self.wb_for(seller)
        if wb is None:
            return "Токен не подключён"
        done = 0
        for r in rows:
            if await self._publish(seller, wb, r, r["draft"], by="seller"):
                done += 1
        return f"Опубликовано: {done} из {len(rows)}"

    async def on_edited_text(self, seller, text: str) -> str:
        item_id = int(seller["awaiting"].split(":")[1])
        row = self.db.item(item_id)
        self.db.update_seller(seller["id"], awaiting=None)
        if row is None or row["seller_id"] != seller["id"] or row["status"] in ("published", "skipped", "answered_elsewhere"):
            return "Этот отзыв уже обработан."
        wb = self.wb_for(seller)
        if wb and await self._publish(seller, wb, row, text, by="seller"):
            return "✅ Опубликовано"
        return "Не опубликовано — попробуйте ещё раз кнопкой «Изменить»."

    # --- сводка жалоб ---

    async def send_digests(self, now: int) -> None:
        for seller in self.db.active_sellers():
            last = seller["last_digest_at"] or seller["created_at"]
            if now - last < DIGEST_EVERY:
                continue
            text = weekly_digest(self.db, seller, now)
            if text:
                await self.notifier.send(seller["tg_id"], text)
            self.db.update_seller(seller["id"], last_digest_at=now)


def classify_complaints(text: str) -> list[str]:
    low = (text or "").lower()
    return [name for name, pattern in COMPLAINTS.items() if re.search(pattern, low)]


def weekly_digest(db: DB, seller, now: int) -> str | None:
    rows = db.items_since(seller["id"], now - DIGEST_EVERY)
    feedbacks = [r for r in rows if r["kind"] == "feedback"]
    if not rows:
        return None
    by_product: dict[str, dict] = defaultdict(lambda: {"n": 0, "sum": 0, "neg": 0, "complaints": Counter()})
    for r in feedbacks:
        p = by_product[r["product_name"] or f"nm {r['nm_id']}"]
        p["n"] += 1
        p["sum"] += r["rating"] or 0
        if (r["rating"] or 5) <= 3:
            p["neg"] += 1
            p["complaints"].update(classify_complaints(" ".join(filter(None, [r["text"], r["cons"]]))))
    questions = [r for r in rows if r["kind"] == "question"]
    published = [r for r in rows if r["status"] == "published"]

    lines = ["📊 <b>Сводка за неделю</b>", "",
             f"Отзывов: {len(feedbacks)}, вопросов: {len(questions)}, опубликовано ответов: {len(published)}"]
    problem = sorted(((name, p) for name, p in by_product.items() if p["neg"]), key=lambda x: -x[1]["neg"])
    if problem:
        lines += ["", "<b>Товары с негативом:</b>"]
        for name, p in problem[:7]:
            avg = p["sum"] / p["n"]
            top = ", ".join(f"{c} ×{k}" for c, k in p["complaints"].most_common(3)) or "без явной причины"
            lines.append(f"• {h(name)} — {p['neg']} из {p['n']} отзывов ≤3★ (средняя {avg:.1f}). Чаще всего: {top}")
        lines.append("\nЕсли жалоба повторяется — стоит поправить карточку (размерную сетку, фото, описание).")
    if questions:
        by_q = Counter(r["product_name"] for r in questions)
        lines += ["", "<b>Больше всего вопросов:</b>"]
        lines += [f"• {h(name)} — {k}" for name, k in by_q.most_common(3)]
        lines.append("Частые вопросы — подсказка, чего не хватает в описании.")
    return "\n".join(lines)


def pilot_stats(db: DB) -> str:
    """Сводка для оператора: метрики решения «продолжать или нет»."""
    lines = ["<b>Пилот «ОтветБот»</b>"]
    for s in db.all_sellers():
        rows = db.conn.execute("SELECT * FROM items WHERE seller_id = ? AND draft IS NOT NULL", (s["id"],)).fetchall()
        published = [r for r in rows if r["status"] == "published"]
        unchanged = sum(1 for r in published if (r["published_text"] or "") == (r["draft"] or ""))
        minor = sum(1 for r in published if r["published_text"] != r["draft"] and r["draft"]
                    and difflib.SequenceMatcher(None, r["draft"], r["published_text"]).ratio() >= 0.85)
        flagged = sum(1 for r in rows if r["needs_seller"])
        skipped = sum(1 for r in rows if r["status"] == "skipped")
        ok_share = f"{100 * (unchanged + minor) / len(published):.0f}%" if published else "—"
        lines.append(
            f"\n• {h(s['name'])} (tg {s['tg_id']}{', ⛔ токен' if not s['token_ok'] else ''}): черновиков {len(rows)}, "
            f"опубликовано {len(published)} (без правок {unchanged}, мелкие правки {minor} → {ok_share}), "
            f"с пометкой {flagged}, пропущено {skipped}"
        )
    if len(lines) == 1:
        lines.append("пока нет продавцов")
    return "\n".join(lines)
