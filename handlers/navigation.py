from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.filters.command import Command
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated

from config import SALON_ADDRESS, WORK_HOURS
from keyboards.navigation import get_navigation_keyboard

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
    await event.edit(
        text=NAV_TEXT,
        attachments=[get_navigation_keyboard()],
        format=TextFormat.HTML,
    )
