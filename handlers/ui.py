"""Shared helpers for rendering screens on messages (MAX)."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def edit_or_send(
    event: Any,
    text: str,
    attachments: Any = None,
    format: Any = None,
    **kwargs: Any,
) -> None:
    """
    Shows a screen on the message a button was pressed on.

    If editing fails (the message was deleted, is too old, or the API rejects
    the edit), the screen is sent as a fresh message so the button never
    appears dead.
    """
    payload: dict[str, Any] = {"text": text, **kwargs}
    if attachments is not None:
        payload["attachments"] = attachments
    if format is not None:
        payload["format"] = format

    try:
        await event.edit(**payload)
        return
    except Exception as e:
        logger.debug("MAX edit failed, sending a new message instead: %s", e)

    target = getattr(event, "message", None) or event
    try:
        await target.answer(**payload)
    except Exception as e:
        logger.warning("Failed to send fallback screen in MAX: %s", e)
