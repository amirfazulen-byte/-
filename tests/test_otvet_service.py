import asyncio
import time

import pytest
from cryptography.fernet import Fernet

from otvetbot.db import DB, TokenBox
from otvetbot.drafter import Draft, DraftError
from otvetbot.service import Service, classify_complaints, pilot_stats, weekly_digest
from otvetbot.wb import Card, Item, Product, WBAuthError

TG = 777
TOKEN = "wb-" + "t" * 40


def fb(wb_id, rating, text, name="Платье", cons=""):
    return Item(kind="feedback", wb_id=wb_id, text=text, rating=rating, cons=cons,
                product=Product(nm_id=10, name=name, size="M"))


class FakeWB:
    def __init__(self, feedbacks=(), questions=(), broken=False):
        self.feedbacks = list(feedbacks)
        self.questions = list(questions)
        self.broken = broken
        self.answers = []
        self.takes = []

    async def unanswered_feedbacks(self, take=100):
        if self.broken:
            raise WBAuthError("401")
        self.takes.append(take)
        answered = {a[1] for a in self.answers}
        return [f for f in self.feedbacks if f.wb_id not in answered][:take]

    async def unanswered_questions(self, take=100):
        answered = {a[1] for a in self.answers}
        return [q for q in self.questions if q.wb_id not in answered][:take]

    async def answer(self, kind, wb_id, text):
        self.answers.append((kind, wb_id, text))

    async def card(self, nm_id):
        return Card(nm_id=nm_id, title="Платье миди", sizes=["M / 46"])


class FakeDrafter:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def draft(self, item, card_text, style):
        self.calls.append((item.wb_id, card_text, style))
        if self.fail:
            raise DraftError("Claude API недоступен")
        if "брак" in item.text:
            return Draft("Нам жаль, что так вышло.", True, "брак")
        return Draft(f"Ответ на {item.wb_id}", False, "")


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return len(self.sent)

    async def edit(self, chat_id, message_id, text):
        self.edits.append((message_id, text))


@pytest.fixture
def env(tmp_path):
    db = DB(tmp_path / "o.db")
    wb = FakeWB([fb("a", 5, "Отлично"), fb("b", 2, "Пришёл брак, шов разошёлся", cons="брак"), fb("c", 4, "Хорошо")],
                [Item(kind="question", wb_id="q", text="Есть ли 48?", product=Product(nm_id=10, name="Платье"))])
    drafter, notifier = FakeDrafter(), FakeNotifier()
    svc = Service(db, TokenBox(Fernet.generate_key().decode()), lambda token: wb, drafter, notifier)
    db.add_seller(TG, "Анна")
    yield svc, db, wb, drafter, notifier
    db.close()


def run(coro):
    return asyncio.run(coro)


def seller(db):
    return db.seller_by_tg(TG)


def connect(svc, db):
    ok, msg = run(svc.connect_token(seller(db), TOKEN))
    assert ok, msg


def test_token_is_encrypted_and_validated(env):
    svc, db, wb, *_ = env
    ok, msg = run(svc.connect_token(seller(db), "short"))
    assert not ok and "не похоже" in msg
    connect(svc, db)
    s = seller(db)
    assert s["token_ok"] == 1 and s["awaiting"] is None
    assert TOKEN.encode() not in s["wb_token_enc"]
    assert svc.box.open(s["wb_token_enc"]) == TOKEN


def test_full_flow(env):
    svc, db, wb, drafter, notifier = env
    connect(svc, db)
    run(svc.poll(seller(db)))

    assert wb.takes == [1, 30]  # проверка токена, затем первый опрос с накопившимися отзывами
    drafts = [s for s in notifier.sent if s[2] and s[2][0][0][1].startswith("p:")]
    assert len(drafts) == 4
    assert drafter.calls[0][1].startswith("Название: Платье миди")  # карточка передана в черновик
    flagged = next(s for s in notifier.sent if "Пришёл брак" in s[1])
    assert "Нужно ваше решение" in flagged[1] and "брак" in flagged[1]
    bulk = notifier.sent[-1]
    assert bulk[2][0][0][1] == "pa" and "(3)" in bulk[2][0][0][0]

    # Повторный опрос ничего не дублирует
    before = len(notifier.sent)
    run(svc.poll(seller(db)))
    assert len(notifier.sent) == before and wb.takes[-1] == 100

    # «Опубликовать все без пометок» — только безопасные
    assert run(svc.on_button(TG, "pa")) == "Опубликовано: 3 из 3"
    assert {a[1] for a in wb.answers} == {"a", "c", "q"}
    assert ("question", "q", "Ответ на q") in wb.answers

    # Помеченный: правим текст сами
    b = db.conn.execute("SELECT * FROM items WHERE wb_id = 'b'").fetchone()
    assert run(svc.on_button(TG, f"e:{b['id']}")) == "Жду ваш текст"
    assert run(svc.on_edited_text(seller(db), "Извините! Напишите в чат — разберёмся.")) == "✅ Опубликовано"
    assert wb.answers[-1] == ("feedback", "b", "Извините! Напишите в чат — разберёмся.")
    assert run(svc.on_button(TG, f"p:{b['id']}")) == "Уже обработано"

    stats = pilot_stats(db)
    assert "опубликовано 4 (без правок 3" in stats


def test_foreign_seller_cannot_press_buttons(env):
    svc, db, wb, *_ = env
    connect(svc, db)
    run(svc.poll(seller(db)))
    db.add_seller(999, "Чужой")
    item_id = db.conn.execute("SELECT id FROM items LIMIT 1").fetchone()[0]
    assert run(svc.on_button(999, f"p:{item_id}")) == "Кнопка устарела"
    assert wb.answers == []


def test_skip(env):
    svc, db, wb, _, notifier = env
    connect(svc, db)
    run(svc.poll(seller(db)))
    item_id = db.conn.execute("SELECT id FROM items WHERE wb_id = 'c'").fetchone()[0]
    assert run(svc.on_button(TG, f"s:{item_id}")) == "Пропущено"
    assert db.item(item_id)["status"] == "skipped" and notifier.edits


def test_autopublish_only_safe_five_stars(env):
    svc, db, wb, _, notifier = env
    connect(svc, db)
    db.update_seller(seller(db)["id"], autopublish_5=1)
    run(svc.poll(seller(db)))
    assert [a[1] for a in wb.answers] == ["a"]
    assert db.conn.execute("SELECT published_by FROM items WHERE wb_id = 'a'").fetchone()[0] == "auto"


def test_draft_failure_retries_next_poll(env):
    svc, db, wb, drafter, notifier = env
    connect(svc, db)
    drafter.fail = True
    run(svc.poll(seller(db)))
    assert db.conn.execute("SELECT COUNT(*) FROM items WHERE status = 'draft_failed'").fetchone()[0] == 4
    drafter.fail = False
    run(svc.poll(seller(db)))
    assert db.conn.execute("SELECT COUNT(*) FROM items WHERE status = 'drafted'").fetchone()[0] == 4


def test_broken_token_notifies_once_and_stops(env):
    svc, db, wb, _, notifier = env
    connect(svc, db)
    wb.broken = True
    run(svc.poll(seller(db)))
    assert seller(db)["token_ok"] == 0 and "перестал принимать" in notifier.sent[-1][1]
    assert db.active_sellers() == []


def test_monthly_limit_stops_drafting(env):
    svc, db, wb, drafter, notifier = env
    connect(svc, db)
    db.update_seller(seller(db)["id"], monthly_limit=0)
    run(svc.poll(seller(db)))
    run(svc.poll(seller(db)))
    assert drafter.calls == []
    assert sum("Лимит тарифа" in s[1] for s in notifier.sent) == 1


def test_html_in_review_is_escaped(env):
    svc, db, wb, _, notifier = env
    wb.feedbacks = [fb("x", 5, "<b>жирный</b> & текст")]
    wb.questions = []
    connect(svc, db)
    run(svc.poll(seller(db)))
    assert "&lt;b&gt;жирный&lt;/b&gt; &amp; текст" in notifier.sent[0][1]


def test_weekly_digest(env):
    svc, db, wb, *_ = env
    wb.feedbacks += [fb("d", 1, "Сильно маломерит"), fb("e", 2, "Маломерит, и запах")]
    connect(svc, db)
    run(svc.poll(seller(db)))
    text = weekly_digest(db, seller(db), int(time.time()) + 60)
    assert "Платье — 3 из 5 отзывов ≤3★" in text
    assert "маломерит ×2" in text
    assert "Больше всего вопросов" in text


def test_classify_complaints():
    assert classify_complaints("Очень маломерит и пахнет химией") == ["маломерит", "запах"]
    assert classify_complaints("Всё отлично") == []


def test_answered_in_wb_cabinet_is_closed(env):
    svc, db, wb, _, notifier = env
    connect(svc, db)
    run(svc.poll(seller(db)))
    wb.feedbacks = [f for f in wb.feedbacks if f.wb_id != "a"]  # селлер ответил сам в кабинете
    run(svc.poll(seller(db)))
    a = db.conn.execute("SELECT * FROM items WHERE wb_id = 'a'").fetchone()
    assert a["status"] == "answered_elsewhere"
    assert run(svc.on_button(TG, f"p:{a['id']}")) == "Уже отвечено в кабинете WB"
    assert run(svc.on_button(TG, "pa")) == "Опубликовано: 2 из 2"  # c и q, без повторного ответа на a
    assert "a" not in {x[1] for x in wb.answers}
