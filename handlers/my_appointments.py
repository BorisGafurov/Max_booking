"""Client-facing "My appointments" section (MAX)."""

import asyncio
import datetime
import html
import logging

from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS, SALON_ADDRESS
from services.appointments import (
    SARATOV_TZ,
    get_user_upcoming_appointments,
    reschedule_appointment,
)
from services.booking import get_schedule
from services.reviews import get_reviewable_for_platform
from handlers.ui import edit_or_send as _edit_or_send

logger = logging.getLogger(__name__)
router = Router(router_id="my_appointments")

# MAX user id -> appointment id pending reschedule
user_resched_appt: dict[int, int] = {}


def _fmt_dt(dt_val) -> str:
    if isinstance(dt_val, str):
        try:
            dt_val = datetime.datetime.fromisoformat(dt_val)
        except Exception:
            return dt_val
    if dt_val.tzinfo is None:
        dt_val = dt_val.replace(tzinfo=SARATOV_TZ)
    return dt_val.astimezone(SARATOV_TZ).strftime("%d.%m.%Y в %H:%M")


def _my_appts_keyboard(appts: list[dict], reviewable: list[dict] | None = None):
    builder = InlineKeyboardBuilder()
    if reviewable:
        for r in reviewable:
            builder.row(CallbackButton(
                text=f"⭐ Оставить отзыв ({_fmt_dt(r['appointment_time'])})",
                payload=f"review:start:{r['id']}",
            ))
    for a in appts:
        builder.row(CallbackButton(
            text=f"✏️ Перенести ({_fmt_dt(a['appointment_time'])})",
            payload=f"my:resched:{a['id']}",
        ))
        builder.row(CallbackButton(text="❌ Отменить запись", payload=f"appt:cancel:{a['id']}"))
    if not appts and not reviewable:
        builder.row(CallbackButton(text="✂️ Записаться", payload="menu:booking"))
    return builder.as_markup()


def _appts_text(appts: list[dict], reviewable: list[dict] | None = None) -> str:
    parts: list[str] = []
    if reviewable:
        rev_lines = [
            f"• <b>{html.escape(str(r.get('services') or 'Визит'))}</b> ({_fmt_dt(r['appointment_time'])})"
            for r in reviewable[:3]
        ]
        parts.append(
            "⭐ <b>Ожидает твоего отзыва:</b>\n"
            + "\n".join(rev_lines)
            + "\n<i>Нажми кнопку ниже, чтобы оценить визит 👇</i>"
        )

    if appts:
        blocks = []
        for a in appts:
            blocks.append(
                f"#{a['id']}. 📅 <b>{_fmt_dt(a['appointment_time'])}</b>\n"
                f"✂️ {html.escape(str(a.get('services') or 'Стрижка'))} — {a.get('total_price') or 0} ₽\n"
                f"📍 {html.escape(SALON_ADDRESS)}"
            )
        parts.append("📋 <b>Твои активные записи:</b>\n\n" + "\n\n".join(blocks))
    elif not reviewable:
        parts.append(
            "📋 <b>У тебя нет активных записей.</b>\n\n"
            "Запишись на удобное время — и запись появится здесь."
        )

    return "\n\n".join(parts)


async def _show_my_appointments(event_target, user_id: int, edit_event: MessageCallback | None = None):
    appts = await asyncio.to_thread(get_user_upcoming_appointments, "max", user_id)
    reviewable = await asyncio.to_thread(get_reviewable_for_platform, "max", user_id)
    text = _appts_text(appts, reviewable)
    kb = _my_appts_keyboard(appts, reviewable)

    if edit_event is not None:
        try:
            await edit_event.edit(text=text, attachments=[kb], format=TextFormat.HTML)
        except Exception as e:
            logger.debug("Error editing my appointments msg: %s", e)
            try:
                await event_target.answer(
                    text=text, attachments=[kb], format=TextFormat.HTML
                )
            except Exception as e2:
                logger.debug("Error sending my appointments msg: %s", e2)
        await edit_event.answer()
    else:
        await event_target.answer(text=text, attachments=[kb], format=TextFormat.HTML)


@router.message_created(
    F.message.body.text.in_({"📋 Мои записи", "Мои записи", "мои записи"})
)
async def my_appointments_message(event: MessageCreated) -> None:
    if not event.message or not event.message.sender:
        return
    await _show_my_appointments(event, event.message.sender.user_id)


@router.message_callback(F.callback.payload == "menu:my_appts")
@router.message_callback(F.callback.payload == "my:list")
async def my_appointments_callback(event: MessageCallback) -> None:
    user = event.callback.user if event.callback else None
    if not user:
        await event.answer()
        return
    await _show_my_appointments(event, user.user_id, edit_event=event)


@router.message_callback(F.callback.payload.startswith("my:resched:"))
async def my_resched_start(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return

    user_id = event.callback.user.user_id
    parts = (event.callback.payload or "").split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await event.answer(notification="Некорректная запись")
        return
    user_resched_appt[user_id] = int(parts[2])

    schedule = await asyncio.to_thread(get_schedule, {"hair"})
    builder = InlineKeyboardBuilder()
    day_items = list(schedule.values())[:8]
    for d in day_items:
        builder.row(CallbackButton(text=f"📅 {d['title']}", payload=f"my:resched_day:{d['day_key']}"))
    builder.row(CallbackButton(text="◀️ Мои записи", payload="my:list"))

    try:
        await _edit_or_send(event, 
            text="🗓 <b>Перенос записи</b>\n\nВыбери новый день:",
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing reschedule start: %s", e)
    await event.answer()


@router.message_callback(F.callback.payload.startswith("my:resched_day:"))
async def my_resched_day(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return

    parts = (event.callback.payload or "").split(":")
    day_key = parts[2] if len(parts) > 2 else ""

    schedule = await asyncio.to_thread(get_schedule, {"hair"})
    day_data = schedule.get(day_key)
    slots = day_data["slots"] if day_data else []
    title = day_data["title"] if day_data else day_key

    builder = InlineKeyboardBuilder()
    for s in slots:
        builder.row(CallbackButton(text=f"🕒 {s['time']}", payload=f"my:resched_slot:{s['timestamp']}"))
    builder.row(CallbackButton(text="◀️ Другой день", payload="my:list"))

    try:
        await _edit_or_send(event, 
            text=f"🕒 <b>Свободные окна на {title}:</b>\n\nВыбери новое время:",
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing reschedule day: %s", e)
    await event.answer()


@router.message_callback(F.callback.payload.startswith("my:resched_slot:"))
async def my_resched_slot(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return

    user_id = event.callback.user.user_id
    appt_id = user_resched_appt.get(user_id)
    if not appt_id:
        await event.answer(notification="Сессия переноса устарела, начни заново.")
        return

    parts = (event.callback.payload or "").split(":")
    ts = int(parts[2])
    new_dt = datetime.datetime.fromtimestamp(ts, tz=SARATOV_TZ)

    ok, msg, appt = await asyncio.to_thread(
        lambda: reschedule_appointment(appt_id, new_dt, platform="max", platform_user_id=user_id)
    )
    user_resched_appt.pop(user_id, None)

    if not ok:
        try:
            await _edit_or_send(event, text=f"⚠️ {html.escape(msg)}", format=TextFormat.HTML)
        except Exception as e:
            logger.debug("Error editing reschedule error: %s", e)
        await event.answer()
        return

    text = (
        "✅ <b>Запись перенесена!</b>\n\n"
        f"📅 <b>{_fmt_dt(appt['appointment_time'])}</b>\n"
        f"✂️ {html.escape(str(appt.get('services') or 'Стрижка'))} — {appt.get('total_price') or 0} ₽\n"
        f"📍 {html.escape(SALON_ADDRESS)}\n\n"
        "Напомним накануне вечером и за 2 часа до визита."
    )
    try:
        await _edit_or_send(event, 
            text=text,
            attachments=[_my_appts_keyboard([appt])],
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing reschedule success: %s", e)
    await event.answer(notification="Запись перенесена!")

    bot = event._ensure_bot()
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                user_id=admin_id,
                text=(
                    "🗓 <b>Клиент перенёс запись (MAX)!</b>\n\n"
                    f"👤 <b>Клиент:</b> {appt.get('client_name') or 'Клиент'}\n"
                    f"📱 <b>Телефон:</b> {appt.get('phone') or '—'}\n"
                    f"✂️ <b>Услуги:</b> {appt.get('services') or ''}\n"
                    f"⏰ <b>Новое время:</b> {_fmt_dt(appt['appointment_time'])}"
                ),
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.error("Failed to notify MAX admin %s about reschedule: %s", admin_id, e)
