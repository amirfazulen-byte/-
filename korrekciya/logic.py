"""Бизнес-логика бота, не зависящая от Telegram: её можно тестировать с фейковым отправщиком."""

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import DB
from .parser import ParseError, parse_visit

HOUR = 3600
DAY = 24 * HOUR

REMINDER_BEFORE = 24 * HOUR      # за сколько до визита просим подтвердить
FRESH_BOOKING = 12 * HOUR        # визит, добавленный позже этого срока до начала, не напоминаем
NO_ANSWER_AFTER = 4 * HOUR       # через сколько после напоминания тревожим мастера
NO_ANSWER_LAST_CALL = 3 * HOUR   # ...или не позже чем за столько до визита
OUTCOME_AFTER = 2 * HOUR         # через сколько после начала спрашиваем «пришла?»
LATER_DELAY = 7 * DAY            # «напомнить позже»
REPORT_WEEKDAY, REPORT_HOUR = 0, 10  # отчёт по понедельникам в 10:00 по времени мастера

# Услуги с более длинным циклом: (подстрока в названии, дней до приглашения)
SERVICE_INTERVALS = [("ламин", 42), ("окрашив", 28)]

WEEKDAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

Buttons = list[list[tuple[str, str]]]  # ряды кнопок (текст, callback_data)


def h(value) -> str:
    """Экранирование для parse_mode=HTML: имена и услуги вводит мастер."""
    return html.escape(str(value), quote=False)


class Notifier(Protocol):
    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> bool: ...
    async def sms(self, phone: str, text: str) -> bool: ...


@dataclass
class Reply:
    text: str
    buttons: Buttons | None = None


# --- время ---

def tz_of(master) -> ZoneInfo:
    try:
        return ZoneInfo(master["tz"])
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Moscow")


def local_dt(master, ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz_of(master))


def to_ts(master, naive_local: datetime) -> int:
    return int(naive_local.replace(tzinfo=tz_of(master)).timestamp())


def fmt_when(master, ts: int) -> str:
    d = local_dt(master, ts)
    return f"{WEEKDAY_SHORT[d.weekday()]} {d:%d.%m} в {d:%H:%M}"


def client_link(bot_username: str, client) -> str:
    return f"https://t.me/{bot_username}?start=c_{client['link_token']}"


def invite_interval_days(master, service: str) -> int:
    s = service.lower()
    for key, days in SERVICE_INTERVALS:
        if key in s:
            return max(days, master["interval_days"])
    return master["interval_days"]


# --- мастер добавляет визит ---

def add_visit_from_text(db: DB, master, text: str, now: int, bot_username: str) -> Reply:
    try:
        parsed = parse_visit(text, local_dt(master, now).replace(tzinfo=None))
    except ParseError as e:
        return Reply(
            f"🤔 {e}.\n\nПример: «Аня, пт 14:00, коррекция» или «Маша завтра в 15 брови +79991234567»"
        )

    client = db.find_client(master["id"], parsed.name)
    if client is None:
        client_id = db.add_client(master["id"], parsed.name, parsed.phone)
    else:
        client_id = client["id"]
        if parsed.phone and parsed.phone != client["phone"]:
            db.update_client(client_id, phone=parsed.phone)
    client = db.get_client(client_id)

    starts_at = to_ts(master, parsed.starts_at)
    visit_id = db.add_visit(master["id"], client_id, starts_at, parsed.service)
    db.cancel_pending_invites(client_id)  # уже записалась, приглашать не нужно
    db.log("visit_added", master_id=master["id"], visit_id=visit_id, client_id=client_id, text=text)

    lines = [f"✅ Записала: {h(client['name'])}, {fmt_when(master, starts_at)}, {h(parsed.service)}."]
    if client["tg_id"]:
        lines.append("Клиентка подключена — за сутки бот попросит её подтвердить визит.")
    else:
        lines.append(
            "\nКлиентка ещё не подключена к боту. Отправьте ей это сообщение один раз:\n\n"
            f"«Чтобы не забыть о записи, нажмите на ссылку — бот напомнит за сутки: {client_link(bot_username, client)}»"
        )
        if client["phone"]:
            lines.append("\nЕсли не подключится — напомню ей по SMS.")
    return Reply("\n".join(lines), [[("Отменить эту запись", f"mx:{visit_id}")]])


def cancel_visit_by_master(db: DB, master, visit_id: int) -> str:
    visit = db.get_visit(visit_id)
    if visit is None or visit["master_id"] != master["id"]:
        return "Запись не найдена"
    if visit["status"] not in ("scheduled", "confirmed", "reschedule"):
        return "Эта запись уже закрыта"
    db.update_visit(visit_id, status="cancelled")
    db.log("visit_cancelled_by_master", master_id=master["id"], visit_id=visit_id)
    return "Запись отменена, напоминаний по ней не будет"


# --- клиентка подключается ---

def link_client(db: DB, token: str, tg_id: int, now: int) -> Reply:
    client = db.get_client_by_token(token)
    if client is None:
        return Reply("Ссылка не работает. Попросите мастера прислать новую.")
    master = db.get_master(client["master_id"])
    db.update_client(client["id"], tg_id=tg_id)
    db.log("client_linked", master_id=master["id"], client_id=client["id"])

    text = (
        f"Готово, {h(client['name'])}! 🌸 Теперь мастер {h(master['name'])} сможет напоминать вам о записях здесь.\n\n"
        "Мы храним только ваше имя, контакт и даты визитов. Удалить свои данные: /forget"
    )
    visit = db.next_visit_of_client(client["id"], now)
    if visit:
        text += f"\n\nВаша ближайшая запись: {fmt_when(master, visit['starts_at'])}, {h(visit['service'])}."
    return Reply(text)


def forget_client_everywhere(db: DB, tg_id: int) -> str:
    rows = db.conn.execute("SELECT id, master_id FROM clients WHERE tg_id = ?", (tg_id,)).fetchall()
    for row in rows:
        db.forget_client(row["id"])
        db.log("client_forgotten", master_id=row["master_id"], client_id=row["id"])
    return "Ваши данные удалены, сообщений больше не будет." if rows else "У нас нет ваших данных."


# --- периодическая проверка ---

async def tick(db: DB, notifier: Notifier, now: int, bot_username: str) -> None:
    """Вызывается раз в минуту. Каждый шаг помечает выполненное в БД, поэтому повторный вызов безопасен."""
    await _send_reminders(db, notifier, now, bot_username)
    await _alert_no_answer(db, notifier, now)
    await _ask_outcomes(db, notifier, now)
    await _send_invites(db, notifier, now)
    await _send_reports(db, notifier, now)


async def _send_reminders(db: DB, notifier: Notifier, now: int, bot_username: str) -> None:
    for v in db.visits_needing_reminder(now, REMINDER_BEFORE):
        master = db.get_master(v["master_id"])
        client = db.get_client(v["client_id"])
        when = fmt_when(master, v["starts_at"])

        if v["starts_at"] - v["created_at"] < FRESH_BOOKING:
            db.update_visit(v["id"], reminder_sent_at=now, reminder_channel="skipped")
            db.log("reminder_skipped_fresh", master_id=master["id"], visit_id=v["id"])
            continue

        channel = None
        if client["tg_id"]:
            ok = await notifier.send(
                client["tg_id"],
                f"Здравствуйте, {h(client['name'])}! Это {h(master['name'])} 🌸\n"
                f"Напоминаю: {when} — {h(v['service'])}.\n\nПодтвердите, пожалуйста:",
                [[("✅ Приду", f"cv:{v['id']}:ok")],
                 [("🔄 Перенести", f"cv:{v['id']}:move"), ("❌ Отменить", f"cv:{v['id']}:cancel")]],
            )
            if ok:
                channel = "telegram"
            else:
                db.log("telegram_failed", master_id=master["id"], visit_id=v["id"], client_id=client["id"])

        if channel is None and client["phone"]:
            ok = await notifier.sms(
                client["phone"],
                f"{h(client['name'])}, напоминаем: {when} {h(v['service'])} у мастера {h(master['name'])}. "
                "Если планы изменились, напишите мастеру.",
            )
            if ok:
                channel = "sms"

        if channel is None:
            channel = "manual"
            await notifier.send(
                master["tg_id"],
                f"📩 {h(client['name'])} не подключена к боту — напомните ей сами о визите {when}.\n"
                f"Ссылка для подключения: {client_link(bot_username, client)}",
            )

        # По SMS и вручную ответа бот не увидит, поэтому тревогу «не ответила» для них не шлём.
        alert = now if channel in ("sms", "manual") else None
        db.update_visit(v["id"], reminder_sent_at=now, reminder_channel=channel, master_alert_sent_at=alert)
        db.log("reminder_sent", master_id=master["id"], visit_id=v["id"], client_id=client["id"], channel=channel)


async def _alert_no_answer(db: DB, notifier: Notifier, now: int) -> None:
    for v in db.visits_without_answer():
        if now < v["reminder_sent_at"] + NO_ANSWER_AFTER and now < v["starts_at"] - NO_ANSWER_LAST_CALL:
            continue
        if now >= v["starts_at"]:
            db.update_visit(v["id"], master_alert_sent_at=now)
            continue
        master = db.get_master(v["master_id"])
        client = db.get_client(v["client_id"])
        await notifier.send(
            master["tg_id"],
            f"⚠️ {h(client['name'])} не подтвердила визит {fmt_when(master, v['starts_at'])}.\n"
            "Напишите ей лично или подготовьте окно для другой клиентки.",
        )
        db.update_visit(v["id"], master_alert_sent_at=now)
        db.log("master_alerted_no_answer", master_id=master["id"], visit_id=v["id"])


async def _ask_outcomes(db: DB, notifier: Notifier, now: int) -> None:
    for v in db.visits_needing_outcome(now - OUTCOME_AFTER):
        if v["status"] == "reschedule":
            # Мастер переносит такие визиты сама, добавляя новую запись.
            db.update_visit(v["id"], status="moved", outcome_asked_at=now)
            continue
        master = db.get_master(v["master_id"])
        client = db.get_client(v["client_id"])
        await notifier.send(
            master["tg_id"],
            f"Как прошёл визит: {h(client['name'])}, {fmt_when(master, v['starts_at'])}?",
            [[("👍 Пришла", f"mo:{v['id']}:came"), ("🚫 Не пришла", f"mo:{v['id']}:noshow")]],
        )
        db.update_visit(v["id"], outcome_asked_at=now)


async def _send_invites(db: DB, notifier: Notifier, now: int) -> None:
    for inv in db.due_invites(now):
        client = db.get_client(inv["client_id"])
        master = db.get_master(inv["master_id"])
        if not client["tg_id"] or client["invite_consent"] != 1:
            db.update_invite(inv["id"], sent_at=now, response="stop")
            continue
        visit = db.get_visit(inv["visit_id"]) if inv["visit_id"] else None
        service = visit["service"] if visit else "следующий визит"
        ok = await notifier.send(
            client["tg_id"],
            f"{h(client['name'])}, здравствуйте! Это {h(master['name'])} 🌸\n"
            f"Пора записаться на {h(service)}. Хотите, подберу вам время?",
            [[("📅 Хочу записаться", f"ci:{inv['id']}:book")],
             [("Напомнить через неделю", f"ci:{inv['id']}:later")],
             [("Больше не напоминать", f"ci:{inv['id']}:stop")]],
        )
        db.update_invite(inv["id"], sent_at=now)
        db.log("invite_sent" if ok else "invite_failed", master_id=master["id"], client_id=client["id"])


async def _send_reports(db: DB, notifier: Notifier, now: int) -> None:
    for master in db.all_masters():
        d = local_dt(master, now)
        if d.weekday() != REPORT_WEEKDAY or d.hour < REPORT_HOUR:
            continue
        if master["last_report_at"] and now - master["last_report_at"] < 6 * DAY:
            continue
        if now - master["created_at"] < 5 * DAY:
            continue  # в первую неделю отчитываться ещё не о чем
        await notifier.send(master["tg_id"], weekly_report(db, master, now))
        db.update_master(master["id"], last_report_at=now)


# --- кнопки ---

async def handle_callback(db: DB, notifier: Notifier, from_tg: int, data: str, now: int) -> str:
    """Обрабатывает нажатие кнопки, возвращает короткий ответ для пользователя."""
    try:
        kind, obj_id, *rest = data.split(":")
        obj_id = int(obj_id)
        action = rest[0] if rest else ""
    except ValueError:
        return "Кнопка устарела"

    if kind == "cv":
        return await _client_visit_answer(db, notifier, from_tg, obj_id, action, now)
    if kind == "cc":
        return _client_consent(db, from_tg, obj_id, action, now)
    if kind == "ci":
        return await _client_invite_answer(db, notifier, from_tg, obj_id, action, now)

    master = db.get_master_by_tg(from_tg)
    if master is None:
        return "Кнопка устарела"
    if kind == "mo":
        return _master_outcome(db, master, obj_id, action, now)
    if kind == "mx":
        return cancel_visit_by_master(db, master, obj_id)
    return "Кнопка устарела"


async def _client_visit_answer(db: DB, notifier: Notifier, from_tg: int, visit_id: int, action: str, now: int) -> str:
    visit = db.get_visit(visit_id)
    client = db.get_client(visit["client_id"]) if visit else None
    if client is None or client["tg_id"] != from_tg:
        return "Кнопка устарела"
    if visit["starts_at"] <= now:
        return "Этот визит уже прошёл"
    if visit["status"] not in ("scheduled", "confirmed"):
        return "Мастер уже знает о вашем ответе 🙂"

    master = db.get_master(visit["master_id"])
    when = fmt_when(master, visit["starts_at"])
    contact = f'<a href="tg://user?id={client["tg_id"]}">{h(client["name"])}</a>'

    if action == "ok":
        db.update_visit(visit_id, status="confirmed")
        db.log("client_confirmed", master_id=master["id"], visit_id=visit_id, client_id=client["id"])
        await notifier.send(master["tg_id"], f"✅ {h(client['name'])} подтвердила визит {when}")
        if client["invite_consent"] is None:
            await notifier.send(
                client["tg_id"],
                "Напомнить вам записаться на следующий визит, когда подойдёт время?",
                [[("Да, напомните", f"cc:{client['id']}:yes"), ("Не нужно", f"cc:{client['id']}:no")]],
            )
        return "Спасибо, ждём вас!"
    if action == "move":
        db.update_visit(visit_id, status="reschedule")
        db.log("client_moved", master_id=master["id"], visit_id=visit_id, client_id=client["id"])
        await notifier.send(
            master["tg_id"],
            f"🔄 {contact} просит перенести визит {when}. Напишите ей, чтобы подобрать время, "
            "и добавьте новую запись.",
        )
        return "Передала мастеру — она напишет вам, чтобы подобрать время"
    if action == "cancel":
        db.update_visit(visit_id, status="cancelled")
        db.log("client_cancelled", master_id=master["id"], visit_id=visit_id, client_id=client["id"])
        await notifier.send(master["tg_id"], f"❌ {contact} отменила визит {when}. Окно свободно.")
        return "Запись отменена. Спасибо, что предупредили!"
    return "Кнопка устарела"


def _client_consent(db: DB, from_tg: int, client_id: int, action: str, now: int) -> str:
    client = db.get_client(client_id)
    if client is None or client["tg_id"] != from_tg:
        return "Кнопка устарела"
    agreed = action == "yes"
    db.update_client(client_id, invite_consent=1 if agreed else 0, consent_at=now)
    db.log("consent_given" if agreed else "consent_declined", master_id=client["master_id"], client_id=client_id)
    return "Хорошо, напомню 🙂 Отказаться можно в любой момент." if agreed else "Хорошо, не буду напоминать"


async def _client_invite_answer(db: DB, notifier: Notifier, from_tg: int, invite_id: int, action: str, now: int) -> str:
    inv = db.get_invite(invite_id)
    client = db.get_client(inv["client_id"]) if inv else None
    if client is None or client["tg_id"] != from_tg:
        return "Кнопка устарела"
    if inv["response"]:
        return "Уже передала ваш ответ 🙂"
    master = db.get_master(inv["master_id"])
    db.update_invite(invite_id, response=action)
    db.log(f"invite_{action}", master_id=master["id"], client_id=client["id"])

    if action == "book":
        contact = f'<a href="tg://user?id={client["tg_id"]}">{h(client["name"])}</a>'
        await notifier.send(master["tg_id"], f"📅 {contact} хочет записаться! Напишите ей, чтобы выбрать время.")
        return "Передала мастеру — она скоро напишет вам 🌸"
    if action == "later":
        db.add_invite(master["id"], client["id"], inv["visit_id"], now + LATER_DELAY)
        return "Хорошо, напомню через неделю"
    if action == "stop":
        db.update_client(client["id"], invite_consent=0, consent_at=now)
        return "Хорошо, больше не буду напоминать"
    return "Кнопка устарела"


def _master_outcome(db: DB, master, visit_id: int, action: str, now: int) -> str:
    visit = db.get_visit(visit_id)
    if visit is None or visit["master_id"] != master["id"]:
        return "Запись не найдена"
    if visit["status"] in ("attended", "no_show"):
        return "Уже отмечено"
    status = "attended" if action == "came" else "no_show"
    db.update_visit(visit_id, status=status)
    db.log(f"visit_{status}", master_id=master["id"], visit_id=visit_id, client_id=visit["client_id"])

    if status == "attended":
        client = db.get_client(visit["client_id"])
        if client["invite_consent"] == 1:
            due = visit["starts_at"] + invite_interval_days(master, visit["service"]) * DAY
            db.add_invite(master["id"], client["id"], visit_id, due)
        return "Отлично, отметила 👍"
    return "Отметила. Жаль 😔"


# --- отчёты ---

def _count_events(db: DB, master_id: int, kinds: tuple[str, ...], since: int, until: int) -> int:
    q = f"SELECT COUNT(*) FROM events WHERE master_id = ? AND ts >= ? AND ts <= ? AND kind IN ({','.join('?' * len(kinds))})"
    return db.conn.execute(q, (master_id, since, until, *kinds)).fetchone()[0]


def master_stats(db: DB, master, since: int, until: int) -> dict:
    visits = db.visits_between(master["id"], since, until)
    clients = db.clients_of(master["id"])
    return {
        "visits": len(visits),
        "attended": sum(v["status"] == "attended" for v in visits),
        "no_show": sum(v["status"] == "no_show" for v in visits),
        "unmarked": sum(v["status"] in ("scheduled", "confirmed") for v in visits),
        "confirmed": _count_events(db, master["id"], ("client_confirmed",), since, until),
        "moved": _count_events(db, master["id"], ("client_moved",), since, until),
        "cancelled": _count_events(db, master["id"], ("client_cancelled",), since, until),
        "invites_sent": _count_events(db, master["id"], ("invite_sent",), since, until),
        "invites_booked": _count_events(db, master["id"], ("invite_book",), since, until),
        "clients": len(clients),
        "clients_linked": sum(1 for c in clients if c["tg_id"]),
        "consents": sum(1 for c in clients if c["invite_consent"] == 1),
    }


def weekly_report(db: DB, master, now: int) -> str:
    s = master_stats(db, master, now - 7 * DAY, now)
    lines = [
        "📊 Ваша неделя с «Коррекцией»",
        "",
        f"Визитов: {s['visits']}",
        f"✅ Клиентки подтвердили: {s['confirmed']}",
        f"🔄 Попросили перенос заранее: {s['moved']}",
        f"❌ Отменили заранее: {s['cancelled']}",
        f"🚫 Не пришли: {s['no_show']}",
    ]
    if s["invites_sent"]:
        lines.append(f"📅 Приглашений на коррекцию: {s['invites_sent']}, хотят записаться: {s['invites_booked']}")
    lines += [
        "",
        f"Клиенток подключено к боту: {s['clients_linked']} из {s['clients']}",
    ]
    if s["moved"] + s["cancelled"]:
        lines.append(
            f"\n{s['moved'] + s['cancelled']} раз(а) вы узнали об изменении планов заранее, а не в день визита — "
            "эти окна можно было отдать другим клиенткам."
        )
    if s["unmarked"]:
        lines.append(f"\nНе отмечено, пришла ли клиентка: {s['unmarked']} — это нужно для честной статистики.")
    return "\n".join(lines)


def pilot_summary(db: DB, now: int) -> str:
    """Сводка для оператора пилота: метрики решения «продолжать или нет»."""
    lines = ["Сводка пилота за 30 дней:"]
    for m in db.all_masters():
        s = master_stats(db, m, now - 30 * DAY, now)
        marked = s["attended"] + s["no_show"]
        rate = f"{100 * s['no_show'] / marked:.0f}%" if marked else "—"
        linked = f"{100 * s['clients_linked'] / s['clients']:.0f}%" if s["clients"] else "—"
        lines.append(
            f"\n• {h(m['name'])} (id {m['id']}): визитов {s['visits']}, неявок {s['no_show']} из {marked} ({rate}); "
            f"подтвердили {s['confirmed']}, перенос/отмена заранее {s['moved'] + s['cancelled']}; "
            f"клиенток в боте {linked}, согласий {s['consents']}; "
            f"приглашений {s['invites_sent']} → хотят записаться {s['invites_booked']}"
        )
    if len(lines) == 1:
        lines.append("пока нет мастеров")
    return "\n".join(lines)


def upcoming_text(db: DB, master, now: int) -> str:
    rows = db.upcoming_visits(master["id"], now, now + 14 * DAY)
    if not rows:
        return "На ближайшие две недели записей нет. Добавьте визит сообщением: «Аня, пт 14:00, коррекция»"
    marks = {"scheduled": "⏳", "confirmed": "✅", "reschedule": "🔄"}
    lines = ["Записи на 2 недели (⏳ ждём подтверждения, ✅ подтвердила, 🔄 просит перенос):", ""]
    for r in rows:
        bot_mark = "" if r["client_tg"] else " 📵"
        lines.append(f"{marks[r['status']]} {fmt_when(master, r['starts_at'])} — {h(r['client_name'])}{bot_mark}, {h(r['service'])}  /del{r['id']}")
    lines.append("\n📵 — клиентка не подключена к боту")
    return "\n".join(lines)
