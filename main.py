import logging
import asyncio
import datetime
import html
import os

from pymongo import MongoClient
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, ChatMemberHandler, filters
)
from telegram.error import Forbidden, ChatMigrated


# =========================================================
# CONFIG
# =========================================================

if os.getenv("RAILWAY_ENVIRONMENT") is None:
    from dotenv import load_dotenv
    load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", 0))

if not BOT_TOKEN or not MONGO_URI:
    raise Exception("BOT_TOKEN o MONGO_URI non configurati!")


# =========================================================
# DB
# =========================================================

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
members_col = db["members"]


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def add_or_update_member(user, chat, points_delta=0):
    if user.username == "GroupAnonymousBot":
        return

    now = datetime.datetime.utcnow()

    group_info = {
        "chat_id": chat.id,
        "title": chat.title or "Senza titolo",
        "joined_at": now,
        "points": max(0, points_delta),
        "last_message_at": now
    }

    member = members_col.find_one({"user_id": user.id})

    if not member:
        members_col.insert_one({
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "groups": [group_info],
            "total_points": points_delta,
            "created_at": now
        })
        return

    # update info base
    members_col.update_one(
        {"user_id": user.id},
        {"$set": {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name
        }}
    )

    existing = next((g for g in member.get("groups", []) if g["chat_id"] == chat.id), None)

    if existing:
        members_col.update_one(
            {"user_id": user.id, "groups.chat_id": chat.id},
            {
                "$inc": {"groups.$.points": points_delta, "total_points": points_delta},
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


# =========================================================
# PERMESSI
# =========================================================

async def is_admin(update: Update) -> bool:
    member = await update.effective_chat.get_member(update.effective_user.id)
    return member.status in ("administrator", "creator")


# =========================================================
# COMANDI
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot attivo.")


async def punto(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await is_admin(update):
        return await update.message.reply_text("Solo admin.")

    if not update.message.reply_to_message:
        return await update.message.reply_text("Rispondi a un messaggio.")

    user = update.message.reply_to_message.from_user
    chat = update.effective_chat

    points = int(context.args[0]) if context.args else 1

    add_or_update_member(user, chat, points)

    member = members_col.find_one({"user_id": user.id})
    total = member.get("total_points", 0)

    await update.message.reply_html(
        f"✅ {html.escape(user.first_name)} ora ha <b>{total}</b> punti"
    )


async def imieipunti(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_chat.type != "private":
        return await update.message.reply_text("Usa il comando in privato.")

    user = update.effective_user
    member = members_col.find_one({"user_id": user.id})

    total = member.get("total_points", 0) if member else 0

    await update.message.reply_text(
        f"🌍 Punti globali: {total}",
        parse_mode=ParseMode.HTML
    )


async def list_members(update: Update, context):

    members = members_col.find({"groups.0": {"$exists": True}}).sort("first_name", 1)

    msg = "<b>👥 Membri:</b>\n"

    for i, m in enumerate(members, 1):
        name = html.escape(m.get("first_name", "Utente"))
        msg += f"{i}. {name} — {m.get('total_points',0)} punti\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# =========================================================
# TRACK MESSAGGI
# =========================================================

async def track_message(update: Update, context):
    if update.effective_user and update.effective_chat:
        add_or_update_member(update.effective_user, update.effective_chat)


# =========================================================
# CLEAN DB (sicuro + ChatMigrated)
# =========================================================

async def clean_inactive_members(app):

    await asyncio.sleep(120)

    while True:

        logger.info("🧹 Pulizia DB...")

        for member in list(members_col.find()):
            user_id = member["user_id"]

            for group in list(member.get("groups", [])):
                chat_id = group["chat_id"]

                try:
                    cm = await app.bot.get_chat_member(chat_id, user_id)

                    if cm.status in ("left", "kicked"):
                        members_col.update_one(
                            {"user_id": user_id},
                            {"$pull": {"groups": {"chat_id": chat_id}}}
                        )

                except ChatMigrated as e:
                new_id = e.new_chat_id

                    members_col.update_many(
                        {"groups.chat_id": chat_id},
                        {"$set": {"groups.$.chat_id": new_id}}
                    )
                    logger.info(f"Gruppo migrato {chat_id} → {new_id}")

                except Forbidden:
                    pass

        # delete orfani
        members_col.delete_many({
            "total_points": 0,
            "groups": []
        })

        await asyncio.sleep(86400)


# =========================================================
# AUTO KICK 6 MESI
# =========================================================

async def auto_tasks(app):

    while True:

        logger.info("🔍 Auto kick 6 mesi...")

        six_months_ago = datetime.datetime.utcnow() - datetime.timedelta(days=180)

        users = members_col.find({
            "total_points": 0,
            "created_at": {"$lte": six_months_ago}
        })

        for user in users:

            user_id = user["user_id"]

            for g in user.get("groups", []):

                chat_id = g["chat_id"]

                try:
                    await app.bot.ban_chat_member(chat_id, user_id)
                    await app.bot.unban_chat_member(chat_id, user_id)

                except ChatMigrated as e:
                    new_id = e.new_chat_id
                    members_col.update_many(
                        {"groups.chat_id": chat_id},
                        {"$set": {"groups.$.chat_id": new_id}}
                    )

                except Forbidden:
                    pass

            members_col.delete_one({"user_id": user_id})

        await asyncio.sleep(86400)


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("punto", punto))
    app.add_handler(CommandHandler("imieipunti", imieipunti))
    app.add_handler(CommandHandler("listmembers", list_members))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_message))

    async def tasks(a):
        a.create_task(clean_inactive_members(a))
        a.create_task(auto_tasks(a))

    app.post_init = tasks

    logger.info("🤖 Bot avviato")
    app.run_polling()
