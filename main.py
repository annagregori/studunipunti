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
# =====================================

