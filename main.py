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
from telegram.error import Forbidden, ChatMigrated, BadRequest


# =========================================================
# CONFIG
# =========================================================

if os.getenv("RAILWAY_ENVIRONMENT") is None:
    from dotenv import load_dotenv
    load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

if not BOT_TOKEN or not MONGO_URI:
    raise Exception("BOT_TOKEN o MONGO_URI non configurati!")

OWNER_ID = os.getenv("OWNER_ID", 0)

if not OWNER_ID:
    raise Exception("OWNER_ID non configurato!")



# =========================================================
# DB
# =========================================================

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]

members_col = db["members"]
groups_col = db["groups"]  # 🔥 nuova collection


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
# TRACK BOT GROUPS (NUOVO)
# =========================================================

async def track_bot_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):

    result = update.my_chat_member
    if not result:
        return

    chat = update.effective_chat
    new_status = result.new_chat_member.status
    now = datetime.datetime.utcnow()

    # Bot attivo nel gruppo
    if new_status in ("member", "administrator"):
        groups_col.update_one(
            {"chat_id": chat.id},
            {
                "$set": {
                    "title": chat.title,
                    "type": chat.type,
                    "active": True,
                    "updated_at": now
                },
                "$setOnInsert": {
                    "added_at": now
                }
            },
            upsert=True
        )

        logger.info(f"✅ Bot attivo nel gruppo: {chat.title}")

    # Bot rimosso
    elif new_status in ("left", "kicked"):
        groups_col.update_one(
            {"chat_id": chat.id},
            {"$set": {"active": False, "updated_at": now}}
        )

        logger.info(f"❌ Bot rimosso dal gruppo: {chat.title}")


# =========================================================
# PERMESSI
# =========================================================

async def is_admin(update: Update) -> bool:
    member = await update.effective_chat.get_member(update.effective_user.id)
    return member.status in ("administrator", "creator")

async def is_owner(update: Update) -> bool:
    return update.effective_user.id == OWNER_ID


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

    await update.message.reply_text(f"🌍 Punti globali: {total}")


async def list_members(update: Update, context):

    members = members_col.find({"groups.0": {"$exists": True}}).sort("first_name", 1)

    msg = "<b>👥 Membri:</b>\n"

    for i, m in enumerate(members, 1):
        name = html.escape(m.get("first_name", "Utente"))
        msg += f"{i}. {name} — {m.get('total_points',0)} punti\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def list_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await is_admin(update):
        return await update.message.reply_text("Solo admin.")

    groups = list(groups_col.find({"active": True}).sort("title", 1))

    if not groups:
        return await update.message.reply_text("Nessun gruppo attivo registrato.")

    msg = "<b>📊 Gruppi attivi:</b>\n\n"

    for i, g in enumerate(groups, 1):

        # conta membri nel DB collegati a quel gruppo
        members_count = members_col.count_documents({
            "groups.chat_id": g["chat_id"]
        })

        msg += (
            f"{i}. <b>{html.escape(g.get('title','Senza titolo'))}</b>\n"
            f"   ID: <code>{g['chat_id']}</code>\n"
            f"   Tipo: {g.get('type','?')}\n"
            f"   Membri registrati: {members_count}\n\n"
        )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def register_group(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # Solo gruppi
    if update.effective_chat.type not in ("group", "supergroup"):
        return

    # Solo owner
    if update.effective_user.id != OWNER_ID:
        return

    chat = update.effective_chat
    now = datetime.datetime.utcnow()

    groups_col.update_one(
        {"chat_id": chat.id},
        {
            "$set": {
                "title": chat.title,
                "type": chat.type,
                "active": True,
                "updated_at": now
            },
            "$setOnInsert": {
                "added_at": now
            }
        },
        upsert=True
    )

    await update.message.reply_text("✅ Gruppo registrato nel database.")


# =========================================================
# TRACK MESSAGGI
# =========================================================

async def track_message(update: Update, context):
    if update.effective_user and update.effective_chat:
        add_or_update_member(update.effective_user, update.effective_chat)


# =========================================================
# CLEAN DB (ora usa groups_col)
# =========================================================

async def clean_inactive_members(app):

    await asyncio.sleep(120)

    while True:

        logger.info("🧹 Pulizia DB...")

        active_groups = list(groups_col.find({"active": True}))

        for member in list(members_col.find()):
            user_id = member["user_id"]

            for group in list(member.get("groups", [])):
                chat_id = group["chat_id"]

                # se il gruppo non è più attivo, rimuovilo subito
                if not any(g["chat_id"] == chat_id for g in active_groups):
                    members_col.update_one(
                        {"user_id": user_id},
                        {"$pull": {"groups": {"chat_id": chat_id}}}
                    )
                    continue

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

                except (Forbidden, BadRequest):
                    pass

        # ⚠️ cancellazione meno aggressiva
        members_col.delete_many({
            "total_points": 0,
            "groups.0": {"$exists": False},
            "created_at": {
                "$lte": datetime.datetime.utcnow() - datetime.timedelta(days=30)
            }
        })

        await asyncio.sleep(120)


# =========================================================
# AUTO TASK
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
                except (Forbidden, BadRequest, ChatMigrated):
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
    app.add_handler(CommandHandler("listgroups", list_groups))
    app.add_handler(CommandHandler("registergroup", register_group))


    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_message))

    # 🔥 tracking gruppi
    app.add_handler(ChatMemberHandler(track_bot_groups, ChatMemberHandler.MY_CHAT_MEMBER))

    async def post_init(app):
        app.create_task(clean_inactive_members(app))
        app.create_task(auto_tasks(app))

    app.post_init = post_init

    logger.info("🤖 Bot avviato")
    app.run_polling(drop_pending_updates=True)
