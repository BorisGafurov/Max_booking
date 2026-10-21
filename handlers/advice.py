import logging
from maxapi import F, Router
from maxapi.enums.parse_mode import TextFormat
from maxapi.filters.command import Command
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated

from keyboards.advice import (
    get_article_detail_keyboard,
    get_articles_keyboard,
    get_product_detail_keyboard,
    get_products_keyboard,
    get_recommendations_hub_keyboard,
)
from services.recommendations import get_article, get_product

logger = logging.getLogger(__name__)
router = Router(router_id="advice")

HUB_TEXT = (
    "🧴 <b>Рекомендации мастера и проверенный стайлинг</b>\n\n"
    "Здесь собраны подробные инструкции по укладке и ссылки на проверенные средства с Ozon, "
    "которыми мастер пользуется лично.\n\n"
    "Выбери нужный раздел:"
)


@router.message_created(Command(["advice", "tips", "care", "products"]))
@router.message_created(
    F.message.body.text.in_(
        {
            "🧴 Советы по уходу",
            "Советы по уходу",
            "советы по уходу",
            "🧴 Советы и товары",
            "Советы и товары",
            "советы и товары",
        }
    )
)
async def advice_message(event: MessageCreated) -> None:
    if not event.message:
        return
    await event.message.answer(
        text=HUB_TEXT,
        attachments=[get_recommendations_hub_keyboard()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.in_({"menu:advice", "rec:hub"}))
async def callback_rec_hub(event: MessageCallback) -> None:
    await event.edit(
        text=HUB_TEXT,
        attachments=[get_recommendations_hub_keyboard()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload == "rec:articles")
async def callback_rec_articles(event: MessageCallback) -> None:
    await event.edit(
        text=(
            "📖 <b>Инструкции по уходу и укладке:</b>\n\n"
            "Выбери тему, чтобы прочитать руководство мастера 👇"
        ),
        attachments=[get_articles_keyboard()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload == "rec:products")
async def callback_rec_products(event: MessageCallback) -> None:
    await event.edit(
        text=(
            "🛍 <b>Проверенные средства на Ozon:</b>\n\n"
            "Здесь собраны проверенные стайлинги и уход, которые мастер лично использует в работе. "
            "Выбери категорию, чтобы посмотреть рекомендации и ссылки 👇"
        ),
        attachments=[get_products_keyboard()],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("art:"))
async def callback_article_detail(event: MessageCallback) -> None:
    payload = event.callback.payload or ""
    slug = payload.split(":", 1)[1]
    article = get_article(slug)

    if not article:
        await event.answer(notification="Инструкция не найдена")
        return

    text = f"<b>{article['title']}</b>\n\n{article['text']}"
    await event.edit(
        text=text,
        attachments=[get_article_detail_keyboard(slug)],
        format=TextFormat.HTML,
    )


@router.message_callback(F.callback.payload.startswith("prod:"))
async def callback_product_detail(event: MessageCallback) -> None:
    payload = event.callback.payload or ""
    slug = payload.split(":", 1)[1]
    product = get_product(slug)

    if not product:
        await event.answer(notification="Товар не найден")
        return

    text = f"<b>{product['title']}</b>\n\n{product['text']}"
    await event.edit(
        text=text,
        attachments=[get_product_detail_keyboard(product["links"])],
        format=TextFormat.HTML,
    )
