from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.filters.command import Command
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated

from keyboards.booking import get_booking_keyboard
from services.referrals import get_user_phone, is_phone_number, link_user_phone

router = Router(router_id="booking")

BOOKING_TEXT = "Нажми кнопку ниже, выбери услугу и свободное время:"


@router.message_created(Command(["book", "booking"]))
@router.message_created(
    F.message.body.text.in_({"✂️ Записаться", "Записаться", "записаться"})
)
async def booking_message(event: MessageCreated) -> None:
    if not event.message or not event.message.sender:
        return

    phone = get_user_phone(event.message.sender.user_id, platform="max")
    if phone:
        await event.message.answer(
            text=BOOKING_TEXT,
            attachments=[get_booking_keyboard()],
            format=TextFormat.HTML,
        )
    else:
        await event.message.answer(
            text="✂️ <b>Онлайн-запись</b>\n\n"
                 "Чтобы закрепить за тобой бонусы и объединить профиль, отправь свой номер телефона сообщением (например, +79991234567):\n\n"
                 "Или переходи сразу к выбору времени:",
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


@router.message_created(
    lambda event: bool(
        event.message
        and event.message.body
        and event.message.body.text
        and is_phone_number(event.message.body.text)
    )
)
async def phone_message_handler(event: MessageCreated) -> None:
    if not event.message or not event.message.body or not event.message.body.text or not event.message.sender:
        return
    text = event.message.body.text
    user_id = event.message.sender.user_id
    success, _ = link_user_phone(user_id, text, platform="max")
    if success:
        await event.message.answer(
            text="✅ <b>Номер телефона успешно сохранен!</b>\n\n"
                 "Теперь твои бонусы и приглашённые друзья объединены в едином профиле.\n\n"
                 "Нажми кнопку ниже, выбери услугу и свободное время:",
            attachments=[get_booking_keyboard()],
            format=TextFormat.HTML,
        )
