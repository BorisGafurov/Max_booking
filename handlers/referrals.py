import html
import logging
from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.filters.command import Command
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS, REFERRAL_REWARD_TEXT
from services.masters_sync import (
    confirm_master_login,
    disconnect_masters_session,
    get_active_masters_session,
    request_master_login,
    sync_masters_appointments,
)
from services.referrals import (
    build_referral_link,
    get_active_rewards,
    get_pending_referrals,
    get_referral_stats,
    get_user_phone,
    is_phone_number,
    mark_referral_visited,
    mark_reward_used,
    save_user,
)

logger = logging.getLogger(__name__)
router = Router(router_id="referrals")

PAGE_SIZE = 8
pending_master_logins: dict[int, dict] = {}


def admin_menu_keyboard() -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="⏳ Ожидают подтверждения", payload="admin:pending:0")
    )
    builder.row(
        CallbackButton(text="💰 Активные бонусы", payload="admin:rewards:0")
    )
    builder.row(
        CallbackButton(text="🔄 Синхронизация Masters", payload="admin:masters")
    )
    builder.row(
        CallbackButton(text="⬅️ Главное меню", payload="menu:main")
    )
    return builder.as_markup()


def pending_referrals_keyboard(
    referrals: list[dict], offset: int, total: int
) -> AttachmentButton:
    builder = InlineKeyboardBuilder()

    for item in referrals:
        invited_name = (item["invited_first_name"] or "Без имени")[:15]
        referrer_name = (item["referrer_first_name"] or "Без имени")[:15]
        text = f"✅ #{item['id']} · {invited_name} ← {referrer_name}"
        builder.row(
            CallbackButton(text=text, payload=f"ref_visit:{item['id']}:{offset}")
        )

    if not referrals:
        builder.row(
            CallbackButton(text="Нет ожидающих рефералов", payload="noop")
        )

    # Пагинация
    nav_buttons = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav_buttons.append(
            CallbackButton(text="◀️ Назад", payload=f"admin:pending:{prev_offset}")
        )
    if offset + PAGE_SIZE < total:
        next_offset = offset + PAGE_SIZE
        nav_buttons.append(
            CallbackButton(text="Вперёд ▶️", payload=f"admin:pending:{next_offset}")
        )
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(
        CallbackButton(text="⬅️ Меню админки", payload="admin:menu"),
        CallbackButton(text="⬅️ Главное меню", payload="menu:main"),
    )
    return builder.as_markup()


def active_rewards_keyboard(
    rewards: list[dict], offset: int, total: int
) -> AttachmentButton:
    builder = InlineKeyboardBuilder()

    for item in rewards:
        first_name = (item["first_name"] or "Без имени")[:18]
        text = f"✂️ Списать #{item['id']} · {first_name} · {item['amount']} ₽"
        builder.row(
            CallbackButton(text=text, payload=f"reward_use:{item['id']}:{offset}")
        )

    if not rewards:
        builder.row(
            CallbackButton(text="Нет активных бонусов", payload="noop")
        )

    # Пагинация
    nav_buttons = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav_buttons.append(
            CallbackButton(text="◀️ Назад", payload=f"admin:rewards:{prev_offset}")
        )
    if offset + PAGE_SIZE < total:
        next_offset = offset + PAGE_SIZE
        nav_buttons.append(
            CallbackButton(text="Вперёд ▶️", payload=f"admin:rewards:{next_offset}")
        )
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


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message_created(Command(["myid"]))
async def my_id(event: MessageCreated) -> None:
    if not event.message or not event.message.sender:
        return
    user = event.message.sender
    await event.message.answer(
        text=f"Твой MAX ID: <code>{user.user_id}</code>",
        format=TextFormat.HTML,
    )


async def show_referrals_card(bot, user, event_or_callback):
    # Ensure current user is registered in DB
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
    F.message.body.text.in_(
        {"🎁 Пригласить друга", "Пригласить друга", "пригласить друга"}
    )
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
    F.message.body.text.in_({"⚙️ Админ-панель", "Админ-панель", "админ"})
)
async def admin_panel_message(event: MessageCreated) -> None:
    if not event.message or not event.message.sender:
        return
    user = event.message.sender
    if not is_admin(user.user_id):
        await event.message.answer(text="Нет доступа.", format=TextFormat.HTML)
        return

    await event.message.answer(
        text="⚙️ <b>Админ-панель:</b>",
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
        text="⚙️ <b>Админ-панель:</b>",
        attachments=[admin_menu_keyboard()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("admin:pending"))
async def admin_pending(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
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
async def admin_rewards(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
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
async def confirm_visit(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await event.answer(notification="Некорректный ID")
        return

    referral_id = int(parts[1])
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    success, text, notify_data = mark_referral_visited(referral_id)

    if success and notify_data:
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
        notification=text[:100],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("reward_use:"))
async def use_reward(event: MessageCallback) -> None:
    if not event.callback or not event.callback.user:
        await event.answer()
        return
    user = event.callback.user
    if not is_admin(user.user_id):
        await event.answer(notification="Нет доступа.")
        return

    payload = event.callback.payload or ""
    parts = payload.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await event.answer(notification="Некорректный ID")
        return

    reward_id = int(parts[1])
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    success, text, notify_data = mark_reward_used(reward_id)

    if success and notify_data:
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
        notification=text[:100],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload == "noop")
async def noop_callback(event: MessageCallback) -> None:
    await event.answer()


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
            "Записи из Masters синхронизируются автоматически каждые 10 минут, "
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

    try:
        await event.edit(
            text=text,
            attachments=[builder.as_markup()],
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.warning("Error editing masters status: %s", e)
        if event.message:
            await event.message.answer(
                text=text,
                attachments=[builder.as_markup()],
                format=TextFormat.HTML,
            )
    await event.answer()


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
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="⬅️ Назад", payload="admin:masters"))
        try:
            await event.edit(
                text=(
                    "📱 <b>Введите номер телефона мастера</b>\n\n"
                    "Напишите номер, зарегистрированный в сервисе Masters (например, <code>+79991234567</code>):"
                ),
                attachments=[builder.as_markup()],
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.warning("Error editing msg: %s", e)
            if event.message:
                await event.message.answer(
                    text=(
                        "📱 <b>Введите номер телефона мастера</b>\n\n"
                        "Напишите номер, зарегистрированный в сервисе Masters (например, <code>+79991234567</code>):"
                    ),
                    attachments=[builder.as_markup()],
                    format=TextFormat.HTML,
                )
        await event.answer()
        return

    await event.answer(notification="Запрашиваю код в Masters...")
    success, msg, session_data = request_master_login(phone)

    if not success:
        builder = InlineKeyboardBuilder()
        builder.row(CallbackButton(text="⬅️ Назад", payload="admin:masters"))
        try:
            await event.edit(
                text=f"❌ <b>{msg}</b>\n\nПопробуйте ещё раз:",
                attachments=[builder.as_markup()],
                format=TextFormat.HTML,
            )
        except Exception as e:
            logger.warning("Error editing msg: %s", e)
            if event.message:
                await event.message.answer(
                    text=f"❌ <b>{msg}</b>\n\nПопробуйте ещё раз:",
                    attachments=[builder.as_markup()],
                    format=TextFormat.HTML,
                )
        return

    pending_master_logins[user_id] = session_data
    try:
        await event.edit(
            text=(
                f"📩 <b>Код подтверждения отправлен</b>\n\n"
                f"На номер <code>{session_data['phone_formatted']}</code> отправлено СМС от Masters.\n\n"
                "👉 <b>Напишите 4 цифры из СМС в ответном сообщении:</b>"
            ),
            format=TextFormat.HTML,
        )
    except Exception as e:
        logger.warning("Error editing msg: %s", e)
        if event.message:
            await event.message.answer(
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

    await event.answer(notification="Синхронизирую записи...")
    ok, count, msg = sync_masters_appointments()
    await event.answer(notification=msg[:60])
    await admin_masters_status(event)


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


@router.message_created(
    lambda event: bool(
        event.message
        and event.message.sender
        and event.message.sender.user_id in pending_master_logins
        and not pending_master_logins.get(event.message.sender.user_id, {}).get("awaiting_phone")
        and event.message.body
        and event.message.body.text
        and event.message.body.text.strip().isdigit()
        and len(event.message.body.text.strip()) == 4
    )
)
async def handle_master_sms_code(event: MessageCreated) -> None:
    if not event.message or not event.message.sender or not event.message.body or not event.message.body.text:
        return

    user_id = event.message.sender.user_id
    session_data = pending_master_logins.get(user_id)
    if not session_data or session_data.get("awaiting_phone"):
        return

    code = event.message.body.text.strip()
    success, msg = confirm_master_login(session_data, code)
    if not success:
        await event.message.answer(
            text=f"❌ <b>{msg}</b>\n\nПопробуйте ввести код ещё раз:",
            format=TextFormat.HTML,
        )
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


@router.message_created(
    lambda event: bool(
        event.message
        and event.message.sender
        and event.message.sender.user_id in pending_master_logins
        and pending_master_logins.get(event.message.sender.user_id, {}).get("awaiting_phone")
        and event.message.body
        and event.message.body.text
        and is_phone_number(event.message.body.text)
    )
)
async def handle_master_phone_input(event: MessageCreated) -> None:
    if not event.message or not event.message.sender or not event.message.body or not event.message.body.text:
        return

    user_id = event.message.sender.user_id
    phone = event.message.body.text.strip()
    success, msg, session_data = request_master_login(phone)
    if not success:
        await event.message.answer(
            text=f"❌ <b>{msg}</b>\n\nПопробуйте ввести другой номер телефона:",
            format=TextFormat.HTML,
        )
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
