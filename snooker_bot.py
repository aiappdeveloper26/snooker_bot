"""
snooker_bot.py
--------------
Telegram bot for tracking snooker results against your friends.

Designed to run as a Render FREE "Web Service":
  - Includes a tiny built-in HTTP server (so Render sees a port being bound).
  - Data is stored in a single JSON file (snooker_data.json) - no SQL needed.

CAVEAT (same as your other Render free-tier bots): the free tier's disk is
temporary. Every time the service restarts or redeploys, snooker_data.json
resets to empty. This is fine for trying things out; if you want your
history to survive restarts long-term, the easiest free upgrade later is to
sync this JSON to a Google Sheet or a Gist - ask if you'd like that added.

Commands:
  /record    - record a new frame result (guided, button-based flow)
  /h2h       - head-to-head stats vs a friend
  /stats     - your overall stats
  /history   - your recent frame history
  /friends   - list saved friends
  /delfriend - remove a friend and all their records
  /undo      - delete the most recently recorded frame
  /cancel    - cancel current action

Environment variables:
  BOT_TOKEN  - your token from BotFather (required)
  PORT       - provided automatically by Render
  DATA_FILE  - optional override for the JSON storage path
"""

import json
import logging
import os
import re
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DATA_FILE = os.environ.get("DATA_FILE", "snooker_data.json")

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
(
    SELECT_FRIEND,
    NEW_FRIEND_NAME,
    SELECT_DATE,
    ENTER_DATE,
    SELECT_OPENER,
    ENTER_SCORE,
    ASK_BREAK,
    SELECT_BREAK_PLAYER,
    ENTER_BREAK_VALUE,
    ASK_MORE,
) = range(10)

CONFIRM_DELETE_FRIEND = 100


# ---------------------------------------------------------------------------
# Keep-alive web server (so Render's free web-service tier accepts the bot)
# ---------------------------------------------------------------------------

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Snooker bot is alive!")

    def log_message(self, *args):  # silence noisy request logs
        pass


def start_web_server():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()


# ---------------------------------------------------------------------------
# Tiny JSON "database"
#
# Shape of the data file:
# {
#   "<telegram_user_id>": {
#     "friends": ["Alice", "Bob"],
#     "frames": [
#        {"friend": "Alice", "date": "2026-06-13", "opener": "me",
#         "my_score": 2, "friend_score": 2,
#         "break_player": "me", "break_value": 45},
#        ...
#     ]
#   },
#   ...
# }
#
# Each entry in "frames" is actually a *session* result entered as a frame
# score (e.g. "2:2" means you won 2 frames and your friend won 2 frames in
# that session).
#
# Every Telegram user who talks to the bot gets their own "friends" list and
# "frames" history, keyed by their Telegram user id.
# ---------------------------------------------------------------------------

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_owner(data: dict, owner_id: int) -> dict:
    key = str(owner_id)
    if key not in data:
        data[key] = {"friends": [], "frames": []}
    return data[key]


def get_friends(data: dict, owner_id: int):
    return get_owner(data, owner_id)["friends"]


def get_or_create_friend(data: dict, owner_id: int, name: str) -> str:
    """Returns the canonical stored name (case-insensitive match), adding it if new."""
    name = name.strip()
    owner = get_owner(data, owner_id)
    for existing in owner["friends"]:
        if existing.lower() == name.lower():
            return existing
    owner["friends"].append(name)
    return name


def delete_friend(data: dict, owner_id: int, name: str) -> None:
    owner = get_owner(data, owner_id)
    owner["friends"] = [f for f in owner["friends"] if f.lower() != name.lower()]
    owner["frames"] = [fr for fr in owner["frames"] if fr["friend"].lower() != name.lower()]


def add_frame(data: dict, owner_id: int, frame: dict) -> None:
    get_owner(data, owner_id)["frames"].append(frame)


def delete_last_frame(data: dict, owner_id: int):
    frames = get_owner(data, owner_id)["frames"]
    if not frames:
        return None
    return frames.pop()


def _sorted_frames(frames, friend: str = None):
    """Most-recent-first: sort by date, then by original order (later = more recent)."""
    indexed = list(enumerate(frames))
    if friend is not None:
        indexed = [(i, f) for i, f in indexed if f["friend"].lower() == friend.lower()]
    indexed.sort(key=lambda pair: (pair[1]["date"], pair[0]), reverse=True)
    return [f for _, f in indexed]


def get_history(data: dict, owner_id: int, friend: str = None, limit: int = 10):
    frames = get_owner(data, owner_id)["frames"]
    return _sorted_frames(frames, friend)[:limit]


def get_h2h(data: dict, owner_id: int, friend: str) -> dict:
    sessions = [f for f in get_owner(data, owner_id)["frames"] if f["friend"].lower() == friend.lower()]
    sessions_count = len(sessions)

    me_wins = sum(f["my_score"] for f in sessions)
    friend_wins = sum(f["friend_score"] for f in sessions)
    total = me_wins + friend_wins

    opener_me_total = sum(f["my_score"] + f["friend_score"] for f in sessions if f["opener"] == "me")
    opener_me_wins = sum(f["my_score"] for f in sessions if f["opener"] == "me")
    opener_friend_total = total - opener_me_total
    opener_friend_wins = me_wins - opener_me_wins

    my_breaks = [f["break_value"] for f in sessions if f.get("break_player") == "me" and f.get("break_value") is not None]
    friend_breaks = [f["break_value"] for f in sessions if f.get("break_player") == "friend" and f.get("break_value") is not None]

    # Recent form: per-session result (W/L/D based on my_score vs friend_score)
    recent = _sorted_frames(sessions)[:5]
    recent_form = "".join(
        "W" if f["my_score"] > f["friend_score"] else ("L" if f["my_score"] < f["friend_score"] else "D")
        for f in recent
    )

    return {
        "friend_name": friend,
        "sessions": sessions_count,
        "total": total,
        "me_wins": me_wins,
        "friend_wins": friend_wins,
        "opener_me_total": opener_me_total,
        "opener_me_wins": opener_me_wins,
        "opener_friend_total": opener_friend_total,
        "opener_friend_wins": opener_friend_wins,
        "my_best_break": max(my_breaks) if my_breaks else None,
        "friend_best_break": max(friend_breaks) if friend_breaks else None,
        "recent_form": recent_form,
    }


def get_overall_stats(data: dict, owner_id: int) -> dict:
    sessions = get_owner(data, owner_id)["frames"]
    sessions_count = len(sessions)
    me_wins = sum(f["my_score"] for f in sessions)
    friend_wins = sum(f["friend_score"] for f in sessions)
    total = me_wins + friend_wins

    breakdown_map = {}
    for f in sessions:
        b = breakdown_map.setdefault(f["friend"], {"sessions": 0, "total": 0, "me_wins": 0})
        b["sessions"] += 1
        b["total"] += f["my_score"] + f["friend_score"]
        b["me_wins"] += f["my_score"]
    breakdown = [
        {
            "name": name,
            "sessions": v["sessions"],
            "total": v["total"],
            "me_wins": v["me_wins"],
            "friend_wins": v["total"] - v["me_wins"],
        }
        for name, v in sorted(breakdown_map.items(), key=lambda kv: kv[1]["total"], reverse=True)
    ]

    best_break = None
    for f in sessions:
        if f.get("break_value") is not None:
            if best_break is None or f["break_value"] > best_break["break_value"]:
                best_break = f

    return {
        "sessions": sessions_count,
        "total": total,
        "me_wins": me_wins,
        "friend_wins": friend_wins,
        "breakdown": breakdown,
        "best_break": best_break,
    }


# ---------------------------------------------------------------------------
# Helper keyboards
# ---------------------------------------------------------------------------

def friends_keyboard(owner_id: int, prefix: str, include_new: bool = False):
    data = load_data()
    friends = get_friends(data, owner_id)
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"{prefix}:{i}")] for i, name in enumerate(friends)
    ]
    if include_new:
        buttons.append([InlineKeyboardButton("\u2795 New friend", callback_data=f"{prefix}:new")])
    return InlineKeyboardMarkup(buttons) if buttons else None


def date_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("\U0001F4C5 Today", callback_data="date:today")],
            [InlineKeyboardButton("\u270F\uFE0F Enter a different date", callback_data="date:custom")],
        ]
    )


def yes_no_keyboard(prefix: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton("No", callback_data=f"{prefix}:no"),
            ]
        ]
    )


def two_choice_keyboard(prefix: str, friend_name: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Me", callback_data=f"{prefix}:me"),
                InlineKeyboardButton(friend_name, callback_data=f"{prefix}:friend"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# /start and /cancel
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "\U0001F3B1 *Snooker Tracker Bot*\n\n"
        "I'll help you keep track of your snooker results against your friends.\n\n"
        "*Commands:*\n"
        "/record - record a session's frame score (e.g. 2:2)\n"
        "/h2h - head-to-head stats vs a friend\n"
        "/stats - your overall stats\n"
        "/history - recent session history\n"
        "/friends - list saved friends\n"
        "/delfriend - remove a friend & their records\n"
        "/undo - delete the last recorded session\n"
        "/cancel - cancel current action"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    msg = "Cancelled. Nothing was saved."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg)
    else:
        await update.message.reply_text(msg)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /record conversation
# ---------------------------------------------------------------------------

async def record_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    owner_id = update.effective_user.id
    kb = friends_keyboard(owner_id, prefix="rf", include_new=True)
    if kb is None:
        await update.message.reply_text(
            "Let's record a result! \U0001F3B1\nYou don't have any friends saved yet - "
            "what's your opponent's name?"
        )
        return NEW_FRIEND_NAME
    await update.message.reply_text("Who did you play against?", reply_markup=kb)
    return SELECT_FRIEND


async def friend_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    owner_id = update.effective_user.id
    choice = query.data.split(":")[1]

    if choice == "new":
        await query.edit_message_text("What's your opponent's name?")
        return NEW_FRIEND_NAME

    data = load_data()
    friends = get_friends(data, owner_id)
    name = friends[int(choice)]
    context.user_data["friend_name"] = name
    await query.edit_message_text(
        f"Opponent: *{name}*\n\nWhat date was this frame played?",
        parse_mode="Markdown",
        reply_markup=date_keyboard(),
    )
    return SELECT_DATE


async def new_friend_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please type a valid name.")
        return NEW_FRIEND_NAME

    data = load_data()
    name = get_or_create_friend(data, owner_id, name)
    save_data(data)

    context.user_data["friend_name"] = name
    await update.message.reply_text(
        f"Opponent: *{name}*\n\nWhat date was this frame played?",
        parse_mode="Markdown",
        reply_markup=date_keyboard(),
    )
    return SELECT_DATE


async def date_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "date:today":
        context.user_data["match_date"] = date.today().isoformat()
        return await ask_opener(query, context)
    await query.edit_message_text("Please type the date in YYYY-MM-DD format (e.g. 2026-06-13):")
    return ENTER_DATE


async def date_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        await update.message.reply_text(
            "Hmm, that doesn't look right. Please use YYYY-MM-DD format (e.g. 2026-06-13):"
        )
        return ENTER_DATE
    context.user_data["match_date"] = parsed.isoformat()
    return await ask_opener(update, context)


async def ask_opener(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    friend_name = context.user_data["friend_name"]
    text = (
        f"Date: *{context.user_data['match_date']}*\n"
        f"Opponent: *{friend_name}*\n\n"
        "Who broke first (opened the first frame)?"
    )
    kb = two_choice_keyboard("opener", friend_name)
    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    return SELECT_OPENER


async def opener_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["opener"] = query.data.split(":")[1]  # "me" or "friend"

    friend_name = context.user_data["friend_name"]
    await query.edit_message_text(
        f"What was the frame score?\n"
        f"Reply in the format *your frames : {friend_name}'s frames*, e.g. `2:2`",
        parse_mode="Markdown",
    )
    return ENTER_SCORE


SCORE_PATTERN = re.compile(r"^\s*(\d+)\s*[:\-]\s*(\d+)\s*$")


async def score_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    match = SCORE_PATTERN.match(text)
    if not match:
        await update.message.reply_text(
            "Please enter the score as `your frames : their frames`, e.g. `2:2` or `3-1`.",
            parse_mode="Markdown",
        )
        return ENTER_SCORE

    my_score, friend_score = int(match.group(1)), int(match.group(2))
    if my_score == 0 and friend_score == 0:
        await update.message.reply_text(
            "At least one frame must have been played. Please enter a score like `2:2`.",
            parse_mode="Markdown",
        )
        return ENTER_SCORE

    context.user_data["my_score"] = my_score
    context.user_data["friend_score"] = friend_score

    await update.message.reply_text(
        "Was there a notable highest break in this session? (optional)",
        reply_markup=yes_no_keyboard("hasbreak"),
    )
    return ASK_BREAK


async def ask_break_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "hasbreak:no":
        context.user_data["break_player"] = None
        context.user_data["break_value"] = None
        return await save_frame_and_ask_more(query, context)

    friend_name = context.user_data["friend_name"]
    await query.edit_message_text(
        "Who scored the highest break?",
        reply_markup=two_choice_keyboard("breakplayer", friend_name),
    )
    return SELECT_BREAK_PLAYER


async def break_player_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["break_player"] = query.data.split(":")[1]
    await query.edit_message_text("What was the break value? (just type a number, e.g. 45)")
    return ENTER_BREAK_VALUE


async def break_value_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Please enter a whole number for the break, e.g. 45.")
        return ENTER_BREAK_VALUE
    value = int(text)
    if not (0 <= value <= 147):
        await update.message.reply_text("A break should be between 0 and 147. Try again:")
        return ENTER_BREAK_VALUE
    context.user_data["break_value"] = value
    return await save_frame_and_ask_more(update, context)


async def save_frame_and_ask_more(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    owner_id = (
        update_or_query.from_user.id
        if hasattr(update_or_query, "from_user")
        else update_or_query.effective_user.id
    )
    ud = context.user_data

    frame = {
        "friend": ud["friend_name"],
        "date": ud["match_date"],
        "opener": ud["opener"],
        "my_score": ud["my_score"],
        "friend_score": ud["friend_score"],
        "break_player": ud.get("break_player"),
        "break_value": ud.get("break_value"),
    }
    data = load_data()
    add_frame(data, owner_id, frame)
    save_data(data)

    summary = build_frame_summary(ud)

    # Reset per-session fields, keep friend & date for a possible next session
    for key in ("opener", "my_score", "friend_score", "break_player", "break_value"):
        ud.pop(key, None)

    text = f"\u2705 Result saved!\n\n{summary}\n\nAdd another session for the same date & opponent?"
    kb = yes_no_keyboard("more")

    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, reply_markup=kb)
    else:
        await update_or_query.message.reply_text(text, reply_markup=kb)
    return ASK_MORE


def build_frame_summary(ud: dict) -> str:
    friend_name = ud["friend_name"]
    opener = "You" if ud["opener"] == "me" else friend_name
    lines = [
        f"\U0001F4C5 Date: {ud['match_date']}",
        f"\U0001F19A Opponent: {friend_name}",
        f"\u25B6\uFE0F Opened: {opener}",
        f"\U0001F3C6 Score: You {ud['my_score']} - {ud['friend_score']} {friend_name}",
    ]
    if ud.get("break_value") is not None:
        bp = "You" if ud["break_player"] == "me" else friend_name
        lines.append(f"\U0001F4A5 Highest break: {ud['break_value']} ({bp})")
    return "\n".join(lines)


async def ask_more_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "more:yes":
        return await ask_opener(query, context)
    context.user_data.clear()
    await query.edit_message_text("All done! Use /h2h or /stats to see how things stack up. \U0001F3B1")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /h2h
# ---------------------------------------------------------------------------

async def h2h_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    kb = friends_keyboard(owner_id, prefix="h2h")
    if kb is None:
        await update.message.reply_text(
            "You don't have any recorded friends yet. Use /record to add your first match!"
        )
        return
    await update.message.reply_text("Head-to-head with whom?", reply_markup=kb)


async def h2h_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    owner_id = update.effective_user.id
    data = load_data()
    friends = get_friends(data, owner_id)
    friend = friends[int(query.data.split(":")[1])]
    stats = get_h2h(data, owner_id, friend)

    if stats["total"] == 0:
        await query.edit_message_text(f"No frames recorded yet against {stats['friend_name']}.")
        return

    win_pct = stats["me_wins"] / stats["total"] * 100

    lines = [
        f"\U0001F3B1 *Head-to-head vs {stats['friend_name']}*",
        "",
        f"Sessions played: {stats['sessions']}",
        f"Frames played: {stats['total']}",
        f"Your frame wins: {stats['me_wins']}",
        f"{stats['friend_name']}'s frame wins: {stats['friend_wins']}",
        f"Your win rate: {win_pct:.1f}%",
    ]

    if stats["recent_form"]:
        lines.append(f"Recent form (most recent first): {stats['recent_form']}")

    lines.append("")
    lines.append("*Break performance:*")
    if stats["opener_me_total"] > 0:
        pct = stats["opener_me_wins"] / stats["opener_me_total"] * 100
        lines.append(
            f"When you broke first: {stats['opener_me_wins']}/{stats['opener_me_total']} won ({pct:.0f}%)"
        )
    else:
        lines.append("When you broke first: no frames yet")

    if stats["opener_friend_total"] > 0:
        pct = stats["opener_friend_wins"] / stats["opener_friend_total"] * 100
        lines.append(
            f"When {stats['friend_name']} broke first: {stats['opener_friend_wins']}/{stats['opener_friend_total']} won by you ({pct:.0f}%)"
        )
    else:
        lines.append(f"When {stats['friend_name']} broke first: no frames yet")

    lines.append("")
    lines.append("*Highest breaks:*")
    lines.append(f"Yours: {stats['my_best_break'] if stats['my_best_break'] is not None else '-'}")
    lines.append(
        f"{stats['friend_name']}'s: {stats['friend_best_break'] if stats['friend_best_break'] is not None else '-'}"
    )

    await query.edit_message_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    data = load_data()
    stats = get_overall_stats(data, owner_id)

    if stats["total"] == 0:
        await update.message.reply_text(
            "No frames recorded yet. Use /record to log your first one!"
        )
        return

    win_pct = stats["me_wins"] / stats["total"] * 100
    lines = [
        "\U0001F4CA *Overall stats*",
        "",
        f"Sessions recorded: {stats['sessions']}",
        f"Total frames played: {stats['total']}",
        f"Your frame wins: {stats['me_wins']} ({win_pct:.1f}%)",
        f"Your frame losses: {stats['friend_wins']}",
        "",
        "*By opponent:*",
    ]
    for row in stats["breakdown"]:
        pct = row["me_wins"] / row["total"] * 100 if row["total"] else 0
        lines.append(
            f"\u2022 {row['name']}: {row['me_wins']}-{row['friend_wins']} "
            f"({pct:.0f}% frame win rate, {row['sessions']} sessions, {row['total']} frames)"
        )

    if stats["best_break"]:
        bb = stats["best_break"]
        who = "You" if bb["break_player"] == "me" else bb["friend"]
        lines.append("")
        lines.append(
            f"\U0001F4A5 *Highest break ever:* {bb['break_value']} by {who} "
            f"(vs {bb['friend']} on {bb['date']})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    data = load_data()
    rows = get_history(data, owner_id, limit=10)
    if not rows:
        await update.message.reply_text("No frames recorded yet. Use /record to log your first one!")
        return

    lines = ["\U0001F551 *Last 10 sessions:*", ""]
    for r in rows:
        opener = "You" if r["opener"] == "me" else r["friend"]
        line = f"{r['date']} vs {r['friend']}: You {r['my_score']} - {r['friend_score']} (opened: {opener})"
        if r.get("break_value") is not None:
            bp = "You" if r["break_player"] == "me" else r["friend"]
            line += f", break {r['break_value']} ({bp})"
        lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /friends
# ---------------------------------------------------------------------------

async def friends_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    data = load_data()
    friends = get_friends(data, owner_id)
    if not friends:
        await update.message.reply_text("No friends saved yet. Use /record to add one!")
        return
    lines = ["\U0001F465 *Your friends:*", ""]
    lines.extend(f"\u2022 {name}" for name in friends)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /delfriend
# ---------------------------------------------------------------------------

async def delfriend_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    kb = friends_keyboard(owner_id, prefix="delf")
    if kb is None:
        await update.message.reply_text("No friends saved yet.")
        return ConversationHandler.END
    await update.message.reply_text(
        "\u26A0\uFE0F Select a friend to remove. This will also delete *all* recorded frames against them.",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return CONFIRM_DELETE_FRIEND


async def delfriend_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    owner_id = update.effective_user.id
    data = load_data()
    friends = get_friends(data, owner_id)
    name = friends[int(query.data.split(":")[1])]
    delete_friend(data, owner_id, name)
    save_data(data)
    await query.edit_message_text(f"\U0001F5D1\uFE0F Removed {name} and all associated frame records.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /undo
# ---------------------------------------------------------------------------

async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = update.effective_user.id
    data = load_data()
    deleted = delete_last_frame(data, owner_id)
    if deleted is None:
        await update.message.reply_text("There's nothing to undo.")
        return
    save_data(data)
    await update.message.reply_text(
        f"\u21A9\uFE0F Removed the last session:\n"
        f"{deleted['date']} vs {deleted['friend']} - "
        f"You {deleted['my_score']} - {deleted['friend_score']} {deleted['friend']}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Please set the BOT_TOKEN environment variable to your Telegram bot token."
        )

    # Start the keep-alive web server in the background so Render's free
    # Web Service tier sees a bound port.
    threading.Thread(target=start_web_server, daemon=True).start()

    application = Application.builder().token(token).build()

    record_conv = ConversationHandler(
        entry_points=[CommandHandler("record", record_start)],
        states={
            SELECT_FRIEND: [CallbackQueryHandler(friend_selected, pattern=r"^rf:")],
            NEW_FRIEND_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_friend_name)],
            SELECT_DATE: [CallbackQueryHandler(date_selected, pattern=r"^date:")],
            ENTER_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_entered)],
            SELECT_OPENER: [CallbackQueryHandler(opener_selected, pattern=r"^opener:")],
            ENTER_SCORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, score_entered)],
            ASK_BREAK: [CallbackQueryHandler(ask_break_response, pattern=r"^hasbreak:")],
            SELECT_BREAK_PLAYER: [
                CallbackQueryHandler(break_player_selected, pattern=r"^breakplayer:")
            ],
            ENTER_BREAK_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, break_value_entered)
            ],
            ASK_MORE: [CallbackQueryHandler(ask_more_response, pattern=r"^more:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    delfriend_conv = ConversationHandler(
        entry_points=[CommandHandler("delfriend", delfriend_start)],
        states={
            CONFIRM_DELETE_FRIEND: [
                CallbackQueryHandler(delfriend_confirm, pattern=r"^delf:")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(record_conv)
    application.add_handler(delfriend_conv)
    application.add_handler(CommandHandler("h2h", h2h_start))
    application.add_handler(CallbackQueryHandler(h2h_callback, pattern=r"^h2h:"))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("friends", friends_command))
    application.add_handler(CommandHandler("undo", undo_command))

    logger.info("Snooker bot is running.")
    application.run_polling()


if __name__ == "__main__":
    main()
