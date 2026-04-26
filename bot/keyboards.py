from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def kb_after_result(revisions_left: int) -> InlineKeyboardMarkup | None:
    if revisions_left <= 0:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Хочу правку", callback_data="want_revision")],
        [InlineKeyboardButton("✅ Всё ок", callback_data="done_ok")],
    ])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отменить", callback_data="cancel_job")],
    ])
