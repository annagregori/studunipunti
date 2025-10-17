import logging
import time
import datetime
import html
import os
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
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

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Collezioni ---
users_col = db["users"]
warnings_col = db["warnings"]
groups_col = db["groups"]
members_col = db["members"]  # 🔥 nuova collezione unificata per tutti i gruppi

# --- Utility DB ---

def add_or_update_member(user, chat, points_delta=0):
    """Registra o aggiorna un membro a livello globale."""
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
        # Aggiorna info base utente
        members_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name
            }}
        )

        # Controlla se già nel gruppo
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
                {
                    "$push": {"groups": group_info},
                    "$inc": {"total_points": points_delta}
                }
            )
    else:
        # Nuovo utente globale
        members_col.insert_one({
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "groups": [group_info],
            "total_points": points_delta,
            "created_at": now
        })

def get_user_mention(user):
    name = html.escape(user.first_name or "Utente")
    return f"<a href='tg://user?id={user.id}'>{name}</a>"

# --- Eventi Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Ciao! Sto tracciando utenti e punti globalmente.")
    add_or_update_member(update.message.from_user, update.effective_chat)

async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    for member in update.message.new_chat_members:
        add_or_update_member(member, chat)
        await update.message.reply_text(
            f"👋 Benvenuto {html.escape(member.first_name)} in {chat.title}!",
            parse_mode=ParseMode.HTML
        )

async def track_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aggiorna il punteggio e la data ultimo messaggio di chi scrive."""
    user = update.effective_user
    chat = update.effective_chat
    add_or_update_member(user, chat, points_delta=1)

async def global_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra la classifica globale di tutti i gruppi."""
    top_members = list(members_col.find().sort("total_points", -1).limit(10))
    if not top_members:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>🏆 Classifica Globale Utenti</b>\n"
    for i, m in enumerate(top_members, start=1):
        name = html.escape(m.get("first_name", "Utente"))
        mention = f"<a href='tg://user?id={m['user_id']}'>{name}</a>"
        msg += f"{i}. {mention} — {m.get('total_points', 0)} punti\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def list_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista di tutti gli utenti conosciuti globalmente."""
    members = list(members_col.find().sort("first_name", 1))
    if not members:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>👥 Membri registrati globalmente:</b>\n"
    for i, m in enumerate(members, start=1):
        name = html.escape(m.get("first_name", "Utente"))
        mention = f"<a href='tg://user?id={m['user_id']}'>{name}</a>"
        msg += f"{i}. {mention} — {m.get('total_points', 0)} punti totali\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f'Update "{update}" caused error "{context.error}"')

async def punto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Assegna punti a un membro del gruppo"""
    if not await is_admin(update):
        await update.message.reply_text("Solo gli amministratori possono usare questo comando.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("Rispondi a un messaggio per assegnare punti a un utente.")
        return

    user = update.message.reply_to_message.from_user
    chat = update.effective_chat

    # Numero di punti (default 1)
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

async def global_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_members = list(db.members.find().sort("total_points", -1).limit(10))
    if not top_members:
        await update.message.reply_text("Nessun membro registrato.")
        return

    msg = "<b>🏆 Classifica Globale</b>\n"
    for i, m in enumerate(top_members, start=1):
        name = html.escape(m.get("first_name", "Utente"))
        mention = f"<a href='tg://user?id={m['user_id']}'>{name}</a>"
        msg += f"{i}. {mention} — {m.get('total_points', 0)} punti\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
# --- Avvio bot ---
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_messages))
    app.add_handler(CommandHandler("globalranking", global_ranking))
    app.add_handler(CommandHandler("listmembers", list_members))
    app.add_handler(CommandHandler("punto", punto))
    app.add_handler(CommandHandler("classifica", global_ranking))


    app.add_error_handler(error_handler)

    app.run_polling()

if __name__ == "__main__":
    main()


