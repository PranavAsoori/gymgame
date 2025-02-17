import logging
import os
import random
import datetime
import pytz
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    ConversationHandler
)
from pymongo import MongoClient

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

# Load environment variables
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")

# Database setup
client = MongoClient(MONGO_URI)
db = client.gym_game
users_collection = db.users
games_collection = db.games

# Logging
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Conversation states (3 total)
SELECT_MODE, SET_DURATION, CONFIRM_PENALTIES = range(3)

# Scheduler for daily summaries
scheduler = BackgroundScheduler()


def get_display_name(user_obj):
    """Fallback logic for generating a display name."""
    if user_obj.username:
        return user_obj.username
    elif user_obj.first_name and user_obj.last_name:
        return f"{user_obj.first_name} {user_obj.last_name}"
    elif user_obj.first_name:
        return user_obj.first_name
    else:
        return "Unknown User"


def start(update: Update, context: CallbackContext):
    """Command: /start — Initiates a new game or rejects if there's already one active."""
    active_game = games_collection.find_one({"active": True})
    if active_game:
        update.message.reply_text(
            "A game has already been started. Please wait until the current game is ended."
        )
        return ConversationHandler.END

    # Store the initiator's user_id so only they can continue the setup
    context.user_data["initiator_id"] = update.message.from_user.id

    update.message.reply_text(
        "Welcome to Gym Game Bot!\n"
        "Choose a game mode:\n 1. Individual\n 2. Team",
        reply_markup=ReplyKeyboardMarkup([["Individual", "Team"]], one_time_keyboard=True)
    )
    return SELECT_MODE


def select_mode(update: Update, context: CallbackContext):
    """User chooses 'Individual' or 'Team'. Randomly assign the initiator if 'Team'."""
    # Ensure only the /start initiator can proceed
    if update.message.from_user.id != context.user_data.get("initiator_id"):
        update.message.reply_text("Only the user who started the game can proceed with the setup.")
        return SELECT_MODE

    mode = update.message.text
    context.user_data["mode"] = mode

    if mode == "Team":
        # Initialize empty teams in context
        context.user_data["team_1"] = []
        context.user_data["team_2"] = []

        initiator_name = get_display_name(update.message.from_user)
        # Random assignment for the initiator
        if random.choice([True, False]):
            context.user_data["team_1"].append(initiator_name)
            update.message.reply_text(f"You have been assigned to Team A, {initiator_name}.")
        else:
            context.user_data["team_2"].append(initiator_name)
            update.message.reply_text(f"You have been assigned to Team B, {initiator_name}.")

    # Prompt for game duration (works for both Team/Individual)
    update.message.reply_text(
        "How long should the game last? (1 week, 2 weeks, 1 month)",
        reply_markup=ReplyKeyboardMarkup([["1 week", "2 weeks", "1 month"]], one_time_keyboard=True)
    )
    return SET_DURATION


def set_duration(update: Update, context: CallbackContext):
    """User chooses the duration of the game."""
    if update.message.from_user.id != context.user_data.get("initiator_id"):
        update.message.reply_text("Only the user who started the game can set the duration.")
        return SET_DURATION

    duration = update.message.text
    context.user_data["duration"] = duration

    update.message.reply_text(
        "Enable penalty mode? (Yes/No)",
        reply_markup=ReplyKeyboardMarkup([["Yes", "No"]], one_time_keyboard=True)
    )
    return CONFIRM_PENALTIES


def confirm_penalties(update: Update, context: CallbackContext):
    """User confirms penalties and the game officially starts."""
    if update.message.from_user.id != context.user_data.get("initiator_id"):
        update.message.reply_text("Only the user who started the game can confirm penalties.")
        return CONFIRM_PENALTIES

    penalties = (update.message.text == "Yes")
    context.user_data["penalties"] = penalties

    game_id = str(datetime.datetime.now().timestamp())
    game_data = {
        "_id": game_id,
        "mode": context.user_data["mode"],
        "duration": context.user_data["duration"],
        "penalties": context.user_data["penalties"],
        "team_1": context.user_data.get("team_1", []),
        "team_2": context.user_data.get("team_2", []),
        "scores": {},
        "active": True,
        "day": 1,
        "start_date": datetime.datetime.now(pytz.UTC)
    }

    # Insert the new game, reset all user data
    games_collection.insert_one(game_data)
    users_collection.update_many(
        {},
        {"$set": {"points": 0, "streak": 0, "last_claim": None}}
    )

    if context.user_data["mode"] == "Team":
        msg = (
            "Game started in Team mode!\n"
            f"Team A: {', '.join(game_data['team_1']) if game_data['team_1'] else 'None'}\n"
            f"Team B: {', '.join(game_data['team_2']) if game_data['team_2'] else 'None'}\n"
            "Anyone who wants to join can type /join to be assigned a team."
        )
    else:
        msg = "Game started in Individual mode! Use /claim to log workouts."

    update.message.reply_text(msg)
    return ConversationHandler.END


def claim(update: Update, context: CallbackContext):
    """Command: /claim — Users claim daily points (with streak logic)."""
    from datetime import date, timedelta

    user_obj = update.message.from_user
    user_id = user_obj.id

    display_name = get_display_name(user_obj)
    user = users_collection.find_one({"user_id": user_id})

    # If user doesn't exist, create them
    if not user:
        users_collection.insert_one({
            "user_id": user_id,
            "display_name": display_name,
            "points": 0,
            "streak": 0,
            "last_claim": None
        })
        user = users_collection.find_one({"user_id": user_id})
    else:
        # Update display_name if changed
        if user.get("display_name") != display_name:
            users_collection.update_one(
                {"user_id": user_id},
                {"$set": {"display_name": display_name}}
            )

    today = date.today()
    # Check if user has already claimed
    if user.get("last_claim") == str(today):
        update.message.reply_text("You already claimed points today!")
        return

    # Streak logic
    yesterday = today - timedelta(days=1)
    if user.get("last_claim") == str(yesterday):
        streak = user.get("streak", 0) + 1
    else:
        streak = 1

    # Points logic (+1, plus bonus if 7-day streak)
    new_points = user.get("points", 0) + 1
    if streak % 7 == 0:
        new_points += 3  # 7-day bonus

    users_collection.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "points": new_points,
                "streak": streak,
                "last_claim": str(today)
            }
        }
    )

    update.message.reply_text(
        f"✅ {display_name} claimed 1 point. "
        f"Current streak: {streak} days. Total points: {new_points}."
    )


def leaderboard(update: Update, context: CallbackContext):
    """Command: /leaderboard — Displays the current leaderboard (Team or Individual)."""
    active_game = games_collection.find_one({"active": True})
    # If no active game, try the most recent one
    if not active_game:
        active_game = games_collection.find_one(sort=[("start_date", -1)])
        if not active_game:
            update.message.reply_text("No game found.")
            return

    game_mode = active_game.get("mode", "Individual")

    if game_mode == "Individual":
        # Sort all users by points desc
        users = users_collection.find().sort("points", -1)
        message = "🏆 **Leaderboard** 🏆\n\n"
        for i, user_doc in enumerate(users, start=1):
            display = user_doc.get("display_name", "Unknown User")
            points = user_doc.get("points", 0)
            message += f"{i}. {display} - {points} points\n"

    elif game_mode == "Team":
        team_1_members = active_game.get("team_1", [])
        team_2_members = active_game.get("team_2", [])

        team_1_users = list(users_collection.find({"display_name": {"$in": team_1_members}}))
        team_2_users = list(users_collection.find({"display_name": {"$in": team_2_members}}))

        team_1_points = sum(u.get("points", 0) for u in team_1_users)
        team_2_points = sum(u.get("points", 0) for u in team_2_users)

        team_1_message = "Team A\n"
        for u in team_1_users:
            display = u.get("display_name", "Unknown User")
            pts = u.get("points", 0)
            team_1_message += f"- {display} - {pts} points\n"

        team_2_message = "Team B\n"
        for u in team_2_users:
            display = u.get("display_name", "Unknown User")
            pts = u.get("points", 0)
            team_2_message += f"- {display} - {pts} points\n"

        if team_1_points > team_2_points:
            message = (
                "🏆 **Leaderboard** 🏆\n\n"
                f"{team_1_message}\n"
                f"{team_2_message}\n"
                f"Team A is in the lead with {team_1_points} points."
            )
        elif team_2_points > team_1_points:
            message = (
                "🏆 **Leaderboard** 🏆\n\n"
                f"{team_1_message}\n"
                f"{team_2_message}\n"
                f"Team B is in the lead with {team_2_points} points."
            )
        else:
            message = (
                "🏆 **Leaderboard** 🏆\n\n"
                f"{team_1_message}\n"
                f"{team_2_message}\n"
                "Both teams are tied."
            )
    else:
        message = "No valid game mode found."

    update.message.reply_text(message)


def admin_override(update: Update, context: CallbackContext):
    """Admin command: /bot add|remove @Name <points> — Manually adjusts user points."""
    if update.message.from_user.id != ADMIN_ID:
        update.message.reply_text("❌ You are not authorized to use this command.")
        return

    try:
        # Format: /bot add @Alice 5
        _, action, display_name, value_str = update.message.text.split()
        value = int(value_str)

        display_name = display_name.lstrip('@')  # remove '@' if present
        user = users_collection.find_one({"display_name": display_name})
        logging.info(f"Searching for user: {display_name}, Found: {user}")

        if not user:
            update.message.reply_text("❌ User not found.")
            return

        if action == "add":
            users_collection.update_one(
                {"display_name": display_name},
                {"$inc": {"points": value}}
            )
            logging.info(f"Added {value} points to {display_name}")
            update.message.reply_text(f"✅ Added {value} points to {display_name}.")
        elif action == "remove":
            users_collection.update_one(
                {"display_name": display_name},
                {"$inc": {"points": -value}}
            )
            logging.info(f"Removed {value} points from {display_name}")
            update.message.reply_text(f"✅ Removed {value} points from {display_name}.")
        else:
            update.message.reply_text("❌ Invalid action. Use 'add' or 'remove'.")

    except Exception as e:
        logging.error(f"Error in admin_override: {e}")
        update.message.reply_text(
            "❌ Invalid command. Use `/bot add @Name points` or `/bot remove @Name points`."
        )


def end_game(update: Update, context: CallbackContext):
    """Admin command: /endgame — Ends the active game, shows final leaderboard, wipes DB."""
    if update.message.from_user.id != ADMIN_ID:
        update.message.reply_text("❌ You are not authorized to use this command.")
        return

    active_game = games_collection.find_one({"active": True})
    if not active_game:
        update.message.reply_text("No active game to end.")
        return

    # Show final leaderboard
    leaderboard(update, context)

    games_collection.update_one({"_id": active_game["_id"]}, {"$set": {"active": False}})
    update.message.reply_text("Game Ended.")

    # Wipe user and game data
    users_collection.delete_many({})
    games_collection.delete_many({})


def reset(update: Update, context: CallbackContext):
    """Admin command: /reset — Resets all user points/streaks without deleting user docs."""
    if update.message.from_user.id != ADMIN_ID:
        update.message.reply_text("❌ You are not authorized to use this command.")
        return

    users_collection.update_many({}, {"$set": {"points": 0, "streak": 0, "last_claim": None}})
    update.message.reply_text("All user points and streaks have been reset.")


def list_users(update: Update, context: CallbackContext):
    """Admin command: /listusers — Lists all users (display_name, user_id, points, streak)."""
    if update.message.from_user.id != ADMIN_ID:
        update.message.reply_text("❌ You are not authorized to use this command.")
        return

    users = users_collection.find()
    message = "Users in the database:\n"
    for user_doc in users:
        display = user_doc.get("display_name", "Unknown User")
        user_id = user_doc.get("user_id", "N/A")
        points = user_doc.get("points", 0)
        streak = user_doc.get("streak", 0)
        message += f"- {display} (ID: {user_id}) - Points: {points}, Streak: {streak}\n"

    update.message.reply_text(message)


def set_points(update: Update, context: CallbackContext):
    """Admin command: /setpoints @Name <points> — Sets exact points for a user."""
    if update.message.from_user.id != ADMIN_ID:
        update.message.reply_text("❌ You are not authorized to use this command.")
        return

    try:
        # Format: /setpoints @Alice 20
        _, display_name, points_str = update.message.text.split()
        points = int(points_str)

        display_name = display_name.lstrip('@')
        user = users_collection.find_one({"display_name": display_name})
        logging.info(f"Searching for user: {display_name}, Found: {user}")

        if not user:
            update.message.reply_text("❌ User not found.")
            return

        users_collection.update_one(
            {"display_name": display_name},
            {"$set": {"points": points}}
        )
        logging.info(f"Set {points} points for {display_name}")
        update.message.reply_text(f"✅ Set {points} points for {display_name}.")

    except Exception as e:
        logging.error(f"Error in set_points: {e}")
        update.message.reply_text("❌ Invalid command. Use `/setpoints @Name points`.")


def end_day(update: Update, context: CallbackContext):
    """Admin command: /endday — Ends the current day, increments day, resets streaks, checks if game ends."""
    if update.message.from_user.id != ADMIN_ID:
        update.message.reply_text("❌ You are not authorized to use this command.")
        return

    active_game = games_collection.find_one({"active": True})
    if not active_game:
        update.message.reply_text("No active game found.")
        return

    current_day = active_game.get("day", 1)
    next_day = current_day + 1

    games_collection.update_one(
        {"_id": active_game["_id"]},
        {"$set": {"day": next_day}}
    )
    logging.info(f"Day updated from {current_day} to {next_day}")

    # Reset daily streaks
    users_collection.update_many(
        {},
        {"$set": {"streak": 0, "last_claim": None}}
    )
    logging.info("Daily streaks and last_claim reset for all users.")

    # Check if the game ends
    game_duration = active_game.get("duration", "1 week")
    if game_duration == "1 week" and next_day > 7:
        end_game(update, context)
        return
    elif game_duration == "2 weeks" and next_day > 14:
        end_game(update, context)
        return
    elif game_duration == "1 month" and next_day > 30:
        end_game(update, context)
        return

    update.message.reply_text(f"Day {current_day} ended. Starting Day {next_day}.")
    daily_summary(context)


def daily_summary(context: CallbackContext):
    """Sends a daily leaderboard summary to the GROUP_CHAT_ID."""
    active_game = games_collection.find_one({"active": True})
    if not active_game:
        logging.info("No active game found for daily summary.")
        return

    users = users_collection.find().sort("points", -1)
    message = "📊 **Daily Gym Game Summary** 📊\n\n"
    for i, user_doc in enumerate(users, start=1):
        display = user_doc.get("display_name", "Unknown User")
        points = user_doc.get("points", 0)
        message += f"{i}. {display} - {points} points\n"

    context.bot.send_message(chat_id=GROUP_CHAT_ID, text=message)
    logging.info("Daily summary sent.")


def check_game_end(context: CallbackContext):
    """Scheduled: checks if game duration is exceeded."""
    bot = context.bot

    active_games = games_collection.find({"active": True})
    for game in active_games:
        game_duration = game.get("duration", "1 week")
        game_start_date = game.get("start_date", datetime.datetime.now(pytz.UTC))
        current_date = datetime.datetime.now(pytz.UTC)
        game_days_passed = (current_date - game_start_date).days

        if ((game_duration == "1 week" and game_days_passed >= 7) or
            (game_duration == "2 weeks" and game_days_passed >= 14) or
            (game_duration == "1 month" and game_days_passed >= 30)):

            logging.info(f"Game with ID {game['_id']} has ended due to duration.")
            games_collection.update_one({"_id": game["_id"]}, {"$set": {"active": False}})
            bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text="The game has ended due to duration."
            )
            return

        # Send daily summary
        daily_summary(context)


def scheduled_check_game_end(context: CallbackContext):
    """Wraps check_game_end for APScheduler."""
    check_game_end(context)


def help_command(update: Update, context: CallbackContext):
    """Command: /help — Lists available commands."""
    help_message = (
        "Start with /start -> chose game mode -> chose length of game"
        "Available commands:\n\n"
        "User Commands:\n"
        "/start - Start a new game and choose a game mode (Individual or Team).\n"
        "/claim - Claim points for your workouts. You can only claim once per day.\n"
        "/join - Join a game.\n"
        "/leaderboard - Display the current leaderboard based on the active game mode.\n"
        "For admin commands, use /adminhelp (admin only)."
    )
    update.message.reply_text(help_message)


def admin_help_command(update: Update, context: CallbackContext):
    """Send a help message with all available admin commands."""
    if update.message.from_user.id != ADMIN_ID:
        update.message.reply_text("❌ You are not authorized to view admin commands.")
        return

    admin_help_message = (
        "Admin commands:\n\n"
        "/bot add @username points - Add a specified number of points to the user.\n"
        "/bot remove @username points - Remove a specified number of points from the user.\n"
        "/endgame - End the current game and send a final leaderboard summary.\n"
        "/reset - Reset all user points and streaks to zero.\n"
        "/listusers - List all users in the database along with their points and streaks.\n"
        "/setpoints @username points - Set the exact number of points for a user.\n"
        "/endday - End the current day, increment the day counter, and send a daily leaderboard summary.\n"
        "/adminhelp - Display this admin-only help message."
    )
    update.message.reply_text(admin_help_message)



def join(update: Update, context: CallbackContext):
    """Command: /join — Places a user into Team A or B (balanced or random if even)."""
    user_obj = update.message.from_user
    user_id = user_obj.id
    display_name = get_display_name(user_obj)

    # Check if user is already in the DB
    user = users_collection.find_one({"user_id": user_id})
    if user:
        update.message.reply_text("You're already in the game!")
        return

    # Check for active game
    active_game = games_collection.find_one({"active": True})
    if not active_game:
        update.message.reply_text("No active game found. Use /start to start a new game.")
        return

    # If Team mode, balance or random
    if active_game.get("mode") == "Team":
        team_1 = active_game.get("team_1", [])
        team_2 = active_game.get("team_2", [])

        # Balance if uneven, else random
        if len(team_1) < len(team_2):
            team_1.append(display_name)
            update.message.reply_text(f"Welcome, {display_name}! You've been assigned to Team A.")
        elif len(team_2) < len(team_1):
            team_2.append(display_name)
            update.message.reply_text(f"Welcome, {display_name}! You've been assigned to Team B.")
        else:
            # If teams are equal, pick randomly
            if random.choice([True, False]):
                team_1.append(display_name)
                update.message.reply_text(f"Welcome, {display_name}! You've been assigned to Team A.")
            else:
                team_2.append(display_name)
                update.message.reply_text(f"Welcome, {display_name}! You've been assigned to Team B.")

        # Update the game doc
        games_collection.update_one(
            {"_id": active_game["_id"]},
            {"$set": {"team_1": team_1, "team_2": team_2}}
        )
    else:
        # Individual mode
        update.message.reply_text(f"Welcome, {display_name}! You've been added to the game.")

    # Insert user doc
    users_collection.insert_one({
        "user_id": user_id,
        "display_name": display_name,
        "points": 0,
        "streak": 0,
        "last_claim": None
    })
    logging.info(f"New user added: {display_name} (ID: {user_id})")


def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # ConversationHandler: only 3 states
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_MODE: [MessageHandler(Filters.text, select_mode)],
            SET_DURATION: [MessageHandler(Filters.text, set_duration)],
            CONFIRM_PENALTIES: [MessageHandler(Filters.text, confirm_penalties)],
        },
        fallbacks=[]
    )

    dp.add_handler(conv_handler)

    # Other commands
    dp.add_handler(CommandHandler("claim", claim))
    dp.add_handler(CommandHandler("leaderboard", leaderboard))
    dp.add_handler(CommandHandler("bot", admin_override))
    dp.add_handler(CommandHandler("endgame", end_game))
    dp.add_handler(CommandHandler("reset", reset))
    dp.add_handler(CommandHandler("listusers", list_users))
    dp.add_handler(CommandHandler("setpoints", set_points))
    dp.add_handler(CommandHandler("endday", end_day))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("join", join))

    # APScheduler job for daily checks
    scheduler.add_job(
        lambda: scheduled_check_game_end(CallbackContext.from_bot(updater.bot)),
        'cron',
        hour=23,
        minute=0,
        timezone=pytz.utc
    )
    scheduler.start()

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
