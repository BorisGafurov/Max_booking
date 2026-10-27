import datetime
import logging
from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.filters.command import Command
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS, BOOKING_URL
from services.appointments import (
    SARATOV_TZ,
    cancel_appointment,
    confirm_appointment_by_client,
    create_or_update_appointment,
)
from keyboards.booking import (
    get_booking_keyboard,
    get_days_keyboard,
    get_services_keyboard,
    get_slots_keyboard,
    get_sms_keyboard,
)
from services.masters import (
    SERVICES,
    calculate_services,
    confirm_sms_code,
    get_schedule,
    initiate_booking,
    user_pending_booking,
    user_selected_day,
    user_selected_services,
)
from services.referrals import get_user_phone, is_phone_number, link_user_phone, normalize_phone

logger = logging.getLogger(__name__)
router = Router(router_id="booking")
user_selected_slot: dict[int, int] = {}


def _format_services_text(selected_keys: list[str] | set[str]) -> str:
    calc = calculate_services(selected_keys)
    names_str = " + ".join(calc["names"])
    return (
        "✂️ <b>Онлайн-запись к мастеру Борису Гафурову</b>\n\n"
        "💡 <b>Как выбрать услуги:</b>\n"
        "• Нажми на услугу один раз — появится галочка ✅ (услуга выбрана)\n"
        "• Нажми ещё раз — галочка снимется ⚪️ (услуга удалена)\n"
        "<i>Можно выбрать одну услугу или сразу обе.</i>\n\n"
        f"📋 <b>Выбрано:</b> {names_str}\n"
        f"💰 <b>Итого:</b> {calc['total_price']} ₽ ({calc['duration_text']})\n\n"
        "<i>После выбора нажми кнопку «📅 Выбрать время»:</i>"
    )


@router.message_created(Command(["book", "booking"]))
@router.message_created(
    F.message.body.text.in_({"✂️ Записаться", "Записаться", "записаться"})
)
async def booking_message(event: MessageCreated) -> None:
    if not event.message or not event.message.sender:
        return

    user_id = event.message.sender.user_id
    user_selected_services[user_id] = {"hair"}

    await event.message.answer(
        text=_format_services_text(user_selected_services[user_id]),
        attachments=[get_services_keyboard(user_selected_services[user_id])],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload == "menu:booking")
@router.message_callback(F.callback.payload == "book:services")
async def booking_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return

    user_id = event.callback.user.user_id
    current = user_selected_services.get(user_id, {"hair"})

    try:
        await event.edit(
            text=_format_services_text(current),
            attachments=[get_services_keyboard(current)],
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing message: %s", e)
    await event.answer()


@router.message_callback(F.callback.payload.startswith("book:toggle:"))
async def toggle_service_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return

    user_id = event.callback.user.user_id
    payload = event.callback.payload or ""
    parts = payload.split(":")
    if len(parts) < 3:
        await event.answer()
        return
    key = parts[2]

    current = user_selected_services.get(user_id, {"hair"})
    alert_msg = None
    if key in current:
        if len(current) > 1:
            current.remove(key)
        else:
            alert_msg = "Нужно выбрать хотя бы одну услугу"
    else:
        current.add(key)
    user_selected_services[user_id] = current

    try:
        await event.edit(
            text=_format_services_text(current),
            attachments=[get_services_keyboard(current)],
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing message: %s", e)

    if alert_msg:
        await event.answer(notification=alert_msg)
    else:
        await event.answer()


@router.message_callback(F.callback.payload == "book:choose_day")
async def choose_day_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return

    user_id = event.callback.user.user_id
    current_services = user_selected_services.get(user_id, {"hair"})
    schedule = get_schedule(current_services)

    if not schedule:
        try:
            await event.edit(
                text=(
                    "⚠️ Не удалось загрузить расписание в реальном времени.\n\n"
                    "Ты можешь выбрать удобное время напрямую на сайте Masters:"
                ),
                attachments=[get_booking_keyboard()],
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.debug("Error editing message: %s", e)
        await event.answer()
        return

    calc = calculate_services(current_services)
    try:
        await event.edit(
            text=(
                f"📅 <b>Выбери удобный день записи:</b>\n\n"
                f"Услуги: <b>{', '.join(calc['names'])}</b>\n"
                f"Длительность: <b>{calc['duration_text']}</b>\n"
                f"Стоимость: <b>{calc['total_price']} ₽</b>"
            ),
            attachments=[get_days_keyboard(schedule)],
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing message: %s", e)
    await event.answer()


@router.message_callback(F.callback.payload.startswith("book:day:"))
async def choose_time_slot_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return

    user_id = event.callback.user.user_id
    payload = event.callback.payload or ""
    parts = payload.split(":")
    if len(parts) < 3:
        await event.answer()
        return
    day_key = parts[2]
    user_selected_day[user_id] = day_key

    current_services = user_selected_services.get(user_id, {"hair"})
    schedule = get_schedule(current_services)
    day_data = schedule.get(day_key)
    slots = day_data["slots"] if day_data else []
    title = day_data["title"] if day_data else day_key

    try:
        await event.edit(
            text=(
                f"🕒 <b>Свободные окна на {title}:</b>\n\n"
                "Выбери удобное время начала стрижки:"
            ),
            attachments=[get_slots_keyboard(slots)],
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing message: %s", e)
    await event.answer()


async def _finalize_successful_max_booking(
    bot,
    user_id: int,
    session_data: dict[str, Any],
    client_name: str,
    target_event_or_msg,
):
    user_pending_booking.pop(user_id, None)
    user_selected_slot.pop(user_id, None)
    calc = session_data["calc"]
    dt = datetime.datetime.fromtimestamp(session_data["timestamp"], tz=SARATOV_TZ)
    phone = session_data["phone"]

    appt_id = create_or_update_appointment(
        phone=phone,
        appointment_time=dt,
        services=", ".join(calc["names"]),
        total_price=calc["total_price"],
        client_name=client_name,
        platform="max",
        platform_user_id=user_id,
        masters_id=session_data.get("appointment_id"),
    )

    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="❌ Отменить запись", payload=f"appt:cancel:{appt_id}"))

    text = (
        f"🎉 <b>Запись успешно подтверждена!</b>\n\n"
        f"✂️ <b>Услуги:</b> {', '.join(calc['names'])}\n"
        f"💰 <b>Стоимость:</b> {calc['total_price']} ₽ ({calc['duration_text']})\n"
        f"📅 <b>Дата и время:</b> {dt.strftime('%d.%m.%Y в %H:%M')}\n"
        f"📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110\n\n"
        "Ждём тебя! Напомним накануне вечером и за 2 часа до визита."
    )

    if isinstance(target_event_or_msg, MessageCallback):
        try:
            await target_event_or_msg.edit(
                text=text,
                attachments=[builder.as_markup()],
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.debug("Error editing message: %s", e)
        await target_event_or_msg.answer()
    else:
        msg_obj = getattr(target_event_or_msg, "message", target_event_or_msg)
        await msg_obj.answer(
            text=text,
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                user_id=admin_id,
                text=(
                    "🔥 <b>Новая онлайн-запись через бота MAX!</b>\n\n"
                    f"👤 <b>Клиент:</b> {client_name}\n"
                    f"📱 <b>Телефон:</b> {phone}\n"
                    f"✂️ <b>Услуги:</b> {', '.join(calc['names'])}\n"
                    f"📅 <b>Время:</b> {dt.strftime('%d.%m.%Y в %H:%M')}\n"
                    f"💰 <b>Сумма:</b> {calc['total_price']} ₽"
                ),
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.error("Failed to notify MAX admin %s: %s", admin_id, e)


@router.message_callback(F.callback.payload.startswith("book:slot:"))
async def initiate_slot_booking_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return

    user_id = event.callback.user.user_id
    payload = event.callback.payload or ""
    parts = payload.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await event.answer()
        return
    timestamp = int(parts[2])
    user_selected_slot[user_id] = timestamp
    phone = get_user_phone(user_id, platform="max")

    if not phone:
        try:
            await event.edit(
                text=(
                    "📱 Отправь свой номер телефона сообщением (например, <code>+79991234567</code>) "
                    "перед бронированием:"
                ),
                attachments=[get_booking_keyboard()],
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.debug("Error editing message: %s", e)
        await event.answer()
        return

    try:
        await event.edit(
            text="⏳ Бронирую слот в Masters...",
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing message: %s", e)

    current_services = user_selected_services.get(user_id, {"hair"})
    client_name = event.callback.user.first_name or "Клиент"
    if event.callback.user.last_name:
        client_name = f"{client_name} {event.callback.user.last_name}"

    success, msg, session_data = initiate_booking(phone, current_services, timestamp, client_name=client_name)

    if not success or not session_data:
        try:
            await event.edit(
                text=f"⚠️ {msg}\n\nТы можешь завершить бронирование напрямую через сайт:",
                attachments=[get_booking_keyboard()],
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.debug("Error editing message: %s", e)
        await event.answer()
        return

    bot = event._ensure_bot()
    if not session_data.get("needs_sms", True):
        await _finalize_successful_max_booking(bot, user_id, session_data, client_name, event)
        return

    user_pending_booking[user_id] = session_data
    dt = datetime.datetime.fromtimestamp(timestamp, tz=SARATOV_TZ)

    try:
        await event.edit(
            text=(
                f"📩 <b>Подтверждение записи</b>\n\n"
                f"Выбрано: <b>{dt.strftime('%d.%m.%Y в %H:%M')}</b>\n"
                f"На твой номер <code>{phone}</code> отправлено СМС-сообщение с кодом от Masters.\n\n"
                "👉 <b>Напиши 4 цифры из СМС в ответном сообщении:</b>"
            ),
            attachments=[get_sms_keyboard()],
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing message: %s", e)
    await event.answer()


@router.message_callback(F.callback.payload == "book:cancel")
async def cancel_booking_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user_id = event.callback.user.user_id
    user_pending_booking.pop(user_id, None)
    user_selected_slot.pop(user_id, None)

    try:
        await event.edit(text="Запись отменена.", format=TextFormat.HTML)
    except Exception as e:
        logger.debug("Error editing message: %s", e)
    await event.answer("Запись отменена.")


@router.message_created(
    lambda event: bool(
        event.message
        and event.message.sender
        and event.message.sender.user_id in user_pending_booking
        and event.message.body
        and event.message.body.text
        and event.message.body.text.strip().isdigit()
        and len(event.message.body.text.strip()) == 4
    )
)
async def handle_sms_code(event: MessageCreated) -> None:
    if not event.message or not event.message.body or not event.message.body.text or not event.message.sender:
        return

    user_id = event.message.sender.user_id
    session_data = user_pending_booking.get(user_id)

    if not session_data:
        return  # Regular 4-digit message, not waiting for booking code

    code = event.message.body.text.strip()
    success, msg = confirm_sms_code(session_data, code)

    if not success:
        await event.message.answer(
            text=f"❌ <b>{msg}</b>\n\nПопробуй ввести 4 цифры ещё раз или заверши запись на сайте:",
            attachments=[get_sms_keyboard()],
            format=TextFormat.HTML,
        )
        return

    # Successfully booked!
    client_name = event.message.sender.first_name or "Клиент"
    if event.message.sender.last_name:
        client_name = f"{client_name} {event.message.sender.last_name}"

    bot = event._ensure_bot()
    await _finalize_successful_max_booking(bot, user_id, session_data, client_name, event)


@router.message_callback(F.callback.payload.startswith("appt:confirm:"))
async def handle_confirm_appointment(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await event.answer(notification="Некорректная запись")
        return

    appt_id = int(parts[2])
    success, msg, appt = confirm_appointment_by_client(appt_id)

    if not success:
        await event.answer(notification=msg)
        return

    dt_str = ""
    if appt and appt.get("appointment_time"):
        dt = appt["appointment_time"]
        if isinstance(dt, str):
            dt = datetime.datetime.fromisoformat(dt)
        dt_local = dt.astimezone(SARATOV_TZ) if dt.tzinfo else dt
        dt_str = dt_local.strftime("%d.%m.%Y в %H:%M")

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
            payload=f"appt:cancel:{appt_id}",
        )
    )

    text_msg = (
        "✅ <b>Запись подтверждена!</b>\n\n"
        f"Спасибо за подтверждение! Ждём тебя <b>{dt_str}</b> ✂️\n"
        "📍 <b>Адрес:</b> Саратов, ул. имени Е.И. Пугачёва, 110"
    )
    try:
        await event.edit(
            text=text_msg,
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing confirmed appt msg: %s", e)
        if event.message:
            try:
                await event.message.answer(
                    text=text_msg,
                    attachments=[builder.as_markup()],
                    format=TextFormat.HTML,
                )
            except Exception:
                pass

    await event.answer(notification="Запись подтверждена! Ждём тебя.")

    # Notify master
    if appt:
        bot = event._ensure_bot()
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    user_id=admin_id,
                    text=(
                        "✅ <b>Клиент подтвердил запись (MAX)!</b>\n\n"
                        f"👤 <b>Клиент:</b> {appt.get('client_name') or 'Клиент'}\n"
                        f"📱 <b>Телефон:</b> {appt.get('phone')}\n"
                        f"✂️ <b>Услуги:</b> {appt.get('services')}\n"
                        f"📅 <b>Время:</b> {dt_str}\n"
                        "Клиент точно придёт к назначенному времени."
                    ),
                    format=TextFormat.HTML,
                )
            except Exception as e:
                logger.error("Failed to notify admin %s: %s", admin_id, e)


@router.message_callback(F.callback.payload.startswith("appt:cancel:"))
async def handle_cancel_appointment(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await event.answer(notification="Некорректная запись")
        return

    appt_id = int(parts[2])
    success, msg, appt = cancel_appointment(appt_id)

    if not success:
        await event.answer(notification=msg)
        return

    try:
        await event.edit(
            text=(
                "❌ <b>Запись успешно отменена</b>\n\n"
                "Будем рады видеть в другой раз! Чтобы выбрать новую дату, нажми «✂️ Записаться»."
            ),
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.debug("Error editing cancelled appt msg: %s", e)

    await event.answer(notification="Запись отменена.")

    if appt:
        bot = event._ensure_bot()
        dt_str = str(appt.get("appointment_time", ""))
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    user_id=admin_id,
                    text=(
                        "⚠️ <b>Клиент отменил запись (MAX)!</b>\n\n"
                        f"👤 <b>Клиент:</b> {appt.get('client_name') or 'Клиент'}\n"
                        f"📱 <b>Телефон:</b> {appt.get('phone')}\n"
                        f"✂️ <b>Услуги:</b> {appt.get('services')}\n"
                        f"📅 <b>Время:</b> {dt_str}\n"
                        "Слот снова свободен для других клиентов."
                    ),
                    format=TextFormat.HTML,
                )
            except Exception as e:
                logger.error("Failed to notify admin %s: %s", admin_id, e)


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

    from handlers.referrals import _has_active_max_draft
    if _has_active_max_draft(event):
        return

    user_id = event.message.sender.user_id
    text = event.message.body.text
    success, _ = link_user_phone(user_id, text, platform="max")
    if not success:
        await event.message.answer(
            text="Пожалуйста, введи корректный номер телефона (например, +79991234567).",
            format=TextFormat.HTML,
        )
        return

    pending_ts = user_selected_slot.pop(user_id, None)
    if pending_ts:
        eff_phone = get_user_phone(user_id, platform="max") or normalize_phone(text)
        current_services = user_selected_services.get(user_id, {"hair"})
        dt = datetime.datetime.fromtimestamp(pending_ts, tz=SARATOV_TZ)

        await event.message.answer(
            text=f"⏳ Номер <code>{eff_phone}</code> сохранён. Связываюсь с Masters для записи на <b>{dt.strftime('%d.%m.%Y в %H:%M')}</b>...",
            format=TextFormat.HTML,
        )
        client_name = event.message.sender.first_name or "Клиент"
        if event.message.sender.last_name:
            client_name = f"{client_name} {event.message.sender.last_name}"

        success_init, msg_init, session_data = initiate_booking(eff_phone, current_services, pending_ts, client_name=client_name)
        if not success_init or not session_data:
            await event.message.answer(
                text=f"⚠️ {msg_init}\n\nТы можешь завершить бронирование напрямую через сайт:",
                attachments=[get_booking_keyboard()],
                format=TextFormat.HTML,
            )
            return

        bot = event._ensure_bot()
        if not session_data.get("needs_sms", True):
            await _finalize_successful_max_booking(bot, user_id, session_data, client_name, event)
            return

        user_pending_booking[user_id] = session_data
        await event.message.answer(
            text=(
                f"📩 <b>Подтверждение записи</b>\n\n"
                f"Выбрано: <b>{dt.strftime('%d.%m.%Y в %H:%M')}</b>\n"
                f"На твой номер <code>{eff_phone}</code> отправлено СМС-сообщение с кодом от Masters.\n\n"
                "👉 <b>Напиши 4 цифры из СМС в ответном сообщении:</b>"
            ),
            attachments=[get_sms_keyboard()],
            format=TextFormat.HTML,
        )
        return

    user_selected_services[user_id] = {"hair"}
    await event.message.answer(
        text="✅ <b>Номер телефона успешно сохранен!</b>\n\n"
             "Теперь твои бонусы и приглашённые друзья объединены в едином профиле.\n\n"
             "Выбери нужные услуги для записи:",
        attachments=[get_services_keyboard(user_selected_services[user_id])],
        format=TextFormat.HTML,
    )
