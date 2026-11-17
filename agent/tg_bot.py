#!/usr/bin/env python3
import os, time, glob, logging, asyncio
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

BASE = "/opt/agent"
QUEUE = os.path.join(BASE, "queue")
REPORTS = os.path.join(BASE, "reports")
TOKEN_FILE = os.path.join(BASE, "tgtoken")
CHAT_FILE = os.path.join(BASE, "tgchat")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg_bot")

def read_file(p):
    with open(p, "r") as f:
        return f.read().strip()

def allowed_chat():
    try:
        return int(read_file(CHAT_FILE))
    except Exception:
        return None

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    allowed = allowed_chat()
    if allowed is not None and chat_id != allowed:
        log.warning("Ignoring message from unauthorized chat %s", chat_id)
        return
    os.makedirs(QUEUE, exist_ok=True)
    ts = int(time.time())
    existing = glob.glob(os.path.join(QUEUE, "*_*.md"))
    nn = len(existing) + 1
    path = os.path.join(QUEUE, "%02d_%d.md" % (nn, ts))
    with open(path, "w") as f:
        f.write(update.message.text)
    log.info("Wrote task %s", path)
    await update.message.reply_text("Task queued: " + os.path.basename(path))

async def send_reports(app):
    os.makedirs(REPORTS, exist_ok=True)
    allowed = allowed_chat()
    if allowed is None:
        return
    for logf in sorted(glob.glob(os.path.join(REPORTS, "*.log"))):
        try:
            with open(logf, "r", errors="replace") as f:
                lines = f.readlines()[-20:]
            if lines:
                text = "[" + os.path.basename(logf) + "]\n" + "".join(lines)
                await app.bot.send_message(chat_id=allowed, text=text[:4000])
        except Exception as e:
            log.error("Failed sending %s: %s", logf, e)

async def report_loop(app):
    last = {}
    while True:
        await asyncio.sleep(30)
        for logf in glob.glob(os.path.join(REPORTS, "*.log")):
            try:
                m = os.path.getmtime(logf)
                if last.get(logf) != m:
                    last[logf] = m
                    await send_reports(app)
                    break
            except Exception:
                pass

async def post_init(app):
    asyncio.create_task(report_loop(app))

def main():
    token = read_file(TOKEN_FILE)
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    log.info("Starting bot")
    app.run_polling()

if __name__ == "__main__":
    main()
