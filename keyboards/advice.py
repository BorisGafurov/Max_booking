from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.attachments.buttons.link_button import LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from services.recommendations import get_articles, get_products


def get_recommendations_hub_keyboard() -> AttachmentButton:
    articles = get_articles()
    builder = InlineKeyboardBuilder()

    # Display articles directly in hub for instant 1-click access
    for slug, item in articles.items():
        builder.row(CallbackButton(text=item["title"][:40], payload=f"art:{slug}"))

    # Button to Ozon products
    builder.row(
        CallbackButton(text="🛍 Проверенные товары на Ozon", payload="rec:products")
    )
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))

    return builder.as_markup()


def get_articles_keyboard() -> AttachmentButton:
    articles = get_articles()
    builder = InlineKeyboardBuilder()

    for slug, item in articles.items():
        builder.row(CallbackButton(text=item["title"][:40], payload=f"art:{slug}"))

    builder.row(CallbackButton(text="⬅️ Назад к советам", payload="rec:hub"))
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))
    return builder.as_markup()


def get_products_keyboard() -> AttachmentButton:
    products = get_products()
    builder = InlineKeyboardBuilder()

    for slug, item in products.items():
        builder.row(CallbackButton(text=item["title"][:40], payload=f"prod:{slug}"))

    builder.row(CallbackButton(text="⬅️ Назад к советам", payload="rec:hub"))
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))
    return builder.as_markup()


def get_article_detail_keyboard(slug: str) -> AttachmentButton:
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="🛍 Товары на Ozon", payload="rec:products"))
    builder.row(CallbackButton(text="⬅️ Назад к советам", payload="rec:hub"))
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))
    return builder.as_markup()


def get_product_detail_keyboard(links: list[tuple[str, str]]) -> AttachmentButton:
    builder = InlineKeyboardBuilder()

    for label, url in links:
        btn_text = f"🛍 {label[:28]} ↗"
        builder.row(LinkButton(text=btn_text, url=url))

    builder.row(CallbackButton(text="⬅️ Назад к товарам", payload="rec:products"))
    builder.row(CallbackButton(text="⬅️ Главное меню", payload="menu:main"))
    return builder.as_markup()
