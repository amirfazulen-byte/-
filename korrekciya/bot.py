"""Telegram-бот «Коррекция» (консьерж-версия для пилота). Запуск: python -m korrekciya.bot"""

import asyncio
import logging
import os
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from . import logic
from .db import DB
from .logic import Buttons, h
from .sms import SmsRu

log = logging.getLogger("korrekciya")

HELP = """Я помогаю не терять клиенток на неявках 🌸

<b>Добавить запись</b> — просто напишите сообщение:
• Аня, пт 14:00, коррекция
• Маша завтра в 15 брови
• Оля 12.11 11:30 ламинирование +79991234567

Или просто <b>перешлите мне сообщение клиентки</b>, где она называет день и время («давайте в пятницу в 14») — имя я возьму из пересылки.

За сутки до визита я попрошу клиентку подтвердить запись, а вам сообщу, если она не ответила. После визита спрошу, пришла ли она — так в отчёте будет честная статистика.

<b>Команды</b>
/list — записи на 2 недели
/clients — клиентки и ссылки для подключения
/report — отчёт за неделю
/tz Europe/Kazan — ваш часовой пояс
/interval 21 — через сколько дней приглашать на коррекцию"""


def keyboard(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in buttons]
    )


class TelegramNotifier:
    def __init__(self, bot: Bot, sms: SmsRu | None):
        self.bot = bot
        self.sms_client = sms

    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> bool:
        try:
            await self.bot.send_message(chat_id, text, reply_markup=keyboard(buttons))
            return True
        except TelegramAPIError as e:
            log.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
            return False

    async def sms(self, phone: str, text: str) -> bool:
        if self.sms_client is None:
            return False
        return await self.sms_client.send(phone, text)


def build_router(db: DB, notifier: TelegramNotifier, cfg: dict) -> Router:
    router = Router()
    admin_id = cfg["admin_id"]

    def master_of(message: Message):
        return db.get_master_by_tg(message.from_user.id)

    @router.message(CommandStart(deep_link=True))
    async def start_deep(message: Message, command: CommandObject):
        arg = command.args or ""
        if arg.startswith("c_"):
            reply = logic.link_client(db, arg[2:], message.from_user.id, int(time.time()))
            await message.answer(reply.text)
            return
        if arg.startswith("m_"):
            if arg[2:] != cfg["invite_code"]:
                await message.answer("Код приглашения не подходит. Напишите тому, кто дал вам ссылку.")
                return
            if master_of(message) is None:
                name = message.from_user.first_name or "Мастер"
                db.add_master(message.from_user.id, name, cfg["default_tz"])
                if admin_id:
                    await notifier.send(admin_id, f"🆕 Новый мастер: {h(name)} (tg {message.from_user.id})")
            await message.answer(f"Добро пожаловать! 🌸\n\n{HELP}\n\nЧасовой пояс сейчас: {cfg['default_tz']}")
            return
        await start_plain(message)

    @router.message(CommandStart())
    async def start_plain(message: Message):
        if master_of(message):
            await message.answer(HELP)
        else:
            await message.answer(
                "Здравствуйте! Я напоминаю о записях к мастеру 🌸\n"
                "Чтобы подключиться, откройте ссылку, которую прислал вам мастер."
            )

    @router.message(Command("help"))
    async def help_cmd(message: Message):
        await message.answer(HELP if master_of(message) else "Я напоминаю о записях. Удалить свои данные: /forget")

    @router.message(Command("forget"))
    async def forget(message: Message):
        await message.answer(logic.forget_client_everywhere(db, message.from_user.id))

    @router.message(Command("stats"))
    async def stats(message: Message):
        if admin_id and message.from_user.id == admin_id:
            await message.answer(logic.pilot_summary(db, int(time.time())))

    @router.message(Command("list"))
    async def list_cmd(message: Message):
        if m := master_of(message):
            await message.answer(logic.upcoming_text(db, m, int(time.time())))

    @router.message(F.text.regexp(r"^/del(\d+)"))
    async def del_cmd(message: Message):
        if m := master_of(message):
            visit_id = int(message.text[4:].split("@")[0])
            await message.answer(logic.cancel_visit_by_master(db, m, visit_id))

    @router.message(Command("clients"))
    async def clients_cmd(message: Message):
        m = master_of(message)
        if not m:
            return
        rows = db.clients_of(m["id"])
        if not rows:
            await message.answer("Клиенток пока нет — они появятся, когда вы добавите первую запись.")
            return
        lines = ["Ваши клиентки (✅ подключена к боту, 📵 нет):", ""]
        for c in rows:
            if c["tg_id"]:
                lines.append(f"✅ {h(c['name'])}")
            else:
                lines.append(f"📵 {h(c['name'])} — {logic.client_link(cfg['bot_username'], c)}")
        await message.answer("\n".join(lines), disable_web_page_preview=True)

    @router.message(Command("report"))
    async def report_cmd(message: Message):
        if m := master_of(message):
            await message.answer(logic.weekly_report(db, m, int(time.time())))

    @router.message(Command("tz"))
    async def tz_cmd(message: Message, command: CommandObject):
        m = master_of(message)
        if not m:
            return
        zone = (command.args or "").strip()
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            await message.answer(f"Сейчас: {m['tz']}. Чтобы сменить, напишите, например: /tz Asia/Yekaterinburg")
            return
        db.update_master(m["id"], tz=zone)
        await message.answer(f"Часовой пояс: {zone}")

    @router.message(Command("interval"))
    async def interval_cmd(message: Message, command: CommandObject):
        m = master_of(message)
        if not m:
            return
        arg = (command.args or "").strip()
        if not arg.isdigit() or not 7 <= int(arg) <= 120:
            await message.answer(f"Сейчас: {m['interval_days']} дней. Пример: /interval 21 (от 7 до 120)")
            return
        db.update_master(m["id"], interval_days=int(arg))
        await message.answer(f"Буду приглашать на коррекцию через {arg} дней после визита (для ламинирования — не раньше чем через 42)")

    @router.message(F.forward_origin)
    async def forwarded(message: Message):
        m = master_of(message)
        if m is None:
            await message.answer("Я только напоминаю о записях. Если хотите что-то спросить, напишите мастеру напрямую 🌸")
            return
        origin = message.forward_origin
        if isinstance(origin, MessageOriginUser):
            own = origin.sender_user.id == message.from_user.id
            sender_name = None if own else origin.sender_user.first_name
        elif isinstance(origin, MessageOriginHiddenUser):
            # Клиентка скрыла аккаунт в пересылках — Telegram отдаёт только её имя.
            sender_name = origin.sender_user_name.split()[0] if origin.sender_user_name.strip() else None
        else:
            sender_name = None
        reply = logic.add_visit_from_forward(
            db, m, message.text or message.caption or "", sender_name,
            int(origin.date.timestamp()), int(time.time()), cfg["bot_username"],
        )
        await message.answer(reply.text, reply_markup=keyboard(reply.buttons), disable_web_page_preview=True)

    @router.message(F.voice)
    async def voice(message: Message):
        if master_of(message):
            await message.answer("Голосовые я пока не понимаю 🙈 Напишите, пожалуйста, текстом: «Аня, пт 14:00, коррекция»")

    @router.message(F.text & ~F.text.startswith("/"))
    async def text(message: Message):
        m = master_of(message)
        if m is None:
            await message.answer("Я только напоминаю о записях. Если хотите что-то спросить, напишите мастеру напрямую 🌸")
            return
        reply = logic.add_visit_from_text(db, m, message.text, int(time.time()), cfg["bot_username"])
        await message.answer(reply.text, reply_markup=keyboard(reply.buttons), disable_web_page_preview=True)

    @router.callback_query()
    async def callback(query: CallbackQuery):
        answer = await logic.handle_callback(db, notifier, query.from_user.id, query.data or "", int(time.time()))
        await query.answer(answer)
        if query.message:
            # Ответ дописываем в само сообщение с кнопками, чтобы порядок в чате был понятным.
            try:
                await query.message.edit_text(f"{query.message.html_text}\n\n<i>→ {h(answer)}</i>", reply_markup=None)
            except TelegramAPIError:
                pass

    return router


async def tick_loop(db: DB, notifier: TelegramNotifier, cfg: dict) -> None:
    last_error_report = 0.0
    while True:
        try:
            await logic.tick(db, notifier, int(time.time()), cfg["bot_username"])
        except Exception:
            log.exception("Ошибка в периодической проверке")
            # Тихий сбой напоминаний — главный риск продукта, поэтому сообщаем оператору (не чаще раза в час).
            if cfg["admin_id"] and time.time() - last_error_report > 3600:
                last_error_report = time.time()
                await notifier.send(cfg["admin_id"], "🔥 Ошибка в рассылке напоминаний — проверьте логи бота")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ["BOT_TOKEN"]
    sms_id = os.environ.get("SMS_RU_API_ID")
    cfg = {
        "invite_code": os.environ["MASTER_INVITE_CODE"],
        "admin_id": int(os.environ["ADMIN_TG_ID"]) if os.environ.get("ADMIN_TG_ID") else None,
        "default_tz": os.environ.get("DEFAULT_TZ", "Europe/Moscow"),
    }

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    cfg["bot_username"] = me.username

    db = DB(os.environ.get("DB_PATH", "korrekciya.db"))
    notifier = TelegramNotifier(bot, SmsRu(sms_id, os.environ.get("SMS_SENDER")) if sms_id else None)

    dp = Dispatcher()
    dp.include_router(build_router(db, notifier, cfg))

    log.info("Бот @%s запущен. Ссылка для мастеров: https://t.me/%s?start=m_%s",
             me.username, me.username, cfg["invite_code"])
    ticker = asyncio.create_task(tick_loop(db, notifier, cfg))
    try:
        await dp.start_polling(bot)
    finally:
        ticker.cancel()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
