from typing import Any
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import BOOKING_URL
from services.booking import SERVICES, calculate_services


def get_booking_keyboard() -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(LinkButton(text="🌐 Перейти на сайт", url=BOOKING_URL))
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
    builder.row(LinkButton(text="🌐 Перейти на сайт", url=BOOKING_URL))
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
        LinkButton(text="🌐 Перейти на сайт", url=BOOKING_URL),
    )
    return builder.as_markup()


PAGE_SLOTS_COUNT = 6


def get_slots_keyboard(
    slots: list[dict[str, Any]],
    page: int = 0,
    day_key: str = "",
) -> AttachmentButton:
    builder = InlineKeyboardBuilder()

    if not slots:
        builder.row(CallbackButton(text="Нет свободных окон", payload="noop"))
        builder.row(
            CallbackButton(text="◀️ Другой день", payload="book:choose_day"),
            LinkButton(text="🌐 Перейти на сайт", url=BOOKING_URL),
        )
        return builder.as_markup()

    total_slots = len(slots)
    total_pages = max(1, (total_slots + PAGE_SLOTS_COUNT - 1) // PAGE_SLOTS_COUNT)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * PAGE_SLOTS_COUNT
    end_idx = start_idx + PAGE_SLOTS_COUNT
    page_slots = slots[start_idx:end_idx]

    for i in range(0, len(page_slots), 3):
        row_buttons = [
            CallbackButton(text=f"🕒 {s['time']}", payload=f"book:slot:{s['timestamp']}")
            for s in page_slots[i : i + 3]
        ]
        builder.row(*row_buttons)

    has_prev = page > 0
    has_next = end_idx < total_slots
    if has_prev or has_next:
        nav_buttons = []
        if has_prev:
            nav_buttons.append(
                CallbackButton(
                    text="◀️ Ранее",
                    payload=f"book:slots_page:{day_key}:{page - 1}",
                )
            )
        if has_next:
            nav_buttons.append(
                CallbackButton(
                    text="Позже ▶️",
                    payload=f"book:slots_page:{day_key}:{page + 1}",
                )
            )
        builder.row(*nav_buttons)

    builder.row(
        CallbackButton(text="◀️ Другой день", payload="book:choose_day"),
        LinkButton(text="🌐 Перейти на сайт", url=BOOKING_URL),
    )
    return builder.as_markup()
