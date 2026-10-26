import asyncio
import datetime
import logging
from maxapi import Bot
from maxapi.enums.parse_mode import TextFormat
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS
from services.appointments import (
    SARATOV_TZ,
    get_due_2h_reminders,
    get_due_confirmation_followup_reminders,
    get_due_evening_reminders,
    get_due_retention_reminders,
    mark_reminder_sent,
)

logger = logging.getLogger(__name__)


def _get_confirmation_keyboard(appointment_id: int) -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(
            text="✅ Подтверждаю запись",
            payload=f"appt:confirm:{appointment_id}",
        )
    )
    builder.row(
        CallbackButton(
            text="❌ Отменить запись",
            payload=f"appt:cancel:{appointment_id}",
        )
    )
    builder.row(
        LinkButton(
            text="📍 Как добраться",
            url="https://yandex.ru/maps/-/CDu~v-7x",
        )
    )
    return builder.as_markup()


def _get_confirmed_2h_keyboard(appointment_id: int) -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(
        LinkButton(
            text="📍 Как добраться",
            url="https://yandex.ru/maps/-/CDu~v-7x",
        )
    )
    builder.row(
        CallbackButton(
            text="❌ Отменить запись",
            payload=f"appt:cancel:{appointment_id}",
        )
    )
    return builder.as_markup()


def _get_retention_keyboard() -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(
            text="✂️ Записаться онлайн",
            payload="menu:booking",
        )
    )
    return builder.as_markup()


def _resolve_platform_user_id(appt: dict, platform: str) -> int | None:
    user_id = appt.get("platform_user_id")
    if user_id and user_id != 0:
        return user_id

    from database import get_connection
    with get_connection() as conn:
        col = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_user_id"}.get(platform)
        if not col:
            return None
        row = None
        if appt.get("user_id"):
            row = conn.execute(f"SELECT {col} FROM users WHERE id = %s", (appt["user_id"],)).fetchone()
        if not row and appt.get("phone"):
            row = conn.execute(f"SELECT {col} FROM users WHERE phone = %s", (appt["phone"],)).fetchone()

        if row and row[col]:
            found_id = row[col]
            conn.execute(
                "UPDATE appointments SET platform_user_id = %s, platform = %s WHERE id = %s",
                (found_id, platform, appt["id"]),
            )
            conn.commit()
            return found_id
    return None


async def run_reminder_worker(bot: Bot, platform: str = "max"):
    logger.info("Запущен фоновый воркер напоминаний (%s)", platform)

    while True:
        try:
            # 1. Evening reminder (day before visit, >= 18:00)
            evening_due = get_due_evening_reminders(platform=platform)
            for appt in evening_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if not target_user_id:
                    mark_reminder_sent(appt["id"], "evening")
                    continue

                dt = appt["appointment_time"]
                if isinstance(dt, str):
                    dt = datetime.datetime.fromisoformat(dt)
                dt_local = dt.astimezone(SARATOV_TZ) if dt.tzinfo else dt
                date_str = dt_local.strftime("%d.%m.%Y")
                time_str = dt_local.strftime("%H:%M")
                weekdays = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
                weekday_str = weekdays[dt_local.weekday()]

                text = (
                    "🔔 <b>Напоминание о записи к мастеру Борису Гафурову</b>\n\n"
                    f"Завтра в {weekday_str} (<b>{date_str}</b>) в <b>{time_str}</b> ждём тебя на стрижку!\n"
                    f"✂️ <b>Услуги:</b> {appt['services']}\n"
                    f"💰 <b>Стоимость:</b> {appt['total_price']} ₽\n"
                    "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
                    "👉 <b>Пожалуйста, подтверди свой визит</b> нажатием кнопки ниже или отмени запись, если твои планы изменились:"
                )
                try:
                    await bot.send_message(
                        user_id=target_user_id,
                        text=text,
                        attachments=[_get_confirmation_keyboard(appt["id"])],
                        format=TextFormat.HTML,
                    )
                    mark_reminder_sent(appt["id"], "evening")
                    logger.info("Отправлено вечернее напоминание клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки вечернего напоминания #%s: %s", appt["id"], send_err)

            # 2. Morning follow-up reminder (on visit day, >= 09:00, if not confirmed)
            morning_due = get_due_confirmation_followup_reminders(platform=platform)
            for appt in morning_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if not target_user_id:
                    mark_reminder_sent(appt["id"], "morning")
                    continue

                dt = appt["appointment_time"]
                if isinstance(dt, str):
                    dt = datetime.datetime.fromisoformat(dt)
                dt_local = dt.astimezone(SARATOV_TZ) if dt.tzinfo else dt
                time_str = dt_local.strftime("%H:%M")

                text = (
                    "⚠️ <b>Подтверждение записи на сегодня</b>\n\n"
                    f"Ты записан сегодня в <b>{time_str}</b>, но ещё <b>не подтвердил</b> свой визит.\n\n"
                    f"✂️ <b>Услуги:</b> {appt['services']}\n"
                    f"💰 <b>Стоимость:</b> {appt['total_price']} ₽\n"
                    "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
                    "👉 Пожалуйста, подтверди запись, чтобы мастер держал за тобой время, или отмени её, если не сможешь прийти:"
                )
                try:
                    await bot.send_message(
                        user_id=target_user_id,
                        text=text,
                        attachments=[_get_confirmation_keyboard(appt["id"])],
                        format=TextFormat.HTML,
                    )
                    mark_reminder_sent(appt["id"], "morning")
                    logger.info("Отправлено утреннее напоминание о подтверждении клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки утреннего напоминания #%s: %s", appt["id"], send_err)

            # 3. 2h express reminder
            two_h_due = get_due_2h_reminders(platform=platform)
            for appt in two_h_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if not target_user_id:
                    mark_reminder_sent(appt["id"], "2h")
                    continue

                dt = appt["appointment_time"]
                if isinstance(dt, str):
                    dt = datetime.datetime.fromisoformat(dt)
                dt_local = dt.astimezone(SARATOV_TZ) if dt.tzinfo else dt
                time_str = dt_local.strftime("%H:%M")

                is_confirmed = bool(appt.get("client_confirmed"))
                if is_confirmed:
                    text = (
                        "⏰ <b>До твоей записи осталось 2 часа!</b>\n\n"
                        f"Ждём тебя сегодня в <b>{time_str}</b>.\n"
                        f"✂️ <b>Услуги:</b> {appt['services']}\n"
                        "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
                        "<i>Твоя запись подтверждена, мастер готов и ждёт тебя! ✂️</i>"
                    )
                    attachments = [_get_confirmed_2h_keyboard(appt["id"])]
                else:
                    text = (
                        "⏰ <b>До твоей записи осталось 2 часа!</b>\n\n"
                        f"Ждём тебя сегодня в <b>{time_str}</b>.\n"
                        f"✂️ <b>Услуги:</b> {appt['services']}\n"
                        "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
                        "⚠️ <b>Внимание: запись всё ещё не подтверждена!</b>\n"
                        "Пожалуйста, подтверди визит или отмени запись, чтобы мастер не ждал впустую:"
                    )
                    attachments = [_get_confirmation_keyboard(appt["id"])]

                try:
                    await bot.send_message(
                        user_id=target_user_id,
                        text=text,
                        attachments=attachments,
                        format=TextFormat.HTML,
                    )
                    mark_reminder_sent(appt["id"], "2h")
                    logger.info("Отправлено 2ч напоминание клиенту #%s (appt #%s, confirmed=%s)", target_user_id, appt["id"], is_confirmed)
                except Exception as send_err:
                    logger.warning("Ошибка отправки 2ч напоминания #%s: %s", appt["id"], send_err)

            # 4. 2.5 weeks retention reminder
            retention_due = get_due_retention_reminders(platform=platform)
            for appt in retention_due:
                target_user_id = _resolve_platform_user_id(appt, platform)
                if not target_user_id:
                    mark_reminder_sent(appt["id"], "retention")
                    continue

                text = (
                    "✂️ <b>Привет! Прошло 2,5 недели с твоей стрижки</b>\n\n"
                    "Волосы уже отросли, и форма начинает теряться. Самое время освежить стрижку и бороду! 💈\n\n"
                    "Нажми кнопку ниже, чтобы занять удобное время на этой неделе:"
                )
                try:
                    await bot.send_message(
                        user_id=target_user_id,
                        text=text,
                        attachments=[_get_retention_keyboard()],
                        format=TextFormat.HTML,
                    )
                    mark_reminder_sent(appt["id"], "retention")
                    logger.info("Отправлено retention напоминание клиенту #%s (appt #%s)", target_user_id, appt["id"])
                except Exception as send_err:
                    logger.warning("Ошибка отправки retention напоминания #%s: %s", appt["id"], send_err)

        except Exception as e:
            logger.error("Ошибка в цикле reminder worker MAX: %s", e)

        await asyncio.sleep(60)
