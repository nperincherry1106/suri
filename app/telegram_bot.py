import asyncio
import os
import re
import sys

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

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


def _emitter(chat_id: int, loop: asyncio.AbstractEventLoop):
    """Build an emit callback the agent can invoke from a worker thread."""
    if _app is None:
        raise RuntimeError("telegram bot not initialized")
    bot = _app.bot

    def emit(reply: str):
        clean = strip_markdown(reply)
        db.log_outbound(clean)
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id=chat_id, text=clean), loop
        )

    return emit


async def run_turn(text: str, chat_id: int | None = None) -> None:
    """Run one agent turn for `text` as if the user just sent it. Logs the
    inbound, takes the per-turn lock, runs the agent on a worker thread, and
    emits each agent reply back to Telegram. Used by both the inbound message
    handler and the OAuth callback (to replay the original prompt after auth)."""
    if _turn_lock is None or _loop is None or _app is None:
        raise RuntimeError("telegram bot not initialized")
    if chat_id is None:
        chat_id = _allowed_user_id()

    async with _turn_lock:
        db.log_inbound(text)
        emit = _emitter(chat_id, _loop)
        try:
            await asyncio.to_thread(agent.handle, emit)
        except Exception as e:
            print(f"[telegram] agent error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            await _app.bot.send_message(
                chat_id=chat_id, text=f"[error: {type(e).__name__}: {e}]"
            )


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
    await run_turn(text, chat_id=chat_id)


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
            access_log=False,
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

    print("[telegram] starting...", file=sys.stderr, flush=True)
    try:
        asyncio.run(_serve(_app))
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
