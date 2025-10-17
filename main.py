import logging
import datetime
import html
import os
import asyncio
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# --- Carica dotenv solo in locale ---
if os.getenv("RAILWAY_ENVIRONMENT") is None:
    from dotenv import load_dotenv
    load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

if not BOT_TOKEN or not MONGO_URI:
    raise Exception("BOT_TOKEN o MONGO_URI non configurati!")

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
members_col = db["members"]

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Gestione utenti ---
def add_or_update_member(user, chat, points_delta=0):
    """Registra o aggiorna un membro e aggiorna la data dell'ultimo messaggio."""
    member = members_col.find_one({"user_id": user.id})
    now = datetime.datetime.utcnow()

    group_info = {
        "chat_id": chat.id,
        "title": chat.title,
        "joined_at": now,
        "points": max(0, points_delta),
        "last_message_at": now
    }

    if member:
        members_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name
            }}
        )

        existing_group = next((g for g in member.get("groups", []) if g["chat_id"] == chat.id), None)
        if existing_group:
            members_col.update_one(
                {"user_id": user.id, "groups.chat_id": chat.id},
                {
                    "$inc": {"groups.$.points": points_delta, "total_points": points_delta},
                    "$set": {"groups.$.last_message_at": now, "groups.$.title": chat.title}
                }
            )
        else:
            members_col.update_one(
                {"user_id": user.id},
                {"$push": {"groups": group_info}, "$inc": {"total_points": points_delta}}
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

# --- Eventi Telegram ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_or_update_member(update.message.from_user, update.effective_chat)
    await update.message.reply_text("🤖 Bot attivo! Sto monitorando la tua attività.")

async def punto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Assegna punti a un membro del gruppo"""
    if not update.message.reply_to_message:
        await update.message.reply_text("Rispondi a un messaggio per assegnare punti a un utente.")
        return

    user = update.message.reply_to_message.from_user
    chat = update.effective_chat
    points = 1
    if context.args and context.args[0].isdigit():
        points = int(context.args[0])
    add_or_update_member(user, chat, points_delta=points)

    member = db.members.find_one({"user_id": user.id})
    total = member.get("total_points", 0)

    await update.message.reply_html(
        f"✅ <b>{html.escape(user.first_name)}</b> ha ricevuto <b>{points}</b> punti!\n"
        f"Totale globale: <b>{total}</b> punti."
    )

# --- BAN AUTOMATICO OGNI 6 MESI DI INATTIVITÀ ---
async def auto_ban_zero_points(app):
    """Ogni giorno banna chi ha 0 punti e non ha interagito da 6 mesi."""
    while True:
        now = datetime.datetime.utcnow()
        six_months_ago = now - datetime.timedelta(days=180)

        logger.info("🔁 Controllo utenti inattivi da oltre 6 mesi...")

        # Seleziona solo utenti inattivi da oltre 6 mesi
        zero_users = list(members_col.find({
            "total_points": 0,
            "groups.last_message_at": {"$lt": six_months_ago}
        }))

        if not zero_users:
            logger.info("✅ Nessun utente inattivo da bannare.")
        else:
            for u in zero_users:
                user_id = u["user_id"]

                for g in u.get("groups", []):
                    chat_id = g["chat_id"]
                    last_msg = g.get("last_message_at")
                    if not last_msg or last_msg > six_months_ago:
                        continue  # è attivo, salta

                    try:
                        # Controlla se è admin prima di bannare
                        admins = await app.bot.get_chat_administrators(chat_id)
                        admin_ids = [a.user.id for a in admins]
                        if user_id in admin_ids:
                            logger.info(f"⏭️ {user_id} è admin di {chat_id}, salto il ban.")
                            continue

                        await app.bot.ban_chat_member(chat_id, user_id)
                        logger.info(f"🚫 Bannato utente {user_id} da chat {chat_id}")
                    except Exception as e:
                        logger.warning(f"⚠️ Errore nel ban di {user_id} da {chat_id}: {e}")

        await asyncio.sleep(86400)  # Ricontrolla ogni 24h

# --- Avvio Bot ---
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("punto", punto))

    async def on_startup(app):
        app.create_task(auto_ban_zero_points(app))
        logger.info("✅ Task auto_ban_zero_points avviato.")

    app.post_init = on_startup
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
