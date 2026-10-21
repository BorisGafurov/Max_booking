from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import BOOKING_URL


def get_booking_keyboard() -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(LinkButton(text="✂️ Записаться онлайн", url=BOOKING_URL))
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))
    return builder.as_markup()
