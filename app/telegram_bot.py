import asyncio
import os
import re
import secrets
import sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import agent, db
from app import push as push_module
from app import scheduler


_app: Application | None = None
_loop: asyncio.AbstractEventLoop | None = None
# Serialize agent turns so two near-simultaneous messages don't both load the
# same _history() and replay each other's input. Single-user app, so one
# global lock is sufficient.
_turn_lock: asyncio.Lock | None = None


def _allowed_user_id() -> int:
    return int(os.environ["TELEGRAM_USER_ID"])


# Telegram renders plain text by default. Suri sometimes still emits markdown
# despite persona instructions ("**bold**", "# header", "[text](url)").
# Strip it deterministically here so she never has to remember.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_BOLD_ITALIC_RE = re.compile(r"(\*{1,3}|_{1,3})(.+?)\1", re.DOTALL)
_MD_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_FENCED_CODE_RE = re.compile(r"```[a-zA-Z]*\n?", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^(\s*)[*+]\s+", re.MULTILINE)


# Persona instructs the agent to end any approval-gated message with a line
# like:    [CONFIRM]: delete 5 emails from spam.com
# We intercept it here, strip from visible text, and render Yes/No buttons.
_CONFIRM_MARKER_RE = re.compile(r"^\s*\[CONFIRM\]:\s*(.+?)\s*$", re.MULTILINE)


def _extract_confirm(text: str) -> tuple[str, str | None]:
    m = _CONFIRM_MARKER_RE.search(text)
    if not m:
        return text, None
    description = m.group(1).strip()
    cleaned = _CONFIRM_MARKER_RE.sub("", text).rstrip()
    return cleaned, description


def strip_markdown(text: str) -> str:
    """Remove markdown syntax that Telegram doesn't render, leaving readable
    plain text. Conservative — only strips formatting marks, never content."""
    # links: "[label](url)" -> "label (url)"
    text = _MD_LINK_RE.sub(r"\1 (\2)", text)
    # ATX headers: "# Foo" -> "Foo"
    text = _MD_HEADER_RE.sub("", text)
    # fenced code blocks: drop the fences, keep the content
    text = _MD_FENCED_CODE_RE.sub("", text)
    # inline code: "`foo`" -> "foo"
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    # markdown bullets ("* item" / "+ item") -> "- item" (keep the indent)
    # Run BEFORE bold/italic so a leading "*" isn't mistaken for italic.
    text = _MD_BULLET_RE.sub(r"\1- ", text)
    # bold/italic/bold-italic with * or _: "**foo**" / "_foo_" / "***foo***" -> "foo"
    text = _MD_BOLD_ITALIC_RE.sub(r"\2", text)
    return text


def _make_emit(chat_id: int, bot, loop: asyncio.AbstractEventLoop):
    """Build the agent's send-callback. Logs each reply, strips markdown,
    and renders inline Yes/No buttons whenever the agent ends a message
    with a [CONFIRM]: marker."""

    def emit(reply: str):
        clean = strip_markdown(reply)
        text, confirm_desc = _extract_confirm(clean)
        # Log the cleaned (button-stripped) text so future _history() reads
        # see what the user actually saw.
        db.log_outbound(text or confirm_desc or "")
        if confirm_desc:
            token = secrets.token_hex(4)
            db.create_pending_action(
                action_id=token,
                action_type="confirm",
                payload={"description": confirm_desc},
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Yes", callback_data=f"y:{token}"),
                        InlineKeyboardButton("No", callback_data=f"n:{token}"),
                    ]
                ]
            )
            coro = bot.send_message(
                chat_id=chat_id,
                text=text or confirm_desc,
                reply_markup=kb,
            )
        else:
            coro = bot.send_message(chat_id=chat_id, text=text)
        asyncio.run_coroutine_threadsafe(coro, loop)

    return emit


async def _run_turn(chat_id: int, text: str, bot):
    """Shared entry point for both inbound text and button-callback synthetic
    messages. Logs the inbound under the turn lock and runs the agent."""
    if _turn_lock is None:
        raise RuntimeError("telegram bot not initialized: _turn_lock is None")
    loop = asyncio.get_running_loop()
    emit = _make_emit(chat_id, bot, loop)
    async with _turn_lock:
        db.log_inbound(text)
        try:
            await asyncio.to_thread(agent.handle, emit)
        except Exception as e:
            print(f"[telegram] agent error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            await bot.send_message(chat_id=chat_id, text=f"[error: {type(e).__name__}: {e}]")


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.effective_user.id != _allowed_user_id():
        # Silently ignore anyone who isn't the allowlisted user.
        # Suri is single-tenant; we don't want to leak presence to randos.
        return
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text:
        return

    chat_id = update.effective_chat.id
    bot = context.bot
    # TYPING outside the lock so the user sees activity even if a previous
    # turn is still running.
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    await _run_turn(chat_id, text, bot)


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Yes/No taps on inline confirmation buttons. Translates the tap
    into a synthetic user message ("yes — confirmed via button: ...") and
    runs the agent loop so it can act on the approval."""
    cq = update.callback_query
    if cq is None or cq.from_user is None or cq.from_user.id != _allowed_user_id():
        return
    await cq.answer()  # dismiss the loading spinner
    data = cq.data or ""
    if ":" not in data:
        return
    verdict, token = data.split(":", 1)
    pending = db.get_pending_action(token)
    if pending is None:
        try:
            await cq.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if pending["status"] != "pending":
        # Already handled — strip the buttons and bail.
        try:
            await cq.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    new_status = "confirmed" if verdict == "y" else "declined"
    db.update_pending_action_status(token, new_status)
    try:
        await cq.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    description = (pending["payload"] or {}).get("description", "")
    word = "yes" if verdict == "y" else "no"
    synthetic = f"{word} — {new_status} via button: {description}"
    chat_id = cq.message.chat_id if cq.message else _allowed_user_id()
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    await _run_turn(chat_id, synthetic, context.bot)


async def push(text: str):
    """Send a proactive (unprompted) message to the allowlisted user.
    Used by the scheduler when reminders fire."""
    if _app is None:
        raise RuntimeError("telegram bot not initialized")
    clean = strip_markdown(text)
    db.log_outbound(clean)
    await _app.bot.send_message(chat_id=_allowed_user_id(), text=clean)


def push_threadsafe(text: str):
    """Sync entry point for pushing from a non-asyncio thread (e.g. APScheduler).
    Schedules onto the bot loop and waits up to 10s for the send to complete
    so we surface failures to the caller (instead of silently swallowing them
    in a fire-and-forget Future)."""
    if _loop is None:
        raise RuntimeError("telegram bot not initialized: _loop is None")
    if _app is None:
        raise RuntimeError("telegram bot not initialized: _app is None")
    fut = asyncio.run_coroutine_threadsafe(push(text), _loop)
    try:
        fut.result(timeout=10)
    except Exception as e:
        raise RuntimeError(f"push send failed: {type(e).__name__}: {e}") from e


async def _post_init(application: Application):
    global _loop, _turn_lock
    _loop = asyncio.get_running_loop()
    _turn_lock = asyncio.Lock()
    # Register us as the global push transport. Anyone (scheduler firing a
    # reminder, outlook prompting for device-code re-auth, etc.) calls
    # push.push() and it lands in telegram.
    push_module.set_callback(push_threadsafe)
    # Now that the push channel is live, restore any pending reminders.
    scheduler.restore_pending()
    print(
        f"[telegram] ready. allowlisted user_id={_allowed_user_id()}",
        file=sys.stderr,
        flush=True,
    )


def main():
    global _app
    db.init()
    scheduler.start()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("error: TELEGRAM_BOT_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("TELEGRAM_USER_ID"):
        print("error: TELEGRAM_USER_ID not set in .env", file=sys.stderr)
        sys.exit(1)

    _app = Application.builder().token(token).post_init(_post_init).build()
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    _app.add_handler(CallbackQueryHandler(_on_callback))

    print("[telegram] starting polling...", file=sys.stderr, flush=True)
    try:
        _app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
