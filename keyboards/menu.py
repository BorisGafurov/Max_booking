from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


def get_main_menu(is_admin: bool = False) -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="✂️ Записаться", payload="menu:booking"),
        CallbackButton(text="📍 Как добраться", payload="menu:navigation"),
    )
    builder.row(
        CallbackButton(text="📋 Мои записи", payload="menu:my_appts"),
        CallbackButton(text="🎁 Пригласить друга", payload="menu:referrals"),
    )
    builder.row(
        CallbackButton(text="🧴 Советы и товары", payload="menu:advice"),
        CallbackButton(text="🌐 Наш сайт", payload="menu:site"),
    )

    if is_admin:
        builder.row(
            CallbackButton(text="⚙️ Админ-панель", payload="menu:admin"),
        )

    return builder.as_markup()
