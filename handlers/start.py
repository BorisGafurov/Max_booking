import html
import logging
from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.filters.command import CommandStart
from maxapi.types.updates.bot_started import BotStarted
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated
from maxapi.utils.deep_linking import decode_payload

from config import ADMIN_IDS
from keyboards.menu import get_main_menu
from services.referrals import register_referral, save_user

logger = logging.getLogger(__name__)
router = Router(router_id="start")


async def handle_start_logic(
    bot,
    user,
    chat_id: int | None,
    raw_payload: str | None = None,
    edit_callback_event: MessageCallback | None = None,
):
    if not user:
        return

    user_db_id, is_new = save_user(
        max_user_id=user.user_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    text = "Привет! Выбери нужный раздел:"
    extra_text = ""

    payload = raw_payload
    if payload:
        # Check if payload was encoded
        try:
            if not payload.startswith("ref_"):
                decoded = decode_payload(payload)
                if decoded.startswith("ref_"):
                    payload = decoded
        except Exception:
            pass

    if payload and payload.startswith("ref_"):
        referrer_part = payload.replace("ref_", "", 1)
        if referrer_part.isdigit():
            referrer_max_id = int(referrer_part)
            if not is_new:
                extra_text = (
                    "\n\nℹ️ Вы уже были зарегистрированы в боте ранее. "
                    "Реферальный бонус начисляется только за приглашение новых пользователей."
                )
            else:
                success, result_text, notify_data = register_referral(
                    new_user_max_id=user.user_id,
                    referrer_max_id=referrer_max_id,
                )

                if success:
                    extra_text = (
                        "\n\n🎁 Вы пришли по приглашению. Реферальная связь сохранена!"
                    )
                    if notify_data:
                        try:
                            invited_name = html.escape(notify_data["invited_first_name"])
                            await bot.send_message(
                                user_id=notify_data["referrer_max_id"],
                                text=(
                                    f"👋 По вашей ссылке зарегистрировался друг: <b>{invited_name}</b>!\n"
                                    f"После его первого визита в салон вам начислится бонус."
                                ),
                                format=TextFormat.HTML,
                            )
                        except Exception as e:
                            logger.warning("Не удалось уведомить пригласившего: %s", e)
                else:
                    extra_text = f"\n\n{result_text}"

    is_admin = user.user_id in ADMIN_IDS
    menu_markup = get_main_menu(is_admin=is_admin)

    if edit_callback_event:
        await edit_callback_event.edit(
            text=text + extra_text,
            attachments=[menu_markup],
            format=TextFormat.HTML,
        )
    else:
        target_chat = chat_id or (user.user_id if user else None)
        await bot.send_message(
            chat_id=target_chat,
            user_id=user.user_id,
            text=text + extra_text,
            attachments=[menu_markup],
            format=TextFormat.HTML,
        )


@router.bot_started()
async def on_bot_started(event: BotStarted) -> None:
    bot = event._ensure_bot()
    await handle_start_logic(
        bot=bot,
        user=event.user,
        chat_id=event.chat_id,
        raw_payload=event.payload,
    )


@router.message_created(CommandStart())
async def on_command_start(event: MessageCreated, args: list[str] | None = None) -> None:
    bot = event._ensure_bot()
    user = event.message.sender if event.message else None
    chat_id = event.message.recipient.chat_id if event.message and event.message.recipient else None

    raw_payload = args[0] if (args and len(args) > 0) else None

    await handle_start_logic(
        bot=bot,
        user=user,
        chat_id=chat_id,
        raw_payload=raw_payload,
    )


@router.message_callback(F.callback.payload == "menu:main")
async def on_menu_main_callback(event: MessageCallback) -> None:
    user = event.callback.user if event.callback else None
    bot = event._ensure_bot()
    chat_id = event.message.recipient.chat_id if event.message and event.message.recipient else None

    await handle_start_logic(
        bot=bot,
        user=user,
        chat_id=chat_id,
        edit_callback_event=event,
    )
