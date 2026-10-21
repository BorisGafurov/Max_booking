from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import SALON_LATITUDE, SALON_LONGITUDE


def get_navigation_keyboard() -> AttachmentButton:
    yandex_maps_url = (
        f"https://yandex.ru/maps/?pt={SALON_LONGITUDE},{SALON_LATITUDE}&z=17&l=map"
    )
    builder = InlineKeyboardBuilder()
    builder.row(LinkButton(text="🗺 Открыть в Яндекс Картах", url=yandex_maps_url))
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))
    return builder.as_markup()
