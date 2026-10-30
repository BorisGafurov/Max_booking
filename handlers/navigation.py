from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.filters.command import Command
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated

from config import SALON_ADDRESS, WORK_HOURS
from keyboards.navigation import get_navigation_keyboard
from handlers.ui import edit_or_send as _edit_or_send

router = Router(router_id="navigation")

NAV_TEXT = (
    f"📍 <b>Как добраться:</b>\n\n"
    f"<b>Адрес:</b>\n{SALON_ADDRESS}\n\n"
    f"<b>Часы работы:</b>\n{WORK_HOURS}"
)


@router.message_created(Command(["location", "address", "map"]))
@router.message_created(
    F.message.body.text.in_({"📍 Как добраться", "Как добраться", "как добраться"})
)
async def navigation_message(event: MessageCreated) -> None:
    if not event.message:
        return
    await event.message.answer(
        text=NAV_TEXT,
        attachments=[get_navigation_keyboard()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload == "menu:navigation")
async def navigation_callback(event: MessageCallback) -> None:
    await _edit_or_send(event, 
        text=NAV_TEXT,
        attachments=[get_navigation_keyboard()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload == "menu:site")
@router.message_created(F.message.body.text.in_({"🌐 Наш сайт", "Наш сайт", "Сайт", "сайт"}))
async def site_callback(event: MessageCallback | MessageCreated) -> None:
    from config import WEB_APP_URL
    site_text = (
        "🌐 <b>Официальный сайт и онлайн-запись:</b>\n\n"
        f"{WEB_APP_URL}\n\n"
        "На сайте вы можете выбрать любые услуги мастера Бориса Гафурова, удобное время визита и получить бонус 300 ₽!"
    )
    if isinstance(event, MessageCallback):
        await _edit_or_send(event, text=site_text, format=TextFormat.HTML)
    elif event.message:
        await event.message.answer(text=site_text, format=TextFormat.HTML)
