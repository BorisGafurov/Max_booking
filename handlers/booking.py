from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.filters.command import Command
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated

from keyboards.booking import get_booking_keyboard

router = Router(router_id="booking")

BOOKING_TEXT = "Нажми кнопку ниже, выбери услугу и свободное время:"


@router.message_created(Command(["book", "booking"]))
@router.message_created(
    F.message.body.text.in_({"✂️ Записаться", "Записаться", "записаться"})
)
async def booking_message(event: MessageCreated) -> None:
    if not event.message:
        return
    await event.message.answer(
        text=BOOKING_TEXT,
        attachments=[get_booking_keyboard()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload == "menu:booking")
async def booking_callback(event: MessageCallback) -> None:
    await event.edit(
        text=BOOKING_TEXT,
        attachments=[get_booking_keyboard()],
        format=TextFormat.HTML,
    )
