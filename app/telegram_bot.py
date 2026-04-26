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
# global lock is sufficient. The OAuth callback also acquires this when
# re-running the original prompt after auth completes.
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
    text = _MD_LINK_RE.sub(r"\1 (\2)", text)
    text = _MD_HEADER_RE.sub("", text)
    text = _MD_FENCED_CODE_RE.sub("", text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_BULLET_RE.sub(r"\1- ", text)
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


async def run_turn(text: str, chat_id: int | None = None) -> None:
    """Run one agent turn for `text` as if the user just sent it. Logs the
    inbound under the per-turn lock, runs the agent on a worker thread, and
    emits each agent reply back to Telegram with [CONFIRM] button rendering.

    Used by:
      - inbound message handler (`_on_message`),
      - inline-button callback handler (`_on_callback`, with synthetic text),
      - the OAuth callback (to replay the original prompt after auth completes).
    """
    if _turn_lock is None or _loop is None or _app is None:
        raise RuntimeError("telegram bot not initialized")
    if chat_id is None:
        chat_id = _allowed_user_id()
    bot = _app.bot
    emit = _make_emit(chat_id, bot, _loop)

    async with _turn_lock:
        db.log_inbound(text)
        try:
            await asyncio.to_thread(agent.handle, emit)
        except Exception as e:
            print(f"[telegram] agent error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            detail = f"{type(e).__name__}: {e}"
            if len(detail) > 3500:
                detail = detail[:3490] + "…"
            await bot.send_message(chat_id=chat_id, text=f"[error: {detail}]")


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
    # TYPING outside the lock so the user sees activity even if a previous
    # turn is still running.
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    await run_turn(text, chat_id=chat_id)


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
    await run_turn(synthetic, chat_id=chat_id)


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


def schedule_on_loop(coro):
    """Schedule a coroutine on the bot's event loop from any thread.
    Returns a concurrent.futures.Future."""
    if _loop is None:
        raise RuntimeError("telegram bot not initialized: _loop is None")
    return asyncio.run_coroutine_threadsafe(coro, _loop)


async def _serve(application: Application):
    """Run the Telegram polling AND the OAuth HTTP server on the same event
    loop. Either coroutine returning ends the process (uvicorn.serve() blocks
    forever in normal operation; if it crashes we want the supervisor to
    restart us)."""
    global _loop, _turn_lock
    _loop = asyncio.get_running_loop()
    _turn_lock = asyncio.Lock()
    push_module.set_callback(push_threadsafe)
    scheduler.restore_pending()

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    print(
        f"[telegram] polling started. allowlisted user_id={_allowed_user_id()}",
        file=sys.stderr,
        flush=True,
    )

    # Start the OAuth callback server only if a public URL is configured.
    # Local CLI dev / no-domain deploys keep the device-code fallback and
    # don't need an HTTP listener.
    if os.environ.get("SURI_PUBLIC_URL"):
        # Imported lazily so dev environments without uvicorn/fastapi still
        # work for the device-code path.
        from app import oauth_server
        import uvicorn

        port = int(os.environ.get("PORT", "8080"))
        config = uvicorn.Config(
            oauth_server.app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            # Keep access logs ON. The OAuth callback path is rare (≤ daily)
            # and being able to see "did Microsoft's redirect actually reach us"
            # in the logs is worth the noise.
            access_log=True,
        )
        server = uvicorn.Server(config)
        print(f"[oauth] serving on 0.0.0.0:{port}", file=sys.stderr, flush=True)
        try:
            await server.serve()
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
    else:
        print(
            "[oauth] SURI_PUBLIC_URL not set — falling back to device-code "
            "for Outlook auth. polling forever.",
            file=sys.stderr,
            flush=True,
        )
        try:
            await asyncio.Event().wait()  # block forever
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()


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

    _app = Application.builder().token(token).build()
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    _app.add_handler(CallbackQueryHandler(_on_callback))

    print("[telegram] starting...", file=sys.stderr, flush=True)
    try:
        asyncio.run(_serve(_app))
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
