import asyncio
import logging
from maxapi import Bot, Dispatcher, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.types.command import BotCommand
from maxapi.types.updates.message_created import MessageCreated

from config import ADMIN_IDS, MAX_BOT_TOKEN
from database import init_db
from handlers.advice import router as advice_router
from handlers.booking import router as booking_router
from handlers.my_appointments import router as my_appointments_router
from handlers.navigation import router as navigation_router
from handlers.referrals import router as referrals_router
from handlers.start import router as start_router
from keyboards.menu import get_main_menu
from services.reminder_worker import run_reminder_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

fallback_router = Router(router_id="fallback")


@fallback_router.message_created()
async def fallback_handler(event: MessageCreated) -> None:
    if not event.message or not event.message.sender:
        return

    is_admin = event.message.sender.user_id in ADMIN_IDS
    await event.message.answer(
        text="Пожалуйста, выбери нужный раздел с помощью меню ниже 👇",
        attachments=[get_main_menu(is_admin=is_admin)],
        format=TextFormat.HTML,
    )


async def setup_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(name="start", description="Главное меню"),
        BotCommand(name="book", description="Онлайн-запись"),
        BotCommand(name="location", description="Как добраться и адрес"),
        BotCommand(name="advice", description="Советы по уходу и товарам"),
        BotCommand(name="referral", description="Реферальная программа и бонусы"),
        BotCommand(name="myid", description="Узнать свой MAX ID"),
        BotCommand(name="admin", description="Админ-панель (для мастеров)"),
    ]
    try:
        await bot.set_commands(*commands)
        logger.info("Команды бота успешно зарегистрированы в MAX.")
    except Exception as e:
        logger.warning("Не удалось зарегистрировать команды бота: %s", e)


async def main() -> None:
    if not MAX_BOT_TOKEN or MAX_BOT_TOKEN == "your_max_bot_token_here":
        raise SystemExit(
            "ОШИБКА: Добавь токен бота MAX_BOT_TOKEN в файл .env (см. .env.example)"
        )

    init_db()

    dp = Dispatcher()

    dp.include_routers(
        start_router,
        referrals_router,
        booking_router,
        my_appointments_router,
        navigation_router,
        advice_router,
        fallback_router,
    )

    bot = Bot(token=MAX_BOT_TOKEN, format=TextFormat.HTML)

    try:
        await bot.delete_webhook()
        await setup_bot_commands(bot)

        worker_task = asyncio.create_task(run_reminder_worker(bot, "max"))
        logger.info("MAX Бот успешно запущен. Нажми Ctrl+C для остановки.")
        try:
            await dp.start_polling(bot)
        finally:
            worker_task.cancel()
    finally:
        await bot.close_session()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
