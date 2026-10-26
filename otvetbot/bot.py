"""Telegram-бот «ОтветБот»: черновики ответов на отзывы и вопросы WB. Запуск: python -m otvetbot.bot"""

import asyncio
import logging
import os
import time

import aiohttp
import anthropic
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .db import DB, TokenBox
from .drafter import ClaudeDrafter
from .service import Buttons, Service, h, pilot_stats
from .wb import WBClient

log = logging.getLogger("otvetbot")

TOKEN_HELP = """Чтобы я видел отзывы и мог публиковать ответы, нужен API-токен Wildberries:

1. Личный кабинет продавца WB → Настройки → <b>Доступ к API</b>.
2. Создайте новый токен. Категории:
   • <b>Вопросы и отзывы</b> — обязательно;
   • <b>Контент</b> — по желанию: тогда я знаю размеры и характеристики из карточек и отвечаю точнее.
3. Галочку «Только на чтение» <b>не</b> ставьте — иначе я не смогу публиковать.
4. Пришлите токен сюда одним сообщением.

Токен хранится в зашифрованном виде и используется только для отзывов и вопросов. Отключить доступ можно в любой момент, удалив токен в кабинете WB."""

HELP = """Я готовлю черновики ответов на отзывы и вопросы покупателей WB. <b>Ничего не публикую без вас</b>: каждый черновик приходит сюда с кнопками «Опубликовать / Изменить / Пропустить».

Если отзыв про брак, возврат, здоровье, детей или вопрос, на который нет данных в карточке, — помечаю «⚠️ нужно ваше решение».

/style — прислать примеры ваших ответов, чтобы черновики звучали как вы
/auto — включить или выключить автопубликацию ответов на 5★ без рисков
/status — лимит и состояние
/pause, /resume — приостановить или продолжить
/token — заменить токен WB"""


def keyboard(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in buttons]
    )


class TelegramNotifier:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> int | None:
        try:
            msg = await self.bot.send_message(chat_id, text, reply_markup=keyboard(buttons))
            return msg.message_id
        except TelegramAPIError as e:
            log.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
            return None

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=None)
        except TelegramAPIError as e:
            log.info("Не удалось изменить сообщение %s: %s", message_id, e)


def build_router(service: Service, cfg: dict) -> Router:
    router = Router()
    db = service.db
    background: set[asyncio.Task] = set()
    admin_id = cfg["admin_id"]

    def seller_of(message: Message):
        return db.seller_by_tg(message.from_user.id)

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject):
        seller = seller_of(message)
        if seller is None:
            if cfg["invite_code"] and (command.args or "") != cfg["invite_code"]:
                await message.answer("Бот пока в закрытом тесте. Напишите автору, чтобы получить ссылку-приглашение.")
                return
            db.add_seller(message.from_user.id, message.from_user.full_name or "Продавец")
            if admin_id:
                await service.notifier.send(admin_id, f"🆕 Продавец: {h(message.from_user.full_name)} (tg {message.from_user.id})")
            await message.answer(f"Здравствуйте! 👋\n\n{HELP}\n\n{TOKEN_HELP}")
            return
        await message.answer(HELP if seller["token_ok"] else TOKEN_HELP)

    @router.message(Command("help"))
    async def help_cmd(message: Message):
        await message.answer(HELP)

    @router.message(Command("token"))
    async def token_cmd(message: Message):
        if seller := seller_of(message):
            db.update_seller(seller["id"], awaiting="token")
            await message.answer(TOKEN_HELP)

    @router.message(Command("style"))
    async def style_cmd(message: Message):
        if seller := seller_of(message):
            db.update_seller(seller["id"], awaiting="style")
            await message.answer(
                "Пришлите одним сообщением 10–20 своих прошлых ответов покупателям (каждый с новой строки). "
                "Я буду писать черновики в том же тоне. Чтобы сбросить стиль, пришлите слово «сброс»."
            )

    @router.message(Command("auto"))
    async def auto_cmd(message: Message):
        if seller := seller_of(message):
            new = 0 if seller["autopublish_5"] else 1
            db.update_seller(seller["id"], autopublish_5=new)
            db.log("autopublish_toggled", seller_id=seller["id"], on=bool(new))
            await message.answer(
                "Автопубликация включена: ответы на 5★ без пометок я публикую сам и присылаю уведомление. "
                "Всё остальное — по-прежнему через вас." if new else
                "Автопубликация выключена: каждый ответ — только после вашего подтверждения."
            )

    @router.message(Command("pause"))
    async def pause_cmd(message: Message):
        if seller := seller_of(message):
            db.update_seller(seller["id"], paused=1)
            await message.answer("Пауза. Новые черновики не готовлю. Продолжить: /resume")

    @router.message(Command("resume"))
    async def resume_cmd(message: Message):
        if seller := seller_of(message):
            db.update_seller(seller["id"], paused=0)
            await message.answer("Продолжаю работу.")

    @router.message(Command("status"))
    async def status_cmd(message: Message):
        if seller := seller_of(message):
            used = db.answers_this_month(seller["id"], int(time.time()))
            waiting = len(db.items_with_status(seller["id"], "drafted"))
            await message.answer(
                f"Токен WB: {'✅ подключён' if seller['token_ok'] else '❌ не подключён (/token)'}\n"
                f"Ответов в этом месяце: {used} из {seller['monthly_limit']}\n"
                f"Черновиков ждут решения: {waiting}\n"
                f"Автопубликация 5★: {'вкл' if seller['autopublish_5'] else 'выкл'}"
                f"{' · ⏸ на паузе' if seller['paused'] else ''}"
            )

    @router.message(Command("stats"))
    async def stats_cmd(message: Message):
        if admin_id and message.from_user.id == admin_id:
            await message.answer(pilot_stats(db))

    @router.message(Command("setlimit"))
    async def setlimit_cmd(message: Message, command: CommandObject):
        if not (admin_id and message.from_user.id == admin_id):
            return
        try:
            tg_id, limit = (int(x) for x in (command.args or "").split())
        except ValueError:
            await message.answer("Формат: /setlimit <tg_id> <ответов в месяц>")
            return
        seller = db.seller_by_tg(tg_id)
        if seller is None:
            await message.answer("Продавец не найден")
            return
        db.update_seller(seller["id"], monthly_limit=limit)
        await message.answer(f"Лимит для {h(seller['name'])}: {limit} ответов в месяц")

    @router.message(F.text & ~F.text.startswith("/"))
    async def text(message: Message):
        seller = seller_of(message)
        if seller is None:
            await message.answer("Нажмите /start, чтобы начать.")
            return
        awaiting = seller["awaiting"] or ""
        if awaiting == "token":
            ok, reply = await service.connect_token(seller, message.text)
            await message.answer(reply)
            if ok:
                try:
                    await message.delete()  # не держим токен в истории чата
                except TelegramAPIError:
                    pass
                task = asyncio.create_task(service.poll(db.seller(seller["id"])))
                background.add(task)
                task.add_done_callback(background.discard)
        elif awaiting == "style":
            style = "" if message.text.strip().lower() == "сброс" else message.text.strip()
            db.update_seller(seller["id"], style_examples=style, awaiting=None)
            await message.answer("Стиль сброшен." if not style else "Запомнил стиль — новые черновики будут в нём.")
        elif awaiting.startswith("edit:"):
            await message.answer(await service.on_edited_text(seller, message.text))
        else:
            await message.answer("Я присылаю черновики сам, когда появляются новые отзывы. Команды: /help")

    @router.callback_query()
    async def callback(query: CallbackQuery):
        answer = await service.on_button(query.from_user.id, query.data or "")
        await query.answer(answer)

    return router


async def poll_loop(service: Service, cfg: dict) -> None:
    last_error_report = 0.0
    while True:
        started = time.monotonic()
        try:
            sellers = service.db.active_sellers()
            sem = asyncio.Semaphore(5)

            async def one(seller):
                async with sem:
                    await service.poll(seller)

            await asyncio.gather(*(one(s) for s in sellers))
            await service.send_digests(int(time.time()))
        except Exception:
            log.exception("Ошибка в цикле опроса")
            if cfg["admin_id"] and time.time() - last_error_report > 3600:
                last_error_report = time.time()
                await service.notifier.send(cfg["admin_id"], "🔥 Ошибка в цикле опроса WB — проверьте логи")
        await asyncio.sleep(max(60, cfg["poll_seconds"] - (time.monotonic() - started)))


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = {
        "admin_id": int(os.environ["ADMIN_TG_ID"]) if os.environ.get("ADMIN_TG_ID") else None,
        "invite_code": os.environ.get("INVITE_CODE", ""),
        "poll_seconds": int(os.environ.get("POLL_MINUTES", "15")) * 60,
    }
    bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    db = DB(os.environ.get("DB_PATH", "otvetbot.db"))
    box = TokenBox(os.environ["TOKEN_ENCRYPTION_KEY"])

    async with aiohttp.ClientSession() as http:
        drafter = ClaudeDrafter(
            anthropic.AsyncAnthropic(),
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
            effort=os.environ.get("CLAUDE_EFFORT", "low"),
        )
        service = Service(db, box, lambda token: WBClient(token, http), drafter, TelegramNotifier(bot))
        dp = Dispatcher()
        dp.include_router(build_router(service, cfg))
        me = await bot.get_me()
        link = f"https://t.me/{me.username}" + (f"?start={cfg['invite_code']}" if cfg["invite_code"] else "")
        log.info("Бот @%s запущен. Ссылка для продавцов: %s", me.username, link)
        poller = asyncio.create_task(poll_loop(service, cfg))
        try:
            await dp.start_polling(bot)
        finally:
            poller.cancel()
            db.close()


if __name__ == "__main__":
    asyncio.run(main())
