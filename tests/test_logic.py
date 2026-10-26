import asyncio
import time

import pytest

from korrekciya import logic
from korrekciya.db import DB

HOUR, DAY = logic.HOUR, logic.DAY
MASTER_TG, CLIENT_TG = 1001, 2002


class FakeNotifier:
    def __init__(self, sms_ok=False, blocked=()):
        self.sent = []   # (chat_id, text, buttons)
        self.smses = []
        self.sms_ok = sms_ok
        self.blocked = set(blocked)

    async def send(self, chat_id, text, buttons=None):
        if chat_id in self.blocked:
            return False
        self.sent.append((chat_id, text, buttons))
        return True

    async def sms(self, phone, text):
        self.smses.append((phone, text))
        return self.sms_ok

    def to(self, chat_id):
        return [s for s in self.sent if s[0] == chat_id]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "t.db")
    d.add_master(MASTER_TG, "Катя", "Europe/Moscow")
    yield d
    d.close()


def master(db):
    return db.get_master_by_tg(MASTER_TG)


def add(db, text):
    return logic.add_visit_from_text(db, master(db), text, int(time.time()), "korr_bot")


def last_visit(db):
    return db.conn.execute("SELECT * FROM visits ORDER BY id DESC LIMIT 1").fetchone()


def press(db, n, who, data, now):
    return run(logic.handle_callback(db, n, who, data, now))


def test_full_happy_path(db):
    reply = add(db, "Аня, послезавтра 14:00, коррекция")
    assert "Записала: Аня" in reply.text
    assert "https://t.me/korr_bot?start=c_" in reply.text

    client = db.find_client(master(db)["id"], "аня")
    linked = logic.link_client(db, client["link_token"], CLIENT_TG, int(time.time()))
    assert "Готово, Аня" in linked.text and "Ваша ближайшая запись" in linked.text

    v = last_visit(db)
    n = FakeNotifier()

    # За 23 часа до визита — напоминание с кнопками
    t = v["starts_at"] - 23 * HOUR
    run(logic.tick(db, n, t, "korr_bot"))
    reminders = n.to(CLIENT_TG)
    assert len(reminders) == 1 and "Подтвердите" in reminders[0][1]
    assert reminders[0][2][0][0][1] == f"cv:{v['id']}:ok"

    # Повторный тик ничего не дублирует
    run(logic.tick(db, n, t + 60, "korr_bot"))
    assert len(n.to(CLIENT_TG)) == 1

    # Клиентка подтверждает -> мастер узнаёт, клиентку спрашивают о согласии
    assert press(db, n, CLIENT_TG, f"cv:{v['id']}:ok", t + 600) == "Спасибо, ждём вас!"
    assert db.get_visit(v["id"])["status"] == "confirmed"
    assert any("подтвердила" in s[1] for s in n.to(MASTER_TG))
    consent_q = n.to(CLIENT_TG)[-1]
    assert consent_q[2][0][0][1] == f"cc:{client['id']}:yes"
    press(db, n, CLIENT_TG, f"cc:{client['id']}:yes", t + 700)
    assert db.get_client(client["id"])["invite_consent"] == 1

    # Подтверждённый визит не вызывает тревогу «не ответила»
    run(logic.tick(db, n, v["starts_at"] - 2 * HOUR, "korr_bot"))
    assert not any("не подтвердила" in s[1] for s in n.to(MASTER_TG))

    # Через 2+ часа после начала спрашиваем мастера
    run(logic.tick(db, n, v["starts_at"] + 3 * HOUR, "korr_bot"))
    outcome = n.to(MASTER_TG)[-1]
    assert "Как прошёл визит" in outcome[1]
    assert press(db, n, MASTER_TG, f"mo:{v['id']}:came", v["starts_at"] + 4 * HOUR).startswith("Отлично")

    # Приглашение через 21 день
    inv = db.conn.execute("SELECT * FROM invites").fetchone()
    assert inv["due_at"] == v["starts_at"] + 21 * DAY
    run(logic.tick(db, n, inv["due_at"] + 60, "korr_bot"))
    invite_msg = n.to(CLIENT_TG)[-1]
    assert "Пора записаться на коррекция" in invite_msg[1]
    assert press(db, n, CLIENT_TG, f"ci:{inv['id']}:book", inv["due_at"] + 120).startswith("Передала мастеру")
    assert "хочет записаться" in n.to(MASTER_TG)[-1][1]

    # Повторное нажатие не дублирует уведомление мастеру
    before = len(n.to(MASTER_TG))
    press(db, n, CLIENT_TG, f"ci:{inv['id']}:book", inv["due_at"] + 180)
    assert len(n.to(MASTER_TG)) == before


def test_no_answer_alerts_master_once(db):
    add(db, "Аня послезавтра 14:00")
    client = db.find_client(master(db)["id"], "Аня")
    logic.link_client(db, client["link_token"], CLIENT_TG, int(time.time()))
    v = last_visit(db)
    n = FakeNotifier()
    t = v["starts_at"] - 23 * HOUR
    run(logic.tick(db, n, t, "korr_bot"))
    run(logic.tick(db, n, t + 3 * HOUR, "korr_bot"))
    assert not any("не подтвердила" in s[1] for s in n.to(MASTER_TG))
    run(logic.tick(db, n, t + 4 * HOUR, "korr_bot"))
    run(logic.tick(db, n, t + 5 * HOUR, "korr_bot"))
    assert sum("не подтвердила" in s[1] for s in n.to(MASTER_TG)) == 1


def test_client_without_telegram_falls_back_to_sms_then_manual(db):
    add(db, "Оля послезавтра 11:00 брови +79991234567")
    v = last_visit(db)

    n = FakeNotifier(sms_ok=True)
    run(logic.tick(db, n, v["starts_at"] - 23 * HOUR, "korr_bot"))
    assert n.smses and n.smses[0][0] == "+79991234567"
    assert db.get_visit(v["id"])["reminder_channel"] == "sms"

    add(db, "Вера послезавтра 12:00")
    v2 = last_visit(db)
    n2 = FakeNotifier(sms_ok=False)
    run(logic.tick(db, n2, v2["starts_at"] - 23 * HOUR, "korr_bot"))
    assert db.get_visit(v2["id"])["reminder_channel"] == "manual"
    assert "напомните ей сами" in n2.to(MASTER_TG)[0][1]


def test_blocked_bot_falls_back(db):
    add(db, "Аня послезавтра 14:00")
    client = db.find_client(master(db)["id"], "Аня")
    logic.link_client(db, client["link_token"], CLIENT_TG, int(time.time()))
    v = last_visit(db)
    n = FakeNotifier(blocked={CLIENT_TG})
    run(logic.tick(db, n, v["starts_at"] - 23 * HOUR, "korr_bot"))
    assert db.get_visit(v["id"])["reminder_channel"] == "manual"


def test_fresh_booking_not_reminded(db):
    client_id = db.add_client(master(db)["id"], "Аня", None)
    db.add_visit(master(db)["id"], client_id, int(time.time()) + 6 * HOUR, "коррекция")
    v = last_visit(db)
    n = FakeNotifier()
    run(logic.tick(db, n, v["starts_at"] - 2 * HOUR, "korr_bot"))
    assert db.get_visit(v["id"])["reminder_channel"] == "skipped"
    assert n.sent == []


def test_client_cancel_and_foreign_buttons(db):
    add(db, "Аня послезавтра 14:00")
    client = db.find_client(master(db)["id"], "Аня")
    logic.link_client(db, client["link_token"], CLIENT_TG, int(time.time()))
    v = last_visit(db)
    n = FakeNotifier()
    t = v["starts_at"] - 23 * HOUR
    # Чужой человек не может нажать кнопку клиентки
    assert press(db, n, 9999, f"cv:{v['id']}:cancel", t) == "Кнопка устарела"
    assert press(db, n, CLIENT_TG, f"cv:{v['id']}:cancel", t).startswith("Запись отменена")
    assert db.get_visit(v["id"])["status"] == "cancelled"
    assert "отменила" in n.to(MASTER_TG)[-1][1]
    assert press(db, n, CLIENT_TG, f"cv:{v['id']}:ok", t) == "Мастер уже знает о вашем ответе 🙂"


def test_names_are_html_escaped(db):
    reply = add(db, "<b>Аня</b>, послезавтра 14:00, <i>брови</i>")
    assert "<b>" not in reply.text and "&lt;b&gt;" in reply.text


def test_new_booking_cancels_pending_invite(db):
    add(db, "Аня послезавтра 14:00")
    client = db.find_client(master(db)["id"], "Аня")
    db.add_invite(master(db)["id"], client["id"], None, int(time.time()) + 10 * DAY)
    add(db, "Аня 01.12 10:00")
    assert db.conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 0


def test_forget(db):
    add(db, "Аня послезавтра 14:00 +79991234567")
    client = db.find_client(master(db)["id"], "Аня")
    logic.link_client(db, client["link_token"], CLIENT_TG, int(time.time()))
    assert "удалены" in logic.forget_client_everywhere(db, CLIENT_TG)
    c = db.get_client(client["id"])
    assert c["tg_id"] is None and c["phone"] is None and c["name"] == "удалено"


def test_weekly_report_and_summary(db):
    add(db, "Аня послезавтра 14:00")
    client = db.find_client(master(db)["id"], "Аня")
    logic.link_client(db, client["link_token"], CLIENT_TG, int(time.time()))
    v = last_visit(db)
    n = FakeNotifier()
    press(db, n, CLIENT_TG, f"cv:{v['id']}:ok", int(time.time()))
    press(db, n, MASTER_TG, f"mo:{v['id']}:noshow", v["starts_at"] + 3 * HOUR)
    report = logic.weekly_report(db, master(db), v["starts_at"] + DAY)
    assert "Не пришли: 1" in report and "подтвердили: 1" in report
    assert "неявок 1 из 1 (100%)" in logic.pilot_summary(db, v["starts_at"] + DAY)


def test_laminating_interval(db):
    assert logic.invite_interval_days(master(db), "Ламинирование ресниц") == 42
    assert logic.invite_interval_days(master(db), "коррекция") == 21


def test_forwarded_client_message(db):
    now = int(time.time())
    reply = logic.add_visit_from_forward(db, master(db), "давайте послезавтра в 14 на коррекцию", "Аня", now, now, "korr_bot")
    assert "Записала: Аня" in reply.text and "коррекция" in reply.text and "Проверьте день" in reply.text
    ev = db.conn.execute("SELECT json_extract(payload, '$.via') FROM events WHERE kind='visit_added'").fetchone()[0]
    assert ev == "forward"
    # Повторная пересылка от той же клиентки находит её, а не создаёт новую
    logic.add_visit_from_forward(db, master(db), "а ещё 01.12 в 10:00", "аня", now, now, "korr_bot")
    assert len(db.clients_of(master(db)["id"])) == 1
    assert "пересылкой 2" in logic.pilot_summary(db, now + 60)


def test_forwarded_relative_day_counts_from_message_date(db):
    now = int(time.time())
    written = now - 3 * DAY
    reply = logic.add_visit_from_forward(db, master(db), "давайте завтра в 14", "Аня", written, now, "korr_bot")
    assert "уже прошло" in reply.text
    assert db.conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 0


def test_forwarded_own_message_and_no_time(db):
    now = int(time.time())
    assert "Это ваше сообщение" in logic.add_visit_from_forward(db, master(db), "жду в пт в 14", None, now, now, "b").text
    assert "не нашла день и время" in logic.add_visit_from_forward(db, master(db), "а когда есть окно?", "Аня", now, now, "b").text
