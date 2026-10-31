"""Client review handlers for MAX bot."""

import html
import logging
from typing import Any

from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from handlers.ui import edit_or_send as _edit_or_send

from services.reviews import (
    can_review_platform_appointment,
    create_review_from_platform,
    get_reviewable_for_platform,
)

logger = logging.getLogger(__name__)
router = Router(router_id="reviews")

# State for client awaiting review text input: max_user_id -> {"appointment_id": int, "rating": int}
user_pending_review: dict[int, dict[str, Any]] = {}


def get_rating_keyboard(appointment_id: int):
    builder = InlineKeyboardBuilder()
    row_stars = [
        CallbackButton(text=f"⭐ {n}", payload=f"review:rate:{appointment_id}:{n}")
        for n in range(1, 6)
    ]
    builder.row(*row_stars)
    builder.row(CallbackButton(text="❌ Отмена", payload="review:cancel"))
    return builder.as_markup()


def get_cancel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="❌ Отменить написание отзыва", payload="review:cancel"))
    return builder.as_markup()


@router.message_callback(F.callback.payload.startswith("review:start:"))
async def handle_max_review_start(event: MessageCallback) -> None:
    user = event.callback.user if event.callback else None
    if not user:
        await event.answer()
        return

    parts = (event.callback.payload or "").split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await event.answer(notification="Ошибка параметров")
        return

    appt_id = int(parts[2])
    can, msg, appt = can_review_platform_appointment("max", user.user_id, appt_id)
    if not can:
        await event.answer(notification=msg or "Отзыв недоступен")
        return

    services = (appt.get("services") or "Стрижка") if appt else "Визит"
    text = (
        "✂️ <b>Оставить отзыв о визите</b>\n\n"
        f"Услуга: <b>{html.escape(str(services))}</b>\n\n"
        "Пожалуйста, выбери оценку от 1 до 5 звёзд:"
    )
    await _edit_or_send(
        event,
        text=text,
        attachments=[get_rating_keyboard(appt_id)],
        format=TextFormat.HTML,
    )
    await event.answer()


@router.message_callback(F.callback.payload.startswith("review:rate:"))
async def handle_max_review_rate(event: MessageCallback) -> None:
    user = event.callback.user if event.callback else None
    if not user:
        await event.answer()
        return

    parts = (event.callback.payload or "").split(":")
    if len(parts) < 4 or not parts[2].isdigit() or not parts[3].isdigit():
        await event.answer(notification="Ошибка параметров")
        return

    appt_id = int(parts[2])
    rating = int(parts[3])

    can, msg, _ = can_review_platform_appointment("max", user.user_id, appt_id)
    if not can:
        await event.answer(notification=msg or "Отзыв недоступен")
        return

    user_pending_review[user.user_id] = {"appointment_id": appt_id, "rating": rating}

    stars_str = "⭐" * rating
    text = (
        f"Ты выбрал оценку: <b>{rating} из 5</b> ({stars_str})\n\n"
        "✍️ <b>Напиши, пожалуйста, текстовый отзыв:</b>\n"
        "<i>(от 10 до 500 символов — что понравилось, качество стрижки, сервис и атмосфера)</i>\n\n"
        "<i>Для отмены напиши /cancel или нажми кнопку ниже 👇</i>"
    )
    await _edit_or_send(
        event,
        text=text,
        attachments=[get_cancel_keyboard()],
        format=TextFormat.HTML,
    )
    await event.answer()


@router.message_callback(F.callback.payload == "review:cancel")
async def handle_max_review_cancel(event: MessageCallback) -> None:
    user = event.callback.user if event.callback else None
    if user:
        user_pending_review.pop(user.user_id, None)
    await _edit_or_send(
        event,
        text="Отзыв отменён. Ты сможешь оставить его в любое время в разделе «📋 Мои записи».",
        format=TextFormat.HTML,
    )
    await event.answer(notification="Отменено")


@router.message_created()
async def handle_max_review_comment(event: MessageCreated) -> None:
    if not event.message or not event.message.sender or not event.message.body:
        return

    user_id = event.message.sender.user_id
    pending = user_pending_review.get(user_id)
    if not pending:
        # Not awaiting review comment; let other routers/fallback process
        return

    text = (event.message.body.text or "").strip()
    if text in {"/cancel", "отмена", "Отмена"}:
        user_pending_review.pop(user_id, None)
        await event.message.answer(
            text="Отзыв отменён. Ты сможешь оставить его в любое время в разделе «📋 Мои записи».",
            format=TextFormat.HTML,
        )
        return

    appt_id = pending["appointment_id"]
    rating = pending["rating"]

    ok, err_or_msg, created = create_review_from_platform(
        platform="max",
        platform_user_id=user_id,
        appointment_id=appt_id,
        rating=rating,
        comment=text,
    )

    if not ok:
        await event.message.answer(
            text=(
                f"❌ <b>{html.escape(err_or_msg)}</b>\n\n"
                "Пожалуйста, напиши отзыв ещё раз или отправь /cancel для отмены:"
            ),
            attachments=[get_cancel_keyboard()],
            format=TextFormat.HTML,
        )
        return

    user_pending_review.pop(user_id, None)
    await event.message.answer(
        text=(
            "🎉 <b>Спасибо за твой отзыв!</b>\n\n"
            "Он успешно сохранён и опубликован на нашем сайте <b>srezforma.ru</b> в разделе «Отзывы».\n\n"
            "Будем рады видеть тебя снова! 💈"
        ),
        format=TextFormat.HTML,
    )
