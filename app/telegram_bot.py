import asyncio
import os
import re
import sys

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from app import agent, db, scheduler


_app: Application | None = None
_loop: asyncio.AbstractEventLoop | None = None


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

    db.log_inbound(text)

    chat_id = update.effective_chat.id
    bot = context.bot
    loop = asyncio.get_running_loop()
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    def emit(reply: str):
        clean = strip_markdown(reply)
        db.log_outbound(clean)
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id=chat_id, text=clean), loop
        )

    try:
        await asyncio.to_thread(agent.handle, emit)
    except Exception as e:
        print(f"[telegram] agent error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        await bot.send_message(chat_id=chat_id, text=f"[error: {type(e).__name__}: {e}]")


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
    global _loop
    _loop = asyncio.get_running_loop()
    # Wire the scheduler's push channel to us so reminders go to telegram,
    # not stdout. Explicit callback registration > implicit module-attribute
    # lookup (the latter was silently failing).
    scheduler.set_push_callback(push_threadsafe)
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

    print("[telegram] starting polling...", file=sys.stderr, flush=True)
    try:
        _app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
