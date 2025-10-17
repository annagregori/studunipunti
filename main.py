import logging
import asyncio
import datetime
import html
import os
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, ChatMemberHandler
)
from telegram.error import Forbidden

# --- Carica dotenv solo in locale ---
if os.getenv("RAILWAY_ENVIRONMENT") is None:
    from dotenv import load_dotenv
    load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID")  # ID della chat dove inviare notifiche

if not BOT_TOKEN or not MONGO_URI:
    raise Exception("BOT_TOKEN o MONGO_URI non configurati!")

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

members_col = db["members"]

# --- Utility DB ---
def add_or_update_member(user, chat, points_delta=0):
    now = datetime.datetime.utcnow()
    member = members_col.find_one({"user_id": user.id})

    group_info = {
        "chat_id": chat.id,
        "title": chat.title if hasattr(chat, "title") else "",
        "joined_at": now,
        "points": max(0, points_delta),
        "last_message_at": now
    }

    if member:
        members_col.update_one(
            {"user_id": user.id},
            {
                "$set": {
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name
                }
            }
        )
        existing_group = next(
            (g for g in member.get("groups", []) if g["chat_id"] == chat.id),
            None
        )
        if existing_group:
            members_col.update_one(
                {"user_id": user.id, "groups.chat_id": chat.id},
                {
                    "$inc": {
                        "groups.$.points": points_delta,
                        "total_points": points_delta
                    },
                    "$set": {"groups.$.last_message_at": now}
                }
            )
        else:
            members_col.update_one(
                {"user_id": user.id},
                {
                    "$push": {"groups": group_info},
                    "$inc": {"total_points": points_delta}
                }
            )
    else:
        members_col.insert_one({
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "groups": [group_info],
            "total_points": points_delta,
            "created_at": now
        })

# --- Controllo admin ---
async def is_admin(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    try:
        member = await chat.get_member(user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# --- Comandi Telegram ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_or_update_member(update.message.from_user, update.effective_chat)
    await update.message.reply_text("🤖 Ciao! Sto tracciando utenti e punti globalmente.")

async def punto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("Solo gli amministratori possono assegnare punti.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("Rispondi a un messaggio per assegnare punti.")
        return

    user = update.message.reply_to_message.from_user
    chat = update.effective_chat
    points = 1
    if context.args and context.args[0].isdigit():
        points = int(context.args[0])

    add_or_update_member(user, chat, points_delta=points)
    member = members_col.find_one({"user_id": user.id})
    total = member.get("total_points", 0)

    await update.message.reply_html(
        f"✅ {html.escape(user.first_name)} ha ricevuto <b>{points}</b> punti!\n"
        f"Totale globale: <b>{total}</b> punti."
    )

    # Notifica nella chat di log
    if LOG_CHAT_ID:
        await context.bot.send_message(
            chat_id=int(LOG_CHAT_ID),
            text=f"🏅 {user.first_name} ha ricevuto {points} punti in {chat.title if chat.title else 'chat privata'}."
        )

async def global_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = list(members_col.find().sort("total_points", -1).limit(10))
    if not top:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>🏆 Classifica Globale</b>\n"
    for i, m in enumerate(top, start=1):
        name = html.escape(m.get("first_name", "Utente"))
        msg += f"{i}. <a href='tg://user?id={m['user_id']}'>{name}</a> — {m.get('total_points', 0)} punti\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def list_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    members = list(members_col.find().sort("first_name", 1))
    if not members:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>👥 Membri registrati globalmente:</b>\n"
    for i, m in enumerate(members, start=1):
        name = html.escape(m.get("first_name", "Utente"))
        msg += f"{i}. <a href='tg://user?id={m['user_id']}'>{name}</a> — {m.get('total_points', 0)} punti\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# --- AUTO BAN ---
async def auto_ban_zero_points(app):
    while True:
        logger.info("🔁 Controllo utenti con 0 punti registrati da oltre 6 mesi...")
        now = datetime.datetime.utcnow()
        six_months_ago = now - datetime.timedelta(days=180)

        users = list(members_col.find({
            "total_points": 0,
            "created_at": {"$lte": six_months_ago}
        }))

        for user in users:
            user_id = user["user_id"]
            for g in user.get("groups", []):
                chat_id = g["chat_id"]
                try:
                    member = await app.bot.get_chat_member(chat_id, user_id)
                    if member.status in ("administrator", "creator"):
                        continue
                    await app.bot.ban_chat_member(chat_id, user_id)
                    if LOG_CHAT_ID:
                        await app.bot.send_message(
                            chat_id=int(LOG_CHAT_ID),
                            text=f"🚫 Bannato {user_id} da {chat_id} (0 punti da 6 mesi)"
                        )
                except Forbidden:
                    logger.warning(f"❌ Non ho permessi per bannare in {chat_id}")
                except Exception as e:
                    logger.error(f"Errore durante ban di {user_id}: {e}")

        await asyncio.sleep(86400)  # Controllo una volta al giorno

# --- Traccia tutti i messaggi ---
async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    add_or_update_member(user, chat)

# --- Gestione uscita membri ---
async def member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = update.chat_member
    old_status = chat_member.old_chat_member.status
    new_status = chat_member.new_chat_member.status
    user = chat_member.from_user
    chat = update.effective_chat

    if old_status not in ("left", "kicked") and new_status in ("left", "kicked"):
        # Rimuovi membro dal DB
        members_col.update_one(
            {"user_id": user.id},
            {"$pull": {"groups": {"chat_id": chat.id}}}
        )
        if LOG_CHAT_ID:
            await context.bot.send_message(
                chat_id=int(LOG_CHAT_ID),
                text=f"❌ {user.first_name} è uscito o stato rimosso da {chat.title}."
            )

# --- MAIN ---
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Comandi
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("globalranking", global_ranking))
    app.add_handler(CommandHandler("listmembers", list_members))
    app.add_handler(CommandHandler("punto", punto))
    app.add_handler(CommandHandler("classifica", global_ranking))
    app.add_error_handler(error_handler)

    # Tracciamento automatico messaggi
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_message))

    # Tracciamento uscita membri
    app.add_handler(ChatMemberHandler(member_update, ChatMemberHandler.CHAT_MEMBER))

    # Task auto-ban
    app.create_task(auto_ban_zero_points(app))
    logger.info("✅ Task auto_ban_zero_points avviato correttamente.")

    logger.info("🤖 Bot avviato e in ascolto...")
    app.run_polling()

if __name__ == "__main__":
    main()
