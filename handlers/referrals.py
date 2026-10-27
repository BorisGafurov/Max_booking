import asyncio
import datetime
import html
import logging
import re
from zoneinfo import ZoneInfo
from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.filters.command import Command
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS, REFERRAL_REWARD_TEXT
from services.appointments import (
    SARATOV_TZ,
    adjust_master_day_hours,
    cancel_appointment,
    complete_appointment,
    confirm_appointment_by_client,
    create_manual_appointment,
    get_appointment,
    get_appointments_counts,
    get_current_saratov_time,
    get_day_time_slots_status,
    get_master_appointments,
    get_master_day_schedule,
    get_upcoming_days_schedule,
    reschedule_appointment,
    set_master_day_schedule,
    update_appointment_price_and_service,
)
from services.client_import import (
    get_clients_page,
    get_clients_stats,
    import_clients_from_file,
    import_clients_from_text,
    update_appointment_client_details,
)
from services.masters import get_service_options_for_creation
from services.masters_sync import (
    confirm_master_login,
    disconnect_masters_session,
    get_active_masters_session,
    pending_master_logins,
    request_master_login,
    sync_masters_appointments,
)
from services.referrals import (
    build_referral_link,
    get_active_rewards,
    get_broadcast_recipients,
    get_pending_referrals,
    get_referral_stats,
    get_user_phone,
    is_admin_user,
    is_master_user,
    is_phone_number,
    mark_referral_visited,
    mark_reward_used,
    normalize_phone,
    save_user,
)

logger = logging.getLogger(__name__)
router = Router(router_id="referrals")

PAGE_SIZE = 8
admin_broadcast_drafts: dict[int, dict] = {}
admin_create_drafts: dict[int, dict] = {}
admin_resched_drafts: dict[int, dict] = {}
admin_edit_client_drafts: dict[int, dict] = {}
admin_edit_price_drafts: dict[int, dict] = {}
admin_clients_state: dict[int, dict] = {}

RU_MONTHS = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def is_admin(user_id: int) -> bool:
    return is_master_user(user_id, platform="max")


def parse_to_saratov(val) -> datetime.datetime:
    if isinstance(val, datetime.datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=SARATOV_TZ)
        return val.astimezone(SARATOV_TZ)
    if isinstance(val, str):
        val_clean = val.replace("T", " ")
        if "." in val_clean:
            val_clean = val_clean.split(".")[0]
        try:
            dt = datetime.datetime.strptime(val_clean[:19], "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=SARATOV_TZ)
        except Exception:
            pass
    return get_current_saratov_time()


def format_dt(val) -> str:
    dt = parse_to_saratov(val)
    return f"{dt.day} {RU_MONTHS[dt.month]} в {dt.strftime('%H:%M')}"


def admin_menu_keyboard() -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="📋 Календарь записей", payload="admin:appts:all:0"))
    builder.row(CallbackButton(text="➕ Новая запись мастера", payload="admin:create:start"))
    builder.row(CallbackButton(text="👥 База клиентов", payload="admin:clients:0"))
    builder.row(CallbackButton(text="🕒 Рабочий график", payload="admin:sched:0"))
    builder.row(CallbackButton(text="⏳ Ожидают подтверждения", payload="admin:pending:0"))
    builder.row(CallbackButton(text="💰 Активные бонусы", payload="admin:rewards:0"))
    builder.row(CallbackButton(text="🔄 Синхронизация Masters", payload="admin:masters"))
    builder.row(CallbackButton(text="📢 Сделать рассылку", payload="admin:broadcast"))
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))
    return builder.as_markup()


def admin_appts_keyboard(
    appts: list[dict],
    current_filter: str,
    offset: int,
    total: int,
    counts: dict[str, int] | None = None,
) -> AttachmentButton:
    builder = InlineKeyboardBuilder()

    if counts is None:
        try:
            counts = get_appointments_counts()
        except Exception:
            counts = {"today": 0, "tomorrow": 0, "pending": 0, "all": total}

    filters = [
        ("today", f"Сегодня ({counts.get('today', 0)})"),
        ("tomorrow", f"Завтра ({counts.get('tomorrow', 0)})"),
        ("pending", f"Ожидающие ({counts.get('pending', 0)})"),
        ("all", f"Все ({counts.get('all', 0)})"),
    ]
    f_btns = []
    for f_key, f_title in filters:
        title = f"• {f_title} •" if f_key == current_filter else f_title
        f_btns.append(CallbackButton(text=title, payload=f"admin:appts:{f_key}:0"))
    builder.row(*f_btns)

    for item in appts:
        dt = parse_to_saratov(item["appointment_time"])
        time_str = dt.strftime("%H:%M")
        if current_filter == "all":
            time_str = f"{dt.day} {RU_MONTHS[dt.month]} {time_str}"

        st = item.get("status", "confirmed")
        if st == "completed":
            badge = "✅"
        elif item.get("client_confirmed"):
            badge = "🟢"
        else:
            badge = "⏳"

        name = (item.get("client_name") or "Клиент")[:12]
        price = item.get("total_price") or 0
        btn_text = f"{badge} {time_str} · {name} ({price} ₽)"
        builder.row(CallbackButton(text=btn_text, payload=f"admin:appt:{item['id']}:{current_filter}:{offset}"))

    if not appts:
        builder.row(CallbackButton(text="Нет записей в этой категории", payload="noop"))

    nav_btns = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav_btns.append(CallbackButton(text="◀️ Назад", payload=f"admin:appts:{current_filter}:{prev_offset}"))
    if offset + PAGE_SIZE < total:
        next_offset = offset + PAGE_SIZE
        nav_btns.append(CallbackButton(text="Вперёд ▶️", payload=f"admin:appts:{current_filter}:{next_offset}"))
    if nav_btns:
        builder.row(*nav_btns)

    builder.row(
        CallbackButton(text="➕ Новая запись", payload="admin:create:start"),
        CallbackButton(text="⬅️ Меню админки", payload="admin:menu"),
    )
    return builder.as_markup()


def admin_appt_card_keyboard(appt: dict, current_filter: str, offset: int) -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    appt_id = appt["id"]
    status = appt.get("status", "confirmed")

    if status != "completed" and status != "cancelled":
        builder.row(CallbackButton(text="✂️ Пострижен (Завершить)", payload=f"admin:complete_appt:{appt_id}:{current_filter}:{offset}"))
        if not appt.get("client_confirmed"):
            builder.row(CallbackButton(text="✅ Подтвердить запись", payload=f"admin:confirm_appt:{appt_id}:{current_filter}:{offset}"))
        builder.row(
            CallbackButton(text="✏️ Изменить клиента (имя / тел.)", payload=f"admin:edit_client:{appt_id}:{current_filter}:{offset}"),
            CallbackButton(text="💰 Цена / услуга", payload=f"admin:edit_price:{appt_id}:{current_filter}:{offset}"),
        )
        builder.row(
            CallbackButton(text="🗓 Перенести", payload=f"admin:resched_start:{appt_id}:{current_filter}:{offset}"),
            CallbackButton(text="❌ Отменить", payload=f"admin:cancel_appt:{appt_id}:{current_filter}:{offset}"),
        )
    else:
        builder.row(
            CallbackButton(text="✏️ Изменить клиента (имя / тел.)", payload=f"admin:edit_client:{appt_id}:{current_filter}:{offset}"),
            CallbackButton(text="💰 Цена / услуга", payload=f"admin:edit_price:{appt_id}:{current_filter}:{offset}"),
        )

    builder.row(CallbackButton(text="⬅️ К списку записей", payload=f"admin:appts:{current_filter}:{offset}"))
    return builder.as_markup()


def admin_schedule_keyboard(days: list[dict], offset: int, total: int) -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    for d in days:
        status_txt = f"🟢 {d['start_time']} - {d['end_time']}" if d["is_working"] else "🔴 Выходной"
        builder.row(CallbackButton(text=f"{d['title']}: {status_txt}", payload=f"admin:sched_day:{d['day_key']}"))

    nav_btns = []
    if offset > 0:
        nav_btns.append(CallbackButton(text="◀️ Назад", payload=f"admin:sched:{max(0, offset - 7)}"))
    if offset + 7 < total:
        nav_btns.append(CallbackButton(text="Вперёд ▶️", payload=f"admin:sched:{offset + 7}"))
    if nav_btns:
        builder.row(*nav_btns)

    builder.row(CallbackButton(text="⬅️ Главное меню", payload="admin:menu"))
    return builder.as_markup()


def admin_sched_day_keyboard(sched: dict) -> AttachmentButton:
    day_key = sched["day_key"]
    builder = InlineKeyboardBuilder()
    if sched["is_working"]:
        builder.row(CallbackButton(text="🔴 Сделать выходным днем", payload=f"admin:sched_toggle:{day_key}"))
        builder.row(
            CallbackButton(text="➖ Начало -1ч", payload=f"admin:sched_adj:{day_key}:start:-1"),
            CallbackButton(text="➕ Начало +1ч", payload=f"admin:sched_adj:{day_key}:start:1"),
        )
        builder.row(
            CallbackButton(text="➖ Конец -1ч", payload=f"admin:sched_adj:{day_key}:end:-1"),
            CallbackButton(text="➕ Конец +1ч", payload=f"admin:sched_adj:{day_key}:end:1"),
        )
    else:
        builder.row(CallbackButton(text="🟢 Сделать рабочим днем (10:00 - 20:00)", payload=f"admin:sched_toggle:{day_key}"))

    builder.row(CallbackButton(text="⬅️ К календарю графика", payload="admin:sched:0"))
    return builder.as_markup()


def pending_referrals_keyboard(referrals: list[dict], offset: int, total: int) -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    for item in referrals:
        invited_name = (item["invited_first_name"] or "Без имени")[:15]
        referrer_name = (item["referrer_first_name"] or "Без имени")[:15]
        text = f"✅ #{item['id']} · {invited_name} ← {referrer_name}"
        builder.row(CallbackButton(text=text, payload=f"ref_visit:{item['id']}:{offset}"))

    if not referrals:
        builder.row(CallbackButton(text="Нет ожидающих рефералов", payload="noop"))

    nav_buttons = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav_buttons.append(CallbackButton(text="◀️ Назад", payload=f"admin:pending:{prev_offset}"))
    if offset + PAGE_SIZE < total:
        next_offset = offset + PAGE_SIZE
        nav_buttons.append(CallbackButton(text="Вперёд ▶️", payload=f"admin:pending:{next_offset}"))
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(
        CallbackButton(text="⬅️ Меню админки", payload="admin:menu"),
        CallbackButton(text="⬅️ Главное меню", payload="menu:main"),
    )
    return builder.as_markup()


def active_rewards_keyboard(rewards: list[dict], offset: int, total: int) -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    for item in rewards:
        first_name = (item["first_name"] or "Без имени")[:18]
        text = f"✂️ Списать #{item['id']} · {first_name} · {item['amount']} ₽"
        builder.row(CallbackButton(text=text, payload=f"reward_use:{item['id']}:{offset}"))

    if not rewards:
        builder.row(CallbackButton(text="Нет активных бонусов", payload="noop"))

    nav_buttons = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav_buttons.append(CallbackButton(text="◀️ Назад", payload=f"admin:rewards:{prev_offset}"))
    if offset + PAGE_SIZE < total:
        next_offset = offset + PAGE_SIZE
        nav_buttons.append(CallbackButton(text="Вперёд ▶️", payload=f"admin:rewards:{next_offset}"))
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(
        CallbackButton(text="⬅️ Меню админки", payload="admin:menu"),
        CallbackButton(text="⬅️ Главное меню", payload="menu:main"),
    )
    return builder.as_markup()


def referral_card_keyboard() -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))
    return builder.as_markup()


@router.message_created(Command(["myid", "id"]))
@router.message_created(
    F.message.body.text.in_({"myid", "/myid", "id", "/id", "мой id", "мой айди", "айди", "Id", "ID"})
)
async def my_id(event: MessageCreated) -> None:
    if not event.message or not event.message.sender:
        return
    user = event.message.sender
    await event.message.answer(
        text=f"Твой MAX ID: <code>{user.user_id}</code>",
        format=TextFormat.HTML,
    )


async def show_referrals_card(bot, user, event_or_callback):
    save_user(
        max_user_id=user.user_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    referral_link = await build_referral_link(bot, user.user_id)
    stats = get_referral_stats(user.user_id)

    text = (
        "🎁 <b>Приглашай друзей и получай бонусы!</b>\n\n"
        "Отправь свою персональную ссылку другу. "
        f"Когда он впервые придет на стрижку или уход, тебе начислится <b>{REFERRAL_REWARD_TEXT}</b>!\n\n"
        f"🔗 <b>Твоя реферальная ссылка:</b>\n<code>{referral_link}</code>\n\n"
        "📊 <b>Твоя статистика:</b>\n"
        f"• Переходов по ссылке: <b>{stats['started']}</b>\n"
        f"• Подтвержденных визитов: <b>{stats['confirmed']}</b>\n"
        f"• Доступных бонусов: <b>{stats['active_rewards_count']}</b> (на сумму <b>{stats['active_rewards_sum']} ₽</b>)\n\n"
        "<i>Бонусы суммируются: 300 ₽ за каждого друга. Их можно списать при оплате услуг мастера.</i>"
    )

    if isinstance(event_or_callback, MessageCallback):
        await event_or_callback.edit(
            text=text,
            attachments=[referral_card_keyboard()],
            format=TextFormat.HTML,
        )
    else:
        await event_or_callback.message.answer(
            text=text,
            attachments=[referral_card_keyboard()],
            format=TextFormat.HTML,
        )


@router.message_created(Command(["referral", "ref"]))
@router.message_created(
    F.message.body.text.in_({"🎁 Пригласить друга", "Пригласить друга", "пригласить друга"})
)
async def referrals_message(event: MessageCreated) -> None:
    if not event.message or not event.message.sender:
        return
    bot = event._ensure_bot()
    await show_referrals_card(bot, event.message.sender, event)


@router.message_callback(F.callback.payload == "menu:referrals")
async def referrals_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    bot = event._ensure_bot()
    await show_referrals_card(bot, event.callback.user, event)


@router.message_created(Command(["admin"]))
@router.message_created(
    F.message.body.text.in_({"⚙️ Админ-панель", "Админ-панель", "админ", "Панель мастера"})
)
async def admin_panel_message(event: MessageCreated) -> None:
    if not event.message or not event.message.sender:
        return
    user = event.message.sender
    if not is_admin(user.user_id):
        await event.message.answer(text="Нет доступа.", format=TextFormat.HTML)
        return

    await event.message.answer(
        text="⚙️ <b>Панель мастера и администратора:</b>",
        attachments=[admin_menu_keyboard()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.in_({"menu:admin", "admin:menu"}))
async def admin_menu_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    await event.edit(
        text="⚙️ <b>Панель мастера и администратора:</b>",
        attachments=[admin_menu_keyboard()],
        format=TextFormat.HTML,
    )


# Appointments Calendar
@router.message_callback(F.callback.payload.startswith("admin:appts:"))
async def admin_appts_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    current_filter = parts[2] if len(parts) > 2 else "all"
    offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    counts = get_appointments_counts()
    appts, total = get_master_appointments(date_filter=current_filter, limit=PAGE_SIZE, offset=offset)

    filter_names = {
        "today": "Сегодня",
        "tomorrow": "Завтра",
        "pending": "Ожидающие подтверждения",
        "all": "Все будущие",
    }
    title = filter_names.get(current_filter, current_filter)

    text = f"📋 <b>Календарь записей · {title}</b> ({total} всего):\n\n"
    if not appts:
        text += "<i>Записей не найдено.</i>"
    else:
        text += "<i>Нажмите на запись для просмотра деталей, подтверждения, завершения визита или переноса:</i>"

    await event.edit(
        text=text,
        attachments=[admin_appts_keyboard(appts, current_filter, offset, total, counts)],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:appt:"))
async def admin_appt_detail_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    try:
        await event.answer()
    except Exception:
        pass

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    appt = get_appointment(appt_id)
    if not appt:
        await event.answer(notification="Запись не найдена")
        return

    st = appt.get("status", "confirmed")
    if st == "completed":
        st_text = "✂️ Завершена (клиент пострижен)"
    elif st == "cancelled":
        st_text = "❌ Отменена"
    elif appt.get("client_confirmed"):
        st_text = "🟢 Подтверждена клиентом"
    else:
        st_text = "⏳ Ожидает подтверждения"

    phone = appt.get("phone") or "—"
    name = appt.get("client_name") or "Клиент"
    services = appt.get("services") or "—"
    price = appt.get("total_price") or 0
    time_fmt = format_dt(appt["appointment_time"])
    platform = (appt.get("platform") or "max").upper()
    masters_id = appt.get("masters_id") or "—"

    card_text = (
        f"📋 <b>Карточка записи #{appt['id']}</b>\n\n"
        f"👤 <b>Клиент:</b> {html.escape(name)}\n"
        f"📱 <b>Телефон:</b> <code>{phone}</code>\n"
        f"✂️ <b>Услуга:</b> {html.escape(services)}\n"
        f"💰 <b>Стоимость:</b> {price} ₽\n"
        f"⏰ <b>Дата и время:</b> {time_fmt}\n"
        f"📌 <b>Статус:</b> {st_text}\n"
        f"🌐 <b>Источник:</b> {platform}\n"
        f"🔄 <b>Masters ID:</b> <code>{masters_id}</code>"
    )

    await event.edit(
        text=card_text,
        attachments=[admin_appt_card_keyboard(appt, current_filter, offset)],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:complete_appt:"))
async def admin_complete_appt_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    ok, msg, appt, ref_notify = complete_appointment(appt_id)
    if not ok:
        await event.answer(notification=msg[:60])
        return

    if ref_notify and ref_notify.get("referrer_max_id"):
        bot = event._ensure_bot()
        try:
            invited_name = html.escape(ref_notify["invited_first_name"])
            await bot.send_message(
                user_id=ref_notify["referrer_max_id"],
                text=(
                    f"🎉 <b>Отличные новости!</b>\n"
                    f"Твой друг <b>{invited_name}</b> посетил салон!\n"
                    f"Тебе начислен бонус: <b>{ref_notify['amount']} ₽</b>.\n\n"
                    f"💰 Доступный баланс бонусов: <b>{ref_notify['new_balance']} ₽</b>.\n"
                    "Ты можешь списать их при следующем визите к мастеру!"
                ),
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.warning("Не удалось отправить уведомление рефереру: %s", e)

    await event.answer(notification=msg[:60])
    updated_appt = get_appointment(appt_id)
    if updated_appt:
        await event.edit(
            text=f"✅ {msg}\n\n" + f"📋 <b>Карточка записи #{appt_id}</b>\nСтатус: ✂️ <b>Завершена</b>",
            attachments=[admin_appt_card_keyboard(updated_appt, current_filter, offset)],
            format=TextFormat.HTML,
        )


@router.message_callback(F.callback.payload.startswith("admin:confirm_appt:"))
async def admin_confirm_appt_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    appt_id = int(payload.split(":")[2])
    ok, msg, appt = confirm_appointment_by_client(appt_id)
    await event.answer(notification=msg[:60])
    await admin_appt_detail_callback(event)


@router.message_callback(F.callback.payload.startswith("admin:cancel_appt:"))
async def admin_cancel_appt_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    ok, msg, appt = cancel_appointment(appt_id)
    if ok and appt and appt.get("platform_user_id") and appt.get("platform") == "max":
        bot = event._ensure_bot()
        try:
            await bot.send_message(
                user_id=appt["platform_user_id"],
                text="❌ <b>Ваша запись была отменена мастером.</b>\n\nЕсли у вас возникли вопросы, свяжитесь с мастером.",
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.warning("Failed to notify client about cancel: %s", e)

    await event.answer(notification="Запись отменена")
    appts, total = get_master_appointments(date_filter=current_filter, limit=PAGE_SIZE, offset=offset)
    await event.edit(
        text=f"❌ Запись #{appt_id} отменена.\n\n📋 <b>Календарь записей ({total} всего):</b>",
        attachments=[admin_appts_keyboard(appts, current_filter, offset, total)],
        format=TextFormat.HTML,
    )


# Reschedule flow
@router.message_callback(F.callback.payload.startswith("admin:resched_start:"))
async def admin_resched_start_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    now = get_current_saratov_time()
    builder = InlineKeyboardBuilder()
    for i in range(7):
        d = now + datetime.timedelta(days=i)
        day_key = d.strftime("%Y-%m-%d")
        title = f"{RU_WEEKDAYS[d.weekday()]}, {d.day} {RU_MONTHS[d.month]}"
        if i == 0:
            title += " (Сегодня)"
        elif i == 1:
            title += " (Завтра)"
        builder.row(CallbackButton(text=title, payload=f"admin:resched_day:{appt_id}:{day_key}:{current_filter}:{offset}"))

    builder.row(CallbackButton(text="⬅️ Назад к карточке", payload=f"admin:appt:{appt_id}:{current_filter}:{offset}"))

    await event.edit(
        text=f"🗓 <b>Перенос записи #{appt_id}</b>\n\nВыберите новую дату:",
        attachments=[builder.as_markup()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:resched_day:"))
async def admin_resched_day_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    day_key = parts[3]
    current_filter = parts[4] if len(parts) > 4 else "today"
    offset = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0

    hours = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00", "20:00"]
    builder = InlineKeyboardBuilder()
    row_btns = []
    for h in hours:
        row_btns.append(CallbackButton(text=h, payload=f"admin:resched_time:{appt_id}:{day_key}:{h}:{current_filter}:{offset}"))
        if len(row_btns) == 3:
            builder.row(*row_btns)
            row_btns = []
    if row_btns:
        builder.row(*row_btns)

    builder.row(CallbackButton(text="✏️ Ввести другое время (08:30...)", payload=f"admin:resched_custom:{appt_id}:{day_key}:{current_filter}:{offset}"))
    builder.row(CallbackButton(text="⬅️ Назад к выбору даты", payload=f"admin:resched_start:{appt_id}:{current_filter}:{offset}"))

    await event.edit(
        text=f"🗓 <b>Перенос записи #{appt_id} на {day_key}</b>\n\nВыберите время или введите своё:",
        attachments=[builder.as_markup()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:resched_time:"))
async def admin_resched_time_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    day_key = parts[3]
    time_str = parts[4]

    try:
        new_dt = datetime.datetime.strptime(f"{day_key} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SARATOV_TZ)
    except Exception:
        await event.answer(notification="Ошибка формата времени")
        return

    ok, msg, appt = reschedule_appointment(appt_id, new_dt)
    if ok and appt and appt.get("platform_user_id") and appt.get("platform") == "max":
        bot = event._ensure_bot()
        try:
            await bot.send_message(
                user_id=appt["platform_user_id"],
                text=(
                    "🗓 <b>Ваша запись перенесена мастером!</b>\n\n"
                    f"⏰ Новое время: <b>{format_dt(new_dt)}</b>\n"
                    f"✂️ Услуга: {appt.get('services')}\n"
                    f"💰 Стоимость: {appt.get('total_price')} ₽\n\n"
                    "Ждем вас!"
                ),
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.warning("Failed to notify client: %s", e)

    await event.answer(notification="Запись успешно перенесена!")
    await admin_appt_detail_callback(event)


@router.message_callback(F.callback.payload.startswith("admin:resched_custom:"))
async def admin_resched_custom_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    day_key = parts[3]
    current_filter = parts[4] if len(parts) > 4 else "today"
    offset = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0

    admin_resched_drafts[user.user_id] = {
        "appt_id": appt_id,
        "day_key": day_key,
        "step": "awaiting_custom_time",
    }

    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="⬅️ Отмена", payload=f"admin:appt:{appt_id}:{current_filter}:{offset}"))

    await event.edit(
        text=f"✏️ <b>Введите желаемое время на {day_key}</b>\n\nНапишите время в ответном сообщении (например <code>08:30</code> или <code>21:15</code>):",
        attachments=[builder.as_markup()],
        format=TextFormat.HTML,
    )


# Manual booking
@router.message_callback(F.callback.payload == "admin:create:start")
async def admin_create_start_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    opts = await asyncio.to_thread(get_service_options_for_creation)
    admin_create_drafts[user.user_id] = {
        "step": "service",
        "options": opts,
    }

    builder = InlineKeyboardBuilder()
    for idx, o in enumerate(opts):
        builder.row(CallbackButton(text=f"{o['name']} ({o['price']} ₽)", payload=f"admin:create:svc:{idx}"))
    builder.row(CallbackButton(text="✏️ Другая услуга / своя цена", payload="admin:create:svc:custom"))
    builder.row(CallbackButton(text="⬅️ Отмена", payload="admin:menu"))

    await event.edit(
        text="➕ <b>Создание новой записи мастером</b>\n\n<b>Шаг 1 из 4:</b> Выберите услугу из Masters или введите другое название:",
        attachments=[builder.as_markup()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:create:svc:"))
async def admin_create_svc_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    parts = event.callback.payload.split(":")
    svc_code = parts[3]

    draft = admin_create_drafts.setdefault(user.user_id, {})
    if svc_code == "custom":
        draft["step"] = "awaiting_custom_service"
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="⬅️ Отмена", payload="admin:create:start"))
        await event.edit(
            text="✏️ <b>Введите название услуги</b> в чат (например: <code>Моделирование бороды VIP</code>):",
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )
        return

    opts = draft.get("options")
    if not opts:
        opts = await asyncio.to_thread(get_service_options_for_creation)
        draft["options"] = opts

    if svc_code.isdigit():
        idx = int(svc_code)
        if 0 <= idx < len(opts):
            selected = opts[idx]
            svc_name = selected["name"]
            price = selected["price"]
        else:
            svc_name = "Стрижка (МУЖСКАЯ)"
            price = 2300
    else:
        svc_name = svc_code
        price = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 2300

    draft["service"] = svc_name
    draft["price"] = price
    draft["step"] = "day"

    now = get_current_saratov_time()
    builder = InlineKeyboardBuilder()
    for i in range(7):
        d = now + datetime.timedelta(days=i)
        day_key = d.strftime("%Y-%m-%d")
        title = f"{RU_WEEKDAYS[d.weekday()]}, {d.day} {RU_MONTHS[d.month]}"
        if i == 0:
            title += " (Сегодня)"
        elif i == 1:
            title += " (Завтра)"
        builder.row(CallbackButton(text=title, payload=f"admin:create:day:{day_key}"))

    builder.row(CallbackButton(text="⬅️ Отмена", payload="admin:menu"))
    await event.edit(
        text="➕ <b>Шаг 2 из 4:</b> Выберите дату записи:",
        attachments=[builder.as_markup()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:create:day:"))
async def admin_create_day_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    day_key = event.callback.payload.split(":")[3]
    draft = admin_create_drafts.setdefault(user.user_id, {})
    draft["day_key"] = day_key
    draft["step"] = "time"

    slots_info = await asyncio.to_thread(get_day_time_slots_status, day_key)
    free_slots = slots_info.get("free_slots", [])
    busy_appts = slots_info.get("busy_appts", [])

    lines = [f"➕ <b>Шаг 3 из 4: Выберите время на {day_key}</b>\n"]
    if free_slots:
        lines.append(f"🟢 <b>Свободные окна:</b> {', '.join(free_slots)}")
    else:
        lines.append("⚠️ <i>На этот день нет свободных окон в Masters</i>")

    if busy_appts:
        lines.append("\n🔴 <b>Уже занято в расписании:</b>")
        for b in busy_appts:
            c_name = b["client_name"] or "Клиент"
            lines.append(f"• <b>{b['time']}</b> — {c_name} (<i>{b['services']}</i>)")

    lines.append("\n<i>Выберите свободное окно 🟢 или занятое 🔴 для овербукинга:</i>")
    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    row_btns = []
    if free_slots:
        for s in free_slots:
            row_btns.append(CallbackButton(text=f"🟢 {s}", payload=f"admin:create:time:{s}"))
            if len(row_btns) == 3:
                builder.row(*row_btns)
                row_btns = []
        if row_btns:
            builder.row(*row_btns)
            row_btns = []

    if busy_appts:
        for b in busy_appts:
            row_btns.append(CallbackButton(text=f"🔴 {b['time']} (занято)", payload=f"admin:create:busy:{b['time']}"))
            if len(row_btns) == 2:
                builder.row(*row_btns)
                row_btns = []
        if row_btns:
            builder.row(*row_btns)
            row_btns = []

    builder.row(CallbackButton(text="✏️ Любое другое время (08:30, 22:00...)", payload="admin:create:time:custom"))
    builder.row(CallbackButton(text="⬅️ Назад к датам", payload="admin:create:start"))
    builder.row(CallbackButton(text="❌ Отмена", payload="admin:menu"))

    await event.edit(
        text=text,
        attachments=[builder.as_markup()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:create:busy:"))
async def admin_create_busy_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    time_val = event.callback.payload.split(":")[3]
    draft = admin_create_drafts.setdefault(user.user_id, {})
    day_key = draft.get("day_key", "")

    slots_info = await asyncio.to_thread(get_day_time_slots_status, day_key)
    b_appt = next((b for b in slots_info.get("busy_appts", []) if b["time"] == time_val), None)
    details = f"{b_appt['client_name']} ({b_appt['services']})" if b_appt else "запись из Masters"

    text = (
        f"⚠️ <b>Внимание: на {day_key} в {time_val} уже есть запись!</b>\n\n"
        f"• <b>{time_val}</b> — {details}\n\n"
        "Вы действительно хотите записать ещё одного клиента на это же время (овербукинг)?"
    )
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text=f"⚠️ Да, записать на {time_val}", payload=f"admin:create:time:{time_val}"))
    builder.row(CallbackButton(text="⬅️ Выбрать другое время", payload=f"admin:create:day:{day_key}"))

    await event.edit(
        text=text,
        attachments=[builder.as_markup()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:create:time:"))
async def admin_create_time_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    time_val = event.callback.payload.split(":")[3]
    draft = admin_create_drafts.setdefault(user.user_id, {})
    if time_val == "custom":
        draft["step"] = "awaiting_custom_time"
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="⬅️ Отмена", payload="admin:create:start"))
        await event.edit(
            text=f"✏️ <b>Введите любое время записи</b> на {draft.get('day_key')}\n\nНапишите в ответном сообщении (например <code>08:30</code> или <code>22:15</code>):",
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )
        return

    draft["time_str"] = time_val
    draft["step"] = "awaiting_client_info"

    curr_price = draft.get("price", 2300)
    curr_svc = draft.get("service", "Стрижка (МУЖСКАЯ)")
    day_key = draft.get("day_key", "")

    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="⬅️ Отмена", payload="admin:menu"))

    await event.edit(
        text=(
            f"➕ <b>Шаг 4 из 4: Данные клиента и цена</b>\n\n"
            f"✂️ Услуга: <b>{curr_svc}</b>\n"
            f"⏰ Время: <b>{day_key} {time_val}</b>\n"
            f"💰 Цена: <b>{curr_price} ₽</b>\n\n"
            "👉 <b>Отправьте в чат телефон и имя клиента:</b>\n"
            "Например: <code>+79991234567 Иван</code> или просто номер <code>89991234567</code>.\n\n"
            "<i>Если хотите изменить цену, укажите её в конце сообщения: <code>89991234567 Иван 1200</code></i>"
        ),
        attachments=[builder.as_markup()],
        format=TextFormat.HTML,
    )


# Schedule Management
@router.message_callback(F.callback.payload.startswith("admin:sched:"))
async def admin_sched_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    offset = int(event.callback.payload.split(":")[2])
    all_days = get_upcoming_days_schedule(days_ahead=14)
    slice_days = all_days[offset:offset+7]

    text = (
        "🕒 <b>Управление рабочим графиком мастера</b>\n\n"
        "Выберите день, чтобы сделать его выходным/рабочим или изменить часы работы (прибавить/убавить начало и конец дня):"
    )
    await event.edit(
        text=text,
        attachments=[admin_schedule_keyboard(slice_days, offset, len(all_days))],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:sched_day:"))
async def admin_sched_day_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    day_key = event.callback.payload.split(":")[2]
    sched = get_master_day_schedule(day_key)

    st_txt = "🟢 Рабочий день" if sched["is_working"] else "🔴 Выходной день"
    card_text = (
        f"🗓 <b>График на {day_key}:</b>\n\n"
        f"• Статус: <b>{st_txt}</b>\n"
        f"• Рабочие часы: <b>{sched['start_time']} — {sched['end_time']}</b>\n\n"
        "Вы можете изменить статус дня или скорректировать рабочие часы:"
    )
    await event.edit(
        text=card_text,
        attachments=[admin_sched_day_keyboard(sched)],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:sched_toggle:"))
async def admin_sched_toggle_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    day_key = event.callback.payload.split(":")[2]
    sched = get_master_day_schedule(day_key)
    new_work = not sched["is_working"]
    set_master_day_schedule(day_key, new_work, sched["start_time"], sched["end_time"])

    msg = "День отмечен как РАБОЧИЙ!" if new_work else "День отмечен как ВЫХОДНОЙ!"
    await event.answer(notification=msg[:60])
    await admin_sched_day_callback(event)


@router.message_callback(F.callback.payload.startswith("admin:sched_adj:"))
async def admin_sched_adj_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    parts = event.callback.payload.split(":")
    day_key = parts[2]
    which = parts[3]
    delta = int(parts[4])

    if which == "start":
        adjust_master_day_hours(day_key, start_delta_hours=delta, end_delta_hours=0)
    else:
        adjust_master_day_hours(day_key, start_delta_hours=0, end_delta_hours=delta)

    await event.answer(notification="Часы обновлены")
    await admin_sched_day_callback(event)


# Referrals & Rewards
@router.message_callback(F.callback.payload.startswith("admin:pending"))
async def admin_pending_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    parts = event.callback.payload.split(":")
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    referrals_list, total = get_pending_referrals(limit=PAGE_SIZE, offset=offset)
    count_info = f" ({total} всего)" if total > 0 else ""

    await event.edit(
        text=(
            f"⏳ <b>Ожидают подтверждения визита{count_info}:</b>\n"
            "<i>(нажми на нужного клиента, чтобы подтвердить его визит и начислить бонус другу)</i>"
        ),
        attachments=[pending_referrals_keyboard(referrals_list, offset, total)],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:rewards"))
async def admin_rewards_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    parts = event.callback.payload.split(":")
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    rewards_list, total = get_active_rewards(limit=PAGE_SIZE, offset=offset)
    count_info = f" ({total} всего)" if total > 0 else ""

    await event.edit(
        text=(
            f"💰 <b>Активные бонусы клиентов{count_info}:</b>\n"
            "<i>(нажми на бонус, чтобы списать его при оплате клиентом)</i>"
        ),
        attachments=[active_rewards_keyboard(rewards_list, offset, total)],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("ref_visit:"))
async def confirm_visit_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    parts = event.callback.payload.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await event.answer(notification="Некорректный ID")
        return

    referral_id = int(parts[1])
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    success, text, notify_data = mark_referral_visited(referral_id)

    if success and notify_data and notify_data.get("referrer_max_id"):
        bot = event._ensure_bot()
        try:
            invited_name = html.escape(notify_data["invited_first_name"])
            await bot.send_message(
                user_id=notify_data["referrer_max_id"],
                text=(
                    f"🎉 <b>Отличные новости!</b>\n"
                    f"Твой друг <b>{invited_name}</b> посетил салон!\n"
                    f"Тебе начислен бонус: <b>{notify_data['amount']} ₽</b>.\n\n"
                    f"💰 Доступный баланс бонусов: <b>{notify_data['new_balance']} ₽</b>.\n"
                    "Ты можешь списать их при следующем визите к мастеру!"
                ),
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.warning("Не удалось отправить уведомление рефереру: %s", e)

    referrals_list, total = get_pending_referrals(limit=PAGE_SIZE, offset=offset)
    safe_text = html.escape(text)

    await event.edit(
        text=f"ℹ️ {safe_text}\n\n⏳ <b>Ожидают подтверждения:</b>",
        attachments=[pending_referrals_keyboard(referrals_list, offset, total)],
        format=TextFormat.HTML,
    )
    await event.answer(notification=text[:60])


@router.message_callback(F.callback.payload.startswith("reward_use:"))
async def use_reward_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    parts = event.callback.payload.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await event.answer(notification="Некорректный ID")
        return

    reward_id = int(parts[1])
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    success, text, notify_data = mark_reward_used(reward_id)

    if success and notify_data and notify_data.get("user_max_id"):
        bot = event._ensure_bot()
        try:
            await bot.send_message(
                user_id=notify_data["user_max_id"],
                text=(
                    f"✂️ Бонус <b>{notify_data['amount']} ₽</b> успешно списан в счет оплаты визита!\n"
                    f"💰 Оставшийся баланс бонусов: <b>{notify_data['remaining_balance']} ₽</b>.\n\n"
                    "Спасибо за визит, ждем снова!"
                ),
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.warning("Не удалось отправить уведомление клиенту о списании: %s", e)

    rewards_list, total = get_active_rewards(limit=PAGE_SIZE, offset=offset)
    safe_text = html.escape(text)

    await event.edit(
        text=f"ℹ️ {safe_text}\n\n💰 <b>Активные бонусы клиентов:</b>",
        attachments=[active_rewards_keyboard(rewards_list, offset, total)],
        format=TextFormat.HTML,
    )
    await event.answer(notification=text[:60])


@router.message_callback(F.callback.payload == "noop")
async def noop_callback(event: MessageCallback) -> None:
    await event.answer()


# Masters integration
@router.message_callback(F.callback.payload == "admin:masters")
async def admin_masters_status(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    session = get_active_masters_session()
    builder = InlineKeyboardBuilder()
    if session:
        text = (
            "✅ <b>Синхронизация Masters подключена</b>\n\n"
            f"📱 Номер мастера: <code>{session['master_phone']}</code>\n"
            "Записи из Masters автоматически синхронизируются каждые 10 минут, "
            "а клиенты получают напоминания о стрижке накануне вечером и за 2 часа.\n\n"
            "Вы можете запустить принудительную синхронизацию прямо сейчас:"
        )
        builder.row(CallbackButton(text="🔄 Синхронизировать сейчас", payload="admin:masters_sync"))
        builder.row(CallbackButton(text="🚪 Отключить аккаунт Masters", payload="admin:masters_logout"))
        builder.row(CallbackButton(text="⬅️ Меню админки", payload="admin:menu"))
    else:
        text = (
            "🔗 <b>Подключение аккаунта Masters</b>\n\n"
            "Бот может автоматически забирать все записи из твоего личного кабинета Masters "
            "и бесплатно рассылать клиентам напоминания о визите накануне вечером и за 2 часа.\n\n"
            "Нажми кнопку ниже, чтобы отправить проверочный код на твой номер:"
        )
        builder.row(CallbackButton(text="📲 Запросить СМС-код для входа", payload="admin:masters_request"))
        builder.row(CallbackButton(text="⬅️ Меню админки", payload="admin:menu"))

    await event.edit(
        text=text,
        attachments=[builder.as_markup()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload == "admin:masters_request")
async def admin_masters_request_code(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    user_id = user.user_id
    phone = get_user_phone(user_id, platform="max")

    if not phone:
        pending_master_logins[user_id] = {"awaiting_phone": True}
        await event.edit(
            text=(
                "📱 <b>Введите номер телефона мастера</b>\n\n"
                "Напишите номер, зарегистрированный в сервисе Masters (например, <code>+79991234567</code>):"
            ),
            format=TextFormat.HTML,
        )
        return

    success, msg, session_data = request_master_login(phone)
    if not success:
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="⬅️ Назад", payload="admin:masters"))
        await event.edit(
            text=f"❌ <b>{msg}</b>\n\nПопробуйте ещё раз:",
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )
        return

    pending_master_logins[user_id] = session_data
    await event.edit(
        text=(
            f"📩 <b>Код подтверждения отправлен</b>\n\n"
            f"На номер <code>{session_data['phone_formatted']}</code> отправлено СМС от Masters.\n\n"
            "👉 <b>Напишите 4 цифры из СМС в ответном сообщении:</b>"
        ),
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload == "admin:masters_sync")
async def admin_masters_sync_now(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    ok, count, msg = sync_masters_appointments()
    await event.answer(notification=msg[:60])


@router.message_callback(F.callback.payload == "admin:masters_logout")
async def admin_masters_disconnect(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    disconnect_masters_session()
    await event.answer(notification="Синхронизация Masters отключена.")
    await admin_masters_status(event)


# Broadcast
@router.message_callback(F.callback.payload == "admin:broadcast")
async def admin_broadcast_start(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    admin_broadcast_drafts[user.user_id] = {"step": "awaiting_text"}

    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="⬅️ Отмена", payload="admin:broadcast:cancel"))

    text = (
        "📢 <b>Рассылка сообщений клиентам (MAX)</b>\n\n"
        "Напишите и отправьте в чат текст, который хотите разослать всем клиентам.\n"
        "<i>(Можно использовать смайлы и форматирование: жирный, курсив и т.д.)</i>\n\n"
        "После отправки бот покажет предпросмотр и попросит подтвердить рассылку перед отправкой."
    )
    await event.edit(
        text=text,
        attachments=[builder.as_markup()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload == "admin:broadcast:cancel")
async def admin_broadcast_cancel(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    admin_broadcast_drafts.pop(user.user_id, None)
    await event.answer(notification="Рассылка отменена.")
    await admin_menu_callback(event)


@router.message_callback(F.callback.payload == "admin:broadcast:send")
async def admin_broadcast_send(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    draft = admin_broadcast_drafts.pop(user.user_id, None)
    if not draft or not draft.get("text"):
        await event.answer(notification="Черновик рассылки не найден.")
        return

    text = draft["text"]
    recipients = get_broadcast_recipients("max")
    total = len(recipients)

    await event.edit(
        text=f"⏳ <b>Отправка рассылки...</b>\n0 из {total}",
        format=TextFormat.HTML,
    )

    bot = event._ensure_bot()
    client_kb = InlineKeyboardBuilder()
    client_kb.row(CallbackButton(text="✂️ Записаться онлайн", payload="menu:booking"))

    success_count = 0
    fail_count = 0

    for uid in recipients:
        try:
            await bot.send_message(
                user_id=uid,
                text=text,
                attachments=[client_kb.as_markup()],
                format=TextFormat.HTML,
            )
            success_count += 1
        except Exception as e:
            logger.warning("Failed broadcast to %s: %s", uid, e)
            fail_count += 1
        await asyncio.sleep(0.05)

    report = (
        "✅ <b>Рассылка успешно завершена!</b>\n\n"
        f"📬 <b>Доставлено:</b> {success_count}\n"
        f"⚠️ <b>Не доставлено:</b> {fail_count}\n"
        f"👥 <b>Всего получателей:</b> {total}"
    )
    back_kb = InlineKeyboardBuilder()
    back_kb.row(CallbackButton(text="⬅️ Меню админки", payload="admin:menu"))

    await event.edit(
        text=report,
        attachments=[back_kb.as_markup()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:clients:"))
async def admin_clients_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    action = parts[2] if len(parts) > 2 else "0"

    if action == "import":
        admin_clients_state[user.user_id] = {"action": "awaiting_import"}
        text = (
            "📥 <b>Импорт базы клиентов из Masters</b>\n\n"
            "Вы можете выгрузить базу клиентов из Masters и загрузить в бота:\n"
            "1. В приложении или на сайте Masters: <b>Клиенты ➔ Экспорт базы (в Excel / CSV)</b>.\n"
            "2. Отправьте список клиентов сообщением в этот чат в формате:\n"
            "<code>Иван Иванов +79271234567\n"
            "Алексей 89053214455</code>"
        )
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="⬅️ Назад к базе", payload="admin:clients:0"))
        await event.edit(text=text, attachments=[builder.as_markup()], format=TextFormat.HTML)
        await event.answer()
        return

    if action == "search":
        admin_clients_state[user.user_id] = {"action": "awaiting_search"}
        text = (
            "🔍 <b>Поиск клиента в базе</b>\n\n"
            "Отправьте в чат имя или номер телефона клиента для поиска:"
        )
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="⬅️ Назад к базе", payload="admin:clients:0"))
        await event.edit(text=text, attachments=[builder.as_markup()], format=TextFormat.HTML)
        await event.answer()
        return

    offset = int(action) if action.isdigit() else 0
    stats = get_clients_stats()
    clients, total = get_clients_page(offset=offset, limit=PAGE_SIZE)

    text = f"👥 <b>База клиентов</b> ({stats['total']} всего, с телефонами: {stats['with_phone']}):\n\n"
    if not clients:
        text += "<i>В базе пока нет сохранённых клиентов.\nНажмите кнопку «📥 Импорт из Masters», чтобы загрузить контакты.</i>"
    else:
        for c in clients:
            c_name = f"{c['first_name'] or ''} {c['last_name'] or ''}".strip() or "Без имени"
            c_phone = c.get("phone") or "—"
            text += f"• <b>{html.escape(c_name)}</b> — <code>{c_phone}</code>\n"

    builder = InlineKeyboardBuilder()
    nav_btns = []
    if offset > 0:
        nav_btns.append(CallbackButton(text="◀️ Назад", payload=f"admin:clients:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        nav_btns.append(CallbackButton(text="Вперёд ▶️", payload=f"admin:clients:{offset + PAGE_SIZE}"))
    if nav_btns:
        builder.row(*nav_btns)

    builder.row(CallbackButton(text="📥 Импорт из Masters", payload="admin:clients:import"))
    builder.row(
        CallbackButton(text="🔍 Поиск клиента", payload="admin:clients:search"),
        CallbackButton(text="⬅️ Меню админки", payload="admin:menu"),
    )

    await event.edit(text=text, attachments=[builder.as_markup()], format=TextFormat.HTML)
    await event.answer()


@router.message_callback(F.callback.payload.startswith("admin:edit_client:"))
async def admin_edit_client_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    appt = get_appointment(appt_id)
    if not appt:
        await event.answer(notification="Запись не найдена")
        return

    admin_edit_client_drafts[user.user_id] = {
        "appt_id": appt_id,
        "current_filter": current_filter,
        "offset": offset,
    }

    cur_name = appt.get("client_name") or "Клиент"
    cur_phone = appt.get("phone") or "—"

    text = (
        f"✏️ <b>Изменение данных клиента для записи #{appt_id}</b>\n\n"
        f"Текущее имя: <b>{html.escape(cur_name)}</b>\n"
        f"Текущий телефон: <code>{cur_phone}</code>\n\n"
        "👉 <b>Отправьте в чат имя и/или номер телефона клиента:</b>\n"
        "Например: <code>Иван +79271234567</code> или просто номер <code>+79271234567</code>, или имя <code>Иван</code>\n\n"
        "<i>Запись мгновенно обновится в базе данных.</i>"
    )
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="⬅️ Отмена", payload=f"admin:appt:{appt_id}:{current_filter}:{offset}"))

    await event.edit(text=text, attachments=[builder.as_markup()], format=TextFormat.HTML)
    await event.answer()


@router.message_callback(F.callback.payload.startswith("admin:edit_price:"))
async def admin_edit_price_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    appt = get_appointment(appt_id)
    if not appt:
        await event.answer(notification="Запись не найдена")
        return

    cur_svc = appt.get("services") or "—"
    cur_price = appt.get("total_price") or 0
    cur_client = appt.get("client_name") or "Клиент"

    text = (
        f"💰 <b>Редактирование услуги и стоимости для записи #{appt_id}</b>\n\n"
        f"👤 Клиент: <b>{html.escape(cur_client)}</b>\n"
        f"✂️ Текущая услуга: <b>{html.escape(cur_svc)}</b>\n"
        f"💵 Текущая стоимость: <b>{cur_price} ₽</b>\n\n"
        "👉 <b>Выберите реальную услугу с ценой в 1 клик или введите любую свою цену:</b>"
    )

    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="💈 Стрижка (МУЖСКАЯ) — 2 300 ₽", payload=f"admin:set_price:{appt_id}:2300:hair:{current_filter}:{offset}"))
    builder.row(CallbackButton(text="🧔 Оформление бороды — 700 ₽", payload=f"admin:set_price:{appt_id}:700:beard:{current_filter}:{offset}"))
    builder.row(CallbackButton(text="💈+🧔 Стрижка + Борода — 3 000 ₽", payload=f"admin:set_price:{appt_id}:3000:combo:{current_filter}:{offset}"))
    builder.row(CallbackButton(text="✏️ Ввести свою цену (например 1500)", payload=f"admin:set_custom_price:{appt_id}:{current_filter}:{offset}"))
    builder.row(CallbackButton(text="⬅️ Назад к карточке", payload=f"admin:appt:{appt_id}:{current_filter}:{offset}"))

    await event.edit(text=text, attachments=[builder.as_markup()], format=TextFormat.HTML)
    await event.answer()


@router.message_callback(F.callback.payload.startswith("admin:set_price:"))
async def admin_set_price_quick_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    price = int(parts[3])
    svc_code = parts[4]

    svc_map = {
        "hair": "Стрижка (МУЖСКАЯ)",
        "beard": "Оформление бороды",
        "combo": "Стрижка + Борода",
    }
    svc_name = svc_map.get(svc_code, "Стрижка")

    ok, msg, appt = update_appointment_price_and_service(appt_id, price, svc_name)
    await event.answer(notification=f"Стоимость обновлена: {price} ₽")
    await admin_appt_detail_callback(event)


@router.message_callback(F.callback.payload.startswith("admin:set_custom_price:"))
async def admin_set_custom_price_callback(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    appt_id = int(parts[2])
    current_filter = parts[3] if len(parts) > 3 else "today"
    offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    admin_edit_price_drafts[user.user_id] = {
        "appt_id": appt_id,
        "current_filter": current_filter,
        "offset": offset,
    }

    text = (
        f"✏️ <b>Ввод своей стоимости для записи #{appt_id}</b>\n\n"
        "Отправьте в чат новую стоимость в рублях (например: <code>1500</code> или <code>2000</code>):"
    )
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="⬅️ Отмена", payload=f"admin:edit_price:{appt_id}:{current_filter}:{offset}"))

    await event.edit(text=text, attachments=[builder.as_markup()], format=TextFormat.HTML)
    await event.answer()


def _has_active_max_draft(event: MessageCreated) -> bool:
    if not event.message or not event.message.sender:
        return False
    uid = event.message.sender.user_id
    return (
        uid in pending_master_logins
        or uid in admin_broadcast_drafts
        or uid in admin_resched_drafts
        or uid in admin_create_drafts
        or uid in admin_edit_client_drafts
        or uid in admin_edit_price_drafts
        or uid in admin_clients_state
    )


# Text messages handler (SMS, Phone, Broadcast, Reschedule custom time, Create appointment, Client edit & search)
@router.message_created(~F.message.body.text.startswith("/"), _has_active_max_draft)
async def handle_max_text_messages(event: MessageCreated) -> None:
    if not event.message or not event.message.sender or not event.message.body or not event.message.body.text:
        return
    user = event.message.sender
    user_id = user.user_id
    raw_text = event.message.body.text.strip()
    bot = event._ensure_bot()

    # 1. Master SMS code
    if user_id in pending_master_logins and raw_text.isdigit() and len(raw_text) == 4:
        session_data = pending_master_logins.get(user_id)
        if session_data and not session_data.get("awaiting_phone"):
            success, msg = confirm_master_login(session_data, raw_text)
            if not success:
                await event.message.answer(text=f"❌ <b>{msg}</b>\n\nПопробуйте ввести код ещё раз:", format=TextFormat.HTML)
                return
            pending_master_logins.pop(user_id, None)
            ok, count, sync_msg = sync_masters_appointments()
            await event.message.answer(
                text=(
                    "🎉 <b>Аккаунт Masters успешно подключен!</b>\n\n"
                    f"✅ {sync_msg}\n"
                    "Теперь бот будет автоматически отправлять клиентам напоминания накануне вечером и за 2 часа до начала визита."
                ),
                attachments=[admin_menu_keyboard()],
                format=TextFormat.HTML,
            )
            return

    # 2. Master phone input
    if user_id in pending_master_logins and pending_master_logins.get(user_id, {}).get("awaiting_phone") and is_phone_number(raw_text):
        success, msg, session_data = request_master_login(raw_text)
        if not success:
            await event.message.answer(text=f"❌ <b>{msg}</b>\n\nПопробуйте ввести другой номер телефона:", format=TextFormat.HTML)
            return
        pending_master_logins[user_id] = session_data
        await event.message.answer(
            text=(
                f"📩 <b>Код подтверждения отправлен</b>\n\n"
                f"На номер <code>{session_data['phone_formatted']}</code> отправлено СМС от Masters.\n\n"
                "👉 <b>Напишите 4 цифры из СМС в ответном сообщении:</b>"
            ),
            format=TextFormat.HTML,
        )
        return

    if not is_admin(user_id):
        return

    # 3. Edit client details on appointment
    if user_id in admin_edit_client_drafts:
        draft = admin_edit_client_drafts.pop(user_id)
        appt_id = draft["appt_id"]
        current_filter = draft.get("current_filter", "today")
        offset = draft.get("offset", 0)

        tokens = raw_text.split()
        client_phone = None
        name_parts = []
        for t in tokens:
            digits = re.sub(r"\D", "", t)
            if len(digits) >= 10:
                client_phone = normalize_phone(t)
            else:
                name_parts.append(t)

        client_name = " ".join(name_parts).strip()
        if not client_name:
            client_name = "Клиент"

        ok, msg = update_appointment_client_details(appt_id, client_name, client_phone)
        appt = get_appointment(appt_id) or {}
        fresh_name = appt.get("client_name") or client_name
        fresh_phone = appt.get("phone") or "—"

        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="📋 К карточке записи", payload=f"admin:appt:{appt_id}:{current_filter}:{offset}"))
        builder.row(CallbackButton(text="⬅️ К списку записей", payload=f"admin:appts:{current_filter}:{offset}"))

        await event.message.answer(
            text=(
                "✅ <b>Данные клиента успешно обновлены!</b>\n\n"
                f"👤 Клиент: <b>{html.escape(fresh_name)}</b>\n"
                f"📱 Телефон: <code>{fresh_phone}</code>"
            ),
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )
        return

    # 3b. Custom price edit on appointment
    if user_id in admin_edit_price_drafts:
        p_draft = admin_edit_price_drafts.pop(user_id)
        appt_id = p_draft["appt_id"]
        current_filter = p_draft.get("current_filter", "today")
        offset = p_draft.get("offset", 0)

        price_digits = re.sub(r"\D", "", raw_text)
        if not price_digits or int(price_digits) <= 0:
            await event.message.answer(text="⚠️ Пожалуйста, введите корректную сумму в рублях (например: <code>1500</code>):", format=TextFormat.HTML)
            admin_edit_price_drafts[user_id] = p_draft
            return

        new_price = int(price_digits)
        ok, res_msg, updated_appt = update_appointment_price_and_service(appt_id, new_price)

        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="📋 К карточке записи", payload=f"admin:appt:{appt_id}:{current_filter}:{offset}"))
        builder.row(CallbackButton(text="⬅️ К календарю записей", payload=f"admin:appts:{current_filter}:{offset}"))

        await event.message.answer(
            text=f"✅ <b>Стоимость записи #{appt_id} успешно изменена на {new_price} ₽!</b>",
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )
        return

    # 4. Clients search / import text
    c_state = admin_clients_state.get(user_id)
    if c_state:
        action = c_state.get("action")
        if action == "awaiting_search":
            admin_clients_state.pop(user_id, None)
            clients, total = get_clients_page(offset=0, limit=PAGE_SIZE, search=raw_text)
            text = f"🔍 <b>Результаты поиска «{html.escape(raw_text)}»</b> (найдено: {total}):\n\n"
            if not clients:
                text += "<i>Клиенты не найдены.</i>"
            else:
                for c in clients:
                    c_name = f"{c['first_name'] or ''} {c['last_name'] or ''}".strip() or "Без имени"
                    text += f"• <b>{html.escape(c_name)}</b> — <code>{c.get('phone', '—')}</code>\n"

            builder = InlineKeyboardBuilder()
            builder.row(CallbackButton(text="🔍 Новый поиск", payload="admin:clients:search"))
            builder.row(CallbackButton(text="👥 Вся база", payload="admin:clients:0"))
            await event.message.answer(text=text, attachments=[builder.as_markup()], format=TextFormat.HTML)
            return

        if action == "awaiting_import":
            admin_clients_state.pop(user_id, None)
            added, updated, msg = import_clients_from_text(raw_text)
            builder = InlineKeyboardBuilder()
            builder.row(CallbackButton(text="👥 Открыть базу клиентов", payload="admin:clients:0"))
            builder.row(CallbackButton(text="⬅️ Меню админки", payload="admin:menu"))
            await event.message.answer(
                text=(
                    "✅ <b>Импорт клиентов завершён!</b>\n\n"
                    f"• Добавлено новых клиентов: <b>{added}</b>\n"
                    f"• Обновлено записей: <b>{updated}</b>\n"
                    f"• {msg}"
                ),
                attachments=[builder.as_markup()],
                format=TextFormat.HTML,
            )
            return

    # 5. Broadcast text
    if admin_broadcast_drafts.get(user_id, {}).get("step") == "awaiting_text":
        recipients = get_broadcast_recipients("max")
        count = len(recipients)
        admin_broadcast_drafts[user_id] = {
            "step": "confirm",
            "text": raw_text,
        }
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text=f"✅ Отправить рассылку ({count})", payload="admin:broadcast:send"))
        builder.row(CallbackButton(text="✏️ Изменить текст", payload="admin:broadcast"))
        builder.row(CallbackButton(text="❌ Отмена", payload="admin:broadcast:cancel"))

        preview = (
            "📢 <b>Предпросмотр рассылки:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{raw_text}\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 <b>Получателей в MAX:</b> {count} чел.\n\n"
            "Отправить это сообщение всем клиентам?"
        )
        await event.message.answer(text=preview, attachments=[builder.as_markup()], format=TextFormat.HTML)
        return

    # 6. Reschedule custom time
    if admin_resched_drafts.get(user_id, {}).get("step") == "awaiting_custom_time":
        draft = admin_resched_drafts.pop(user_id)
        m = re.match(r"^(\d{1,2})[:.-](\d{2})$", raw_text)
        if not m:
            await event.message.answer(text="⚠️ Неверный формат времени. Введите время в формате <code>08:30</code> или <code>21:00</code>:", format=TextFormat.HTML)
            admin_resched_drafts[user_id] = draft
            return

        h, mm = int(m.group(1)), int(m.group(2))
        time_str = f"{h:02d}:{mm:02d}"
        day_key = draft["day_key"]
        appt_id = draft["appt_id"]

        try:
            new_dt = datetime.datetime.strptime(f"{day_key} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SARATOV_TZ)
        except Exception:
            await event.message.answer(text="Некорректная дата/время")
            return

        ok, msg, appt = reschedule_appointment(appt_id, new_dt)
        if ok and appt and appt.get("platform_user_id") and appt.get("platform") == "max":
            try:
                await bot.send_message(
                    user_id=appt["platform_user_id"],
                    text=(
                        "🗓 <b>Ваша запись перенесена мастером!</b>\n\n"
                        f"⏰ Новое время: <b>{format_dt(new_dt)}</b>\n"
                        f"✂️ Услуга: {appt.get('services')}\n"
                        f"💰 Стоимость: {appt.get('total_price')} ₽\n\n"
                        "Ждем вас!"
                    ),
                    format=TextFormat.HTML,
                )
            except Exception as e:
                logger.warning("Failed to notify client: %s", e)

        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="📋 К календарю", payload="admin:appts:today:0"))
        await event.message.answer(
            text=f"✅ <b>Запись #{appt_id} успешно перенесена на {format_dt(new_dt)}!</b>",
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )
        return

    # 7. Create appointment steps
    create_draft = admin_create_drafts.get(user_id)
    if create_draft:
        step = create_draft.get("step")

        if step == "awaiting_custom_service":
            create_draft["service"] = raw_text
            create_draft["price"] = 2300
            create_draft["step"] = "day"

            now = get_current_saratov_time()
            builder = InlineKeyboardBuilder()
            for i in range(7):
                d = now + datetime.timedelta(days=i)
                day_key = d.strftime("%Y-%m-%d")
                title = f"{RU_WEEKDAYS[d.weekday()]}, {d.day} {RU_MONTHS[d.month]}"
                builder.row(CallbackButton(text=title, payload=f"admin:create:day:{day_key}"))
            builder.row(CallbackButton(text="⬅️ Отмена", payload="admin:menu"))

            await event.message.answer(
                text=f"✂️ Услуга: <b>{raw_text}</b>\n\n<b>Шаг 2 из 4:</b> Выберите дату записи:",
                attachments=[builder.as_markup()],
                format=TextFormat.HTML,
            )
            return

        if step == "awaiting_custom_time":
            m = re.match(r"^(\d{1,2})[:.-](\d{2})$", raw_text)
            if not m:
                await event.message.answer(text="⚠️ Неверный формат. Введите время в формате <code>08:30</code> или <code>22:00</code>:", format=TextFormat.HTML)
                return
            h, mm = int(m.group(1)), int(m.group(2))
            create_draft["time_str"] = f"{h:02d}:{mm:02d}"
            create_draft["step"] = "awaiting_client_info"

            builder = InlineKeyboardBuilder()
            builder.row(CallbackButton(text="⬅️ Отмена", payload="admin:menu"))

            await event.message.answer(
                text=(
                    f"⏰ Время установлено: <b>{create_draft['time_str']}</b>\n\n"
                    "👉 <b>Теперь отправьте в чат телефон и имя клиента:</b>\n"
                    "Например: <code>+79991234567 Иван</code> или с ценой <code>89991234567 Иван 1200</code>"
                ),
                attachments=[builder.as_markup()],
                format=TextFormat.HTML,
            )
            return

        if step == "awaiting_client_info":
            tokens = raw_text.split()
            phone = ""
            name = "Клиент"
            custom_price = create_draft.get("price", 2300)

            for token in tokens:
                digits = re.sub(r"\D", "", token)
                if len(digits) >= 10:
                    phone = normalize_phone(token)
                elif token.isdigit() and int(token) >= 100:
                    custom_price = int(token)
                elif not token.isdigit():
                    name = token

            if not phone:
                await event.message.answer(text="⚠️ Пожалуйста, укажите номер телефона клиента (например: <code>+79991234567 Иван</code>):", format=TextFormat.HTML)
                return

            name_parts = [t for t in tokens if not re.sub(r"\D", "", t).isdigit() or len(re.sub(r"\D", "", t)) < 5]
            if name_parts:
                name = " ".join(name_parts)

            create_draft["phone"] = phone
            create_draft["name"] = name
            create_draft["price"] = custom_price

            day_key = create_draft["day_key"]
            time_str = create_draft["time_str"]
            dt_obj = datetime.datetime.strptime(f"{day_key} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SARATOV_TZ)

            ok, res_msg, appt_id = create_manual_appointment(
                phone=phone,
                appointment_time=dt_obj,
                services=create_draft["service"],
                total_price=custom_price,
                client_name=name,
                platform="manual",
            )

            admin_create_drafts.pop(user_id, None)

            if ok:
                builder = InlineKeyboardBuilder()
                builder.row(CallbackButton(text="📋 К календарю записей", payload="admin:appts:today:0"))
                builder.row(CallbackButton(text="➕ Создать ещё запись", payload="admin:create:start"))
                builder.row(CallbackButton(text="⬅️ Главное меню", payload="admin:menu"))
                await event.message.answer(
                    text=(
                        "🎉 <b>Запись успешно создана!</b>\n\n"
                        f"👤 Клиент: <b>{name}</b> (<code>{phone}</code>)\n"
                        f"⏰ Дата и время: <b>{format_dt(dt_obj)}</b>\n"
                        f"✂️ Услуга: <b>{create_draft['service']}</b>\n"
                        f"💰 Стоимость: <b>{custom_price} ₽</b>\n\n"
                        "✅ Запись внесена в календарь и синхронизирована с Masters CRM!"
                    ),
                    attachments=[builder.as_markup()],
                    format=TextFormat.HTML,
                )
            else:
                await event.message.answer(text=f"❌ Ошибка создания записи: {res_msg}", format=TextFormat.HTML)
            return
