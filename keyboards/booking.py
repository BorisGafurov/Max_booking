from typing import Any
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import BOOKING_URL
from services.booking import SERVICES, calculate_services


def get_booking_keyboard() -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(LinkButton(text="🌐 Онлайн-запись «Срез»", url=BOOKING_URL))
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))
    return builder.as_markup()


def get_services_keyboard(selected_keys: list[str] | set[str]) -> AttachmentButton:
    calc = calculate_services(selected_keys)
    active_keys = set(calc["keys"])

    builder = InlineKeyboardBuilder()
    for key, info in SERVICES.items():
        is_selected = key in active_keys
        icon = "✅" if is_selected else "⚪️"
        btn_text = f"{icon} {info['name']} · {info['price']} ₽ ({info['duration_text']})"
        builder.row(CallbackButton(text=btn_text, payload=f"book:toggle:{key}"))

    next_text = f"📅 Выбрать время ({calc['total_price']} ₽ · {calc['duration_text']})"
    builder.row(CallbackButton(text=next_text, payload="book:choose_day"))
    builder.row(LinkButton(text="🌐 Онлайн-запись «Срез»", url=BOOKING_URL))
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))

    return builder.as_markup()


def get_days_keyboard(days_map: dict[str, Any]) -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    day_items = list(days_map.values())[:8]

    for i in range(0, len(day_items), 2):
        row_buttons = [
            CallbackButton(text=f"📅 {d['title']}", payload=f"book:day:{d['day_key']}")
            for d in day_items[i : i + 2]
        ]
        builder.row(*row_buttons)

    if not day_items:
        builder.row(CallbackButton(text="Нет свободных дат", payload="noop"))

    builder.row(
        CallbackButton(text="◀️ К услугам", payload="book:services"),
        LinkButton(text="🌐 На сайт «Срез»", url=BOOKING_URL),
    )
    return builder.as_markup()


def get_slots_keyboard(slots: list[dict[str, Any]]) -> AttachmentButton:
    builder = InlineKeyboardBuilder()

    for i in range(0, len(slots), 3):
        row_buttons = [
            CallbackButton(text=f"🕒 {s['time']}", payload=f"book:slot:{s['timestamp']}")
            for s in slots[i : i + 3]
        ]
        builder.row(*row_buttons)

    if not slots:
        builder.row(CallbackButton(text="Нет свободных окон", payload="noop"))

    builder.row(
        CallbackButton(text="◀️ Другой день", payload="book:choose_day"),
        LinkButton(text="🌐 На сайт «Срез»", url=BOOKING_URL),
    )
    return builder.as_markup()
