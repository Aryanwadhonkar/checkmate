import os
import json
import logging
import random
import uuid
import sys
import asyncio
import time
import re
import backup_patch
from datetime import datetime, timedelta
from dotenv import load_dotenv, set_key
from sync_command import register_sync_command
from custom_media_delete_integration_main import integrate_custom_media_delete
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackContext,
    CallbackQueryHandler,
    ConversationHandler
)


# Import search functions for links channel
from search_links_channel import search_links_channel_for_file, search_links_channel_for_batch

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment file path
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

# Load environment variables
load_dotenv(ENV_FILE)

# Configuration
TOKEN = os.getenv("BOT_TOKEN")
ADMINS = [int(id) for id in os.getenv("ADMINS", "").split(",") if id]
OWNER_ID = int(os.getenv("ADMINS", "0").split(",")[0]) if os.getenv("ADMINS") else 0
DATABASE_CHANNEL = int(os.getenv("DATABASE_CHANNEL", 0))
LINKS_CHANNEL = int(os.getenv("LINKS_CHANNEL", 0))  # New channel for storing links
FORCE_SUB = int(os.getenv("FORCE_SUB", 0))
AUTO_DELETE = int(os.getenv("AUTO_DELETE", 0))
TOKEN_DURATION = int(os.getenv("TOKEN_DURATION", 24))  # Token duration in hours
RENAME_TEMPLATE = os.getenv("RENAME_TEMPLATE", "")
GET_TOKEN = os.getenv("GET_TOKEN", "")  # URL for the Get Token button
TOKEN_VERIFICATION_ENABLED = os.getenv("TOKEN_VERIFICATION_ENABLED", "1") == "1"  # Token verification toggle

# File paths
BANNED_USERS_FILE = "banned_users.json"
FILE_DATABASE = "files.json"
BATCHES_FILE = "batches.json"
TOKENS_FILE = "tokens.json"
PENDING_DELETES_FILE = "pending_deletes.json"
GROUP_STATS_FILE = "group_stats.json"
GROUP_SETTINGS_FILE = "group_settings.json"  # New file for group-specific settings

# Initialize data files
for file in [BANNED_USERS_FILE, FILE_DATABASE, BATCHES_FILE, TOKENS_FILE, PENDING_DELETES_FILE, GROUP_STATS_FILE, GROUP_SETTINGS_FILE]:
    if not os.path.exists(file):
        with open(file, 'w') as f:
            json.dump([] if file == BANNED_USERS_FILE else {}, f)

# Mikasa's Personality Database
MIKASA_QUOTES = {
    'ban': ["Threat neutralized. Eren is safe.", "A Lot Of People I Used To Care About Aren't Here Either"],
    'unban': ["🎌 Access restored!", "⚔️ Second chance granted!"],
    'error': ["💔 Failed... Eren would be disappointed!", "I Don't Want To Lose What Little Family I Have Left."],
    'success': [" I Am Strong. Stronger Than All Of You. Extremely Strong. I Can Kill All The Titans Out There. Even If I Am Alone", "Eat It, Eat And Stay Alive, I Won't Let You Starve To Death"],
    'warning': [" Not so fast!", " I'm watching you..."],
    'info': ["📜 Report:", "📜Status:"],
    'greeting': ["⚔️ I'll protect you.", "I'll always be by your side","My Scarf, Thank You For Always Wrapping It Around Me","✊Shinzo wo sasageyo"],
    'default': ["If I can't, then I'll just die. But if I win I live. Unless I fight, I cannot win","Once I'm dead, I won't even be able to remember you. So I'll win, no matter what. I'll live, no matter what"],
    'welcome': ["Welcome to our group! I'll protect everyone here.", "A new comrade has joined our ranks. Together we'll fight!"]
}

def mikasa_reply(category='default'):
    return random.choice(MIKASA_QUOTES.get(category, MIKASA_QUOTES['default'])) + "\n"

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMINS:
            await update.message.reply_text(mikasa_reply('warning') + "Unauthorized!")
            return
        return await func(update, context)
    return wrapper

def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text(mikasa_reply('warning') + "This command is only available to the owner!")
            return
        return await func(update, context)
    return wrapper

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Error: {context.error}")
    try:
        # Safely get chat_id
        chat_id = None
        if update and hasattr(update, 'effective_chat') and update.effective_chat:
            chat_id = update.effective_chat.id
        else:
            # If we can't get chat_id from update, use the first admin
            chat_id = ADMINS[0] if ADMINS else None
        
        # Only send message if we have a valid chat_id
        if chat_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text=mikasa_reply('error') + f"Error: {context.error}"
            )
        else:
            logging.warning("Could not determine chat_id for error notification")
    except Exception as e:
        logging.error(f"Failed to send error message: {e}")

# ========== AUTO DELETE MECHANISM ========== #
async def delete_message_after_delay(context: CallbackContext):
    job = context.job
    try:
        # Extract chat_id and message_id from job data
        chat_id, message_id = job.data
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logging.info(f"Auto-deleted message {message_id} in chat {chat_id}")
        
        # Remove from pending deletes
        remove_pending_delete(chat_id, message_id)
    except Exception as e:
        logging.error(f"Failed to auto-delete message: {e}")

def save_pending_delete(chat_id, message_id, delete_time):
    """Save a pending delete to file for persistence across bot restarts"""
    try:
        with open(PENDING_DELETES_FILE, 'r+') as f:
            try:
                pending = json.load(f)
            except json.JSONDecodeError:
                pending = {}
            
            # Ensure pending is a dictionary
            if not isinstance(pending, dict):
                pending = {}
            
            # Convert chat_id to string for JSON
            str_chat_id = str(chat_id)
            if str_chat_id not in pending:
                pending[str_chat_id] = {}
            
            # Store message_id with deletion timestamp
            pending[str_chat_id][str(message_id)] = delete_time
            
            # Write back to file
            f.seek(0)
            f.truncate()
            json.dump(pending, f)
    except Exception as e:
        logging.error(f"Error saving pending delete: {e}")

def remove_pending_delete(chat_id, message_id):
    """Remove a pending delete from file after it's been processed"""
    try:
        with open(PENDING_DELETES_FILE, 'r+') as f:
            try:
                pending = json.load(f)
            except json.JSONDecodeError:
                return
            
            # Ensure pending is a dictionary
            if not isinstance(pending, dict):
                return
            
            # Convert chat_id to string for JSON
            str_chat_id = str(chat_id)
            if str_chat_id in pending and str(message_id) in pending[str_chat_id]:
                del pending[str_chat_id][str(message_id)]
                
                # Remove empty chat entries
                if not pending[str_chat_id]:
                    del pending[str_chat_id]
                
                # Write back to file
                f.seek(0)
                f.truncate()
                json.dump(pending, f)
    except Exception as e:
        logging.error(f"Error removing pending delete: {e}")

async def get_group_auto_delete_time(chat_id):
    """Get the auto-delete time for a specific group"""
    try:
        with open(GROUP_SETTINGS_FILE, 'r') as f:
            try:
                settings = json.load(f)
                if not isinstance(settings, dict):
                    settings = {}
            except json.JSONDecodeError:
                settings = {}
            
            # Convert chat_id to string for JSON
            str_chat_id = str(chat_id)
            if str_chat_id in settings and "auto_delete" in settings[str_chat_id]:
                return settings[str_chat_id]["auto_delete"]
    except Exception as e:
        logging.error(f"Error getting group auto-delete time: {e}")
    
    # Return global setting if no group-specific setting exists
    return AUTO_DELETE

async def set_group_auto_delete_time(chat_id, minutes):
    """Set the auto-delete time for a specific group"""
    try:
        with open(GROUP_SETTINGS_FILE, 'r+') as f:
            try:
                settings = json.load(f)
                if not isinstance(settings, dict):
                    settings = {}
            except json.JSONDecodeError:
                settings = {}
            
            # Convert chat_id to string for JSON
            str_chat_id = str(chat_id)
            if str_chat_id not in settings:
                settings[str_chat_id] = {}
            
            # Store auto-delete time
            settings[str_chat_id]["auto_delete"] = minutes
            
            # Write back to file
            f.seek(0)
            f.truncate()
            json.dump(settings, f)
            logging.info(f"Set auto-delete time for group {chat_id} to {minutes} minutes")
            return True
    except Exception as e:
        logging.error(f"Error setting group auto-delete time: {e}")
        return False

async def schedule_message_deletion(context, chat_id, message_id, minutes=None):
    """Schedule a message for deletion and save it for persistence"""
    # If minutes is not provided, get from group settings or global setting
    if minutes is None:
        minutes = await get_group_auto_delete_time(chat_id)
    
    if minutes <= 0:
        return
    
    # Calculate deletion time
    delete_time = int(time.time() + (minutes * 60))
    
    # Save to pending deletes file
    save_pending_delete(chat_id, message_id, delete_time)
    
    # Schedule the job
    context.job_queue.run_once(
        delete_message_after_delay,
        minutes * 60,  # Convert minutes to seconds
        data=(chat_id, message_id),
        name=f"delete_{chat_id}_{message_id}"
    )
    logging.info(f"Scheduled deletion for message {message_id} in chat {chat_id} in {minutes} minutes")

async def restore_pending_deletes(context):
    """Restore pending deletes from file when bot starts"""
    try:
        with open(PENDING_DELETES_FILE, 'r') as f:
            try:
                pending = json.load(f)
            except json.JSONDecodeError:
                return
            
            # Ensure pending is a dictionary
            if not isinstance(pending, dict):
                return
            
            current_time = int(time.time())
            for str_chat_id, messages in pending.items():
                if not isinstance(messages, dict):
                    continue
                
                try:
                    chat_id = int(str_chat_id)
                    for str_message_id, delete_time in messages.items():
                        try:
                            message_id = int(str_message_id)
                            
                            # Calculate remaining time
                            remaining_seconds = delete_time - current_time
                            
                            if remaining_seconds <= 0:
                                # Delete immediately if time has passed
                                try:
                                    await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                                    logging.info(f"Immediately deleted expired message {message_id} in chat {chat_id}")
                                except Exception as e:
                                    logging.error(f"Failed to delete expired message: {e}")
                                remove_pending_delete(chat_id, message_id)
                            else:
                                # Schedule deletion for remaining time
                                context.job_queue.run_once(
                                    delete_message_after_delay,
                                    remaining_seconds,
                                    data=(chat_id, message_id),
                                    name=f"delete_{chat_id}_{message_id}"
                                )
                                logging.info(f"Restored scheduled deletion for message {message_id} in chat {chat_id} in {remaining_seconds/60:.1f} minutes")
                        except (ValueError, TypeError) as e:
                            logging.error(f"Error processing message ID {str_message_id}: {e}")
                except ValueError as e:
                    logging.error(f"Error processing chat ID {str_chat_id}: {e}")
    except Exception as e:
        logging.error(f"Error restoring pending deletes: {e}")

# ========== TOKEN VERIFICATION SYSTEM ========== #
# New function to check for existing valid tokens
def get_valid_token():
    """Check if there's a valid token already in the tokens file"""
    try:
        # Make sure the tokens file exists
        if not os.path.exists(TOKENS_FILE):
            logging.warning("Tokens file does not exist")
            return None
        
        # Read tokens from file
        with open(TOKENS_FILE, 'r') as f:
            try:
                tokens = json.load(f)
                if not isinstance(tokens, dict):
                    logging.warning("Tokens file does not contain a dictionary")
                    return None
            except json.JSONDecodeError:
                logging.warning("Tokens file contains invalid JSON")
                return None
        
        current_time = int(time.time())
        
        # Find the token with the latest expiry time that's still valid
        latest_token = None
        latest_expiry = 0
        
        for token, data in tokens.items():
            if not isinstance(data, dict):
                continue
            
            if "user_id" not in data or "expiry" not in data:
                continue
            
            # Check if token is for placeholder user (0) and not expired
            if data["user_id"] == 0 and data["expiry"] > current_time:
                # If this token expires later than our current latest, update it
                if data["expiry"] > latest_expiry:
                    latest_token = token
                    latest_expiry = data["expiry"]
        
        if latest_token:
            logging.info(f"Found existing valid token that expires at {datetime.fromtimestamp(latest_expiry).strftime('%Y-%m-%d %H:%M:%S')}")
            return latest_token, latest_expiry
        
        logging.info("No valid token found")
        return None
    except Exception as e:
        logging.error(f"Error checking for valid tokens: {e}")
        return None

async def generate_token(user_id, context):
    """Generate a unique token for a user and distribute to admins"""
    token = str(uuid.uuid4())
    expiry = int(time.time() + TOKEN_DURATION * 3600)  # Current time + duration in hours
    
    # Create verification URL
    bot_username = context.bot.username
    verification_url = f"https://t.me/{bot_username}?start=verify_{token}"
    
    # Store token in file
    try:
        # Make sure the tokens file exists
        if not os.path.exists(TOKENS_FILE):
            with open(TOKENS_FILE, 'w') as f:
                json.dump({}, f)
        
        # Read existing tokens
        with open(TOKENS_FILE, 'r') as f:
            try:
                tokens = json.load(f)
                if not isinstance(tokens, dict):
                    tokens = {}
            except json.JSONDecodeError:
                tokens = {}
        
        # Add new token
        tokens[token] = {
            "user_id": user_id,
            "expiry": expiry
        }
        
        # Write updated tokens back to file
        with open(TOKENS_FILE, 'w') as f:
            json.dump(tokens, f)
            
        logging.info(f"Stored token {token} in tokens file")
    except Exception as e:
        logging.error(f"Error storing token in file: {e}")
    
    # Send direct token URL to all admins
    expiry_time = datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M:%S')
    token_message = f"🔑 New Token Generated\n\nVerification URL: {verification_url}\nExpires: {expiry_time}"
    
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=token_message
            )
            logging.info(f"Sent token URL to admin {admin_id}")
        except Exception as e:
            logging.error(f"Failed to send token URL to admin {admin_id}: {e}")
    
    # Send and pin token URL in database channel
    try:
        # Send message to database channel
        db_msg = await context.bot.send_message(
            chat_id=DATABASE_CHANNEL,
            text=f"🔑 Current Access Token (valid for {TOKEN_DURATION} hours)\n\nVerification URL: {verification_url}"
        )
        
        # Pin the message
        await context.bot.pin_chat_message(
            chat_id=DATABASE_CHANNEL,
            message_id=db_msg.message_id,
            disable_notification=True
        )
        logging.info(f"Pinned token URL message {db_msg.message_id} in database channel")
        
        # Unpin previous pinned messages
        try:
            chat = await context.bot.get_chat(DATABASE_CHANNEL)
            if chat.pinned_message and chat.pinned_message.message_id != db_msg.message_id:
                await context.bot.unpin_chat_message(
                    chat_id=DATABASE_CHANNEL,
                    message_id=chat.pinned_message.message_id
                )
        except Exception as e:
            logging.error(f"Error unpinning previous message: {e}")
    except Exception as e:
        logging.error(f"Failed to send/pin token URL in database channel: {e}")
    
    return token, verification_url

def verify_token(token):
    """Verify if a token is valid and not expired"""
    try:
        # Make sure the tokens file exists
        if not os.path.exists(TOKENS_FILE):
            logging.warning("Tokens file does not exist")
            return None
        
        # Read tokens from file
        with open(TOKENS_FILE, 'r') as f:
            try:
                tokens = json.load(f)
                if not isinstance(tokens, dict):
                    logging.warning("Tokens file does not contain a dictionary")
                    return None
            except json.JSONDecodeError:
                logging.warning("Tokens file contains invalid JSON")
                return None
        
        # Check if token exists
        if token not in tokens:
            logging.warning(f"Token {token} not found in tokens file")
            return None
        
        token_data = tokens[token]
        
        # Check if token_data has the expected structure
        if not isinstance(token_data, dict) or "user_id" not in token_data or "expiry" not in token_data:
            logging.warning(f"Token {token} has invalid data structure")
            return None
        
        current_time = int(time.time())
        
        # Check if token is expired
        if token_data["expiry"] <= current_time:
            logging.info(f"Token {token} has expired")
            
            # Remove expired token
            tokens.pop(token)
            with open(TOKENS_FILE, 'w') as f:
                json.dump(tokens, f)
            
            return None
        
        # Token is valid
        logging.info(f"Token {token} is valid for user {token_data['user_id']}")
        return token_data["user_id"]
    except Exception as e:
        logging.error(f"Error verifying token: {e}")
        return None

def check_user_token(user_id):
    """Check if a user has a valid token"""
    try:
        # Make sure the tokens file exists
        if not os.path.exists(TOKENS_FILE):
            logging.warning("Tokens file does not exist")
            return False
        
        # Read tokens from file
        with open(TOKENS_FILE, 'r') as f:
            try:
                tokens = json.load(f)
                if not isinstance(tokens, dict):
                    logging.warning("Tokens file does not contain a dictionary")
                    return False
            except json.JSONDecodeError:
                logging.warning("Tokens file contains invalid JSON")
                return False
        
        current_time = int(time.time())
        
        # Check each token
        for token, data in tokens.items():
            if not isinstance(data, dict):
                continue
            
            if "user_id" not in data or "expiry" not in data:
                continue
            
            # Check if token belongs to user and is not expired
            # Also check if token is for placeholder user (0) which means it's valid for all users
            if (data["user_id"] == user_id or data["user_id"] == 0) and data["expiry"] > current_time:
                logging.info(f"Found valid token for user {user_id}")
                return True
        
        logging.info(f"No valid token found for user {user_id}")
        return False
    except Exception as e:
        logging.error(f"Error checking user token: {e}")
        return False

# Modified to check for existing valid tokens before generating a new one
async def refresh_token(context: CallbackContext):
    """Generate a new token only if no valid token exists or current token is about to expire"""
    logging.info("Scheduled token refresh triggered")
    
    # Check if there's already a valid token
    token_info = get_valid_token()
    
    if token_info:
        token, expiry = token_info
        current_time = int(time.time())
        
        # If token still has more than 1 hour of validity, don't generate a new one
        if expiry - current_time > 3600:  # More than 1 hour remaining
            logging.info(f"Using existing valid token that expires at {datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M:%S')}")
            return
    
    # No valid token found or token is about to expire, generate a new one
    logging.info("No valid token found or token is about to expire, generating a new one")
    await generate_token(0, context)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command with or without arguments"""
    user_id = update.effective_user.id
    args = context.args
    
    # Check ban status
    try:
        with open(BANNED_USERS_FILE, 'r') as f:
            banned_users = json.load(f)
            if not isinstance(banned_users, list):
                banned_users = []
            
            if str(user_id) in banned_users or user_id in banned_users:
                await update.message.reply_text(mikasa_reply('ban') + "You are banned from using this bot!")
                return
    except Exception as e:
        logging.error(f"Error checking ban status: {e}")
    
    # If there are arguments, it might be a file ID, batch ID, or token
    if args:
        arg = args[0]
        
        # Check if it's a token verification
        if arg.startswith("verify_"):
            token = arg[7:]  # Remove "verify_" prefix
            logging.info(f"Verifying token: {token} for user {user_id}")
            
            verified_user_id = verify_token(token)
            logging.info(f"Verification result: {verified_user_id}")
            
            if verified_user_id is not None and (verified_user_id == user_id or verified_user_id == 0):
                # Token is valid for this user
                await update.message.reply_text(
                    mikasa_reply('success') + "Token verified successfully! You now have access for 24 hours."
                )
                return
            else:
                # Invalid or expired token
                await update.message.reply_text(
                    mikasa_reply('warning') + "Invalid or expired token. Please get a new token."
                )
                return
        
        # Check if user has a valid token (only if token verification is enabled)
        if TOKEN_VERIFICATION_ENABLED:
            has_valid_token = check_user_token(user_id)
            
            if not has_valid_token:
                # User doesn't have a valid token
                # Create inline keyboard with Get Token button
                keyboard = []
                if GET_TOKEN:
                    # Make sure GET_TOKEN is a valid URL
                    if not GET_TOKEN.startswith(('http://', 'https://')):
                        logging.warning(f"Invalid GET_TOKEN URL: {GET_TOKEN}")
                        # Generate a new token if GET_TOKEN is invalid
                        _, verification_url = await generate_token(user_id, context)
                        keyboard.append([InlineKeyboardButton("Get Token", url=verification_url)])
                    else:
                        keyboard.append([InlineKeyboardButton("Get Token", url=GET_TOKEN)])
                else:
                    # Generate a new token if GET_TOKEN is not set
                    _, verification_url = await generate_token(user_id, context)
                    keyboard.append([InlineKeyboardButton("Get Token", url=verification_url)])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    mikasa_reply('warning') + "You need to verify access to use this bot.\n\n"
                    "Click the button below to get a 24-hour access token:",
                    reply_markup=reply_markup
                )
                return
        
        # If we get here, user has a valid token, proceed with file/batch handling
        await send_file(update, context)
        return
    
    # No arguments, show welcome message with menu
    welcome_message = (
        f"{mikasa_reply('greeting')}Welcome to the File Sharing Bot!\n\n"
        "Use this bot to access shared files and batches.\n\n"
        "Available commands:\n"
        "/menu - Show main menu\n"
        "/help - Show help information"
    )
    
    # Check if user has a valid token (only if token verification is enabled)
    if TOKEN_VERIFICATION_ENABLED:
        has_valid_token = check_user_token(user_id)
        
        if not has_valid_token:
            # User doesn't have a valid token
            # Create inline keyboard with Get Token button
            keyboard = []
            if GET_TOKEN:
                # Make sure GET_TOKEN is a valid URL
                if not GET_TOKEN.startswith(('http://', 'https://')):
                    logging.warning(f"Invalid GET_TOKEN URL: {GET_TOKEN}")
                    # Generate a new token if GET_TOKEN is invalid
                    _, verification_url = await generate_token(user_id, context)
                    keyboard.append([InlineKeyboardButton("Get Token", url=verification_url)])
                else:
                    keyboard.append([InlineKeyboardButton("Get Token", url=GET_TOKEN)])
            else:
                # Generate a new token if GET_TOKEN is not set
                _, verification_url = await generate_token(user_id, context)
                keyboard.append([InlineKeyboardButton("Get Token", url=verification_url)])
            
            keyboard.append([InlineKeyboardButton("📋 Main Menu", callback_data="menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                welcome_message + "\n\nYou need to verify access to use this bot.\n"
                "Click the button below to get a 24-hour access token:",
                reply_markup=reply_markup
            )
            return
    
    # User has a valid token or token verification is disabled, show regular welcome message
    keyboard = [
        [InlineKeyboardButton("📋 Main Menu", callback_data="menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_message, reply_markup=reply_markup)

# Add token toggle command for owner
@owner_only
async def token_toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle token verification on/off"""
    global TOKEN_VERIFICATION_ENABLED
    
    # Toggle the setting
    TOKEN_VERIFICATION_ENABLED = not TOKEN_VERIFICATION_ENABLED
    
    # Update the environment variable
    try:
        # Update in memory
        os.environ["TOKEN_VERIFICATION_ENABLED"] = "1" if TOKEN_VERIFICATION_ENABLED else "0"
        
        # Update in .env file
        set_key(ENV_FILE, "TOKEN_VERIFICATION_ENABLED", "1" if TOKEN_VERIFICATION_ENABLED else "0")
        
        status = "enabled" if TOKEN_VERIFICATION_ENABLED else "disabled"
        await update.message.reply_text(
            mikasa_reply('success') + f"Token verification has been {status}.\n\n"
            f"Users {'will now' if TOKEN_VERIFICATION_ENABLED else 'will no longer'} need to verify with a token to access files."
        )
    except Exception as e:
        logging.error(f"Error updating token verification setting: {e}")
        await update.message.reply_text(
            mikasa_reply('error') + f"Failed to update token verification setting. Error: {str(e)}"
        )

# ========== CUSTOMIZE COMMAND ========== #
# States for the customize conversation
SELECTING_VARIABLE, ENTERING_VALUE = range(2)

@owner_only
async def customize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to customize environment variables"""
    # List of customizable variables
    customizable_vars = [
        "AUTO_DELETE",
        "TOKEN_DURATION",
        "RENAME_TEMPLATE",
        "GET_TOKEN",
        "FORCE_SUB"
    ]
    
    # Create keyboard with variable options
    keyboard = []
    for var in customizable_vars:
        keyboard.append([InlineKeyboardButton(f"{var} = {os.getenv(var, 'Not set')}", callback_data=f"customize_{var}")])
    
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="customize_cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        mikasa_reply('info') + "Select a variable to customize:",
        reply_markup=reply_markup
    )
    
    # Set the state
    context.user_data['customize_state'] = SELECTING_VARIABLE
    return SELECTING_VARIABLE

async def customize_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle customize command button callbacks"""
    query = update.callback_query
    await query.answer()
    
    # Get the current state
    state = context.user_data.get('customize_state', SELECTING_VARIABLE)
    
    if query.data == "customize_cancel":
        await query.edit_message_text(mikasa_reply('info') + "Customization canceled.")
        return ConversationHandler.END
    
    if state == SELECTING_VARIABLE:
        # Extract the variable name from the callback data
        var_name = query.data.split('_', 1)[1]
        context.user_data['customize_var'] = var_name
        
        # Ask for the new value
        await query.edit_message_text(
            mikasa_reply('info') + f"Enter a new value for {var_name}:\n\nCurrent value: {os.getenv(var_name, 'Not set')}"
        )
        
        # Update state
        context.user_data['customize_state'] = ENTERING_VALUE
        return ENTERING_VALUE
    
    return SELECTING_VARIABLE

async def customize_value_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the value input for customize command"""
    # Check if update.message exists
    if not update.message:
        logging.warning("Received update with no message attribute in customize_value_handler")
        return ConversationHandler.END
        
    var_name = context.user_data.get('customize_var')
    
    # Safely check if text attribute exists
    if not hasattr(update.message, 'text') or not update.message.text:
        await update.message.reply_text(mikasa_reply('error') + "Invalid input. Please try again with a text message.")
        return ConversationHandler.END
        
    new_value = update.message.text.strip()
    
    if not var_name:
        await update.message.reply_text(mikasa_reply('error') + "Something went wrong. Please try again.")
        return ConversationHandler.END
    
    # Update the environment variable
    try:
        # Update in memory
        os.environ[var_name] = new_value
        
        # Update in .env file
        set_key(ENV_FILE, var_name, new_value)
        
        # Update global variables based on the changed env var
        if var_name == "AUTO_DELETE":
            global AUTO_DELETE
            AUTO_DELETE = int(new_value) if new_value.isdigit() else 0
        elif var_name == "TOKEN_DURATION":
            global TOKEN_DURATION
            TOKEN_DURATION = int(new_value) if new_value.isdigit() else 24
        elif var_name == "RENAME_TEMPLATE":
            global RENAME_TEMPLATE
            RENAME_TEMPLATE = new_value
        elif var_name == "GET_TOKEN":
            global GET_TOKEN
            GET_TOKEN = new_value
        elif var_name == "FORCE_SUB":
            global FORCE_SUB
            FORCE_SUB = int(new_value) if new_value.isdigit() else 0
        
        await update.message.reply_text(
            mikasa_reply('success') + f"Successfully updated {var_name} to: {new_value}"
        )
    except Exception as e:
        logging.error(f"Error updating environment variable: {e}")
        await update.message.reply_text(
            mikasa_reply('error') + f"Failed to update {var_name}. Error: {str(e)}"
        )
    
    return ConversationHandler.END

# ========== BATCH HANDLING ========== #
@admin_only
async def start_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['batch'] = []
    logging.info(f"Started new batch for user {update.effective_user.id}")
    await update.message.reply_text(mikasa_reply('success') + "Batch collection started!")

@admin_only
async def end_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'batch' not in context.user_data or not context.user_data['batch']:
        await update.message.reply_text(mikasa_reply('warning') + "No active batch!")
        return
    
    batch_files = context.user_data['batch']
    logging.info(f"Ending batch with {len(batch_files)} files: {batch_files}")
    
    batch_id = str(uuid.uuid4())
    try:
        with open(BATCHES_FILE, 'r+') as f:
            try:
                batches = json.load(f)
                if not isinstance(batches, dict):
                    batches = {}
            except json.JSONDecodeError:
                batches = {}
            
            batches[batch_id] = batch_files
            f.seek(0)
            f.truncate()
            json.dump(batches, f)
            logging.info(f"Saved batch {batch_id} with files: {batch_files}")
    except Exception as e:
        logging.error(f"Error saving batch: {e}")
        await update.message.reply_text(mikasa_reply('error') + "Failed to save batch!")
        return
    
    batch_link = f"t.me/{context.bot.username}?start={batch_id}"
    
    # Store batch link in links channel if configured
    link_msg_id = None
    if LINKS_CHANNEL:
        try:
            # Get file names for the batch
            file_names = []
            with open(FILE_DATABASE, 'r') as f:
                try:
                    files = json.load(f)
                    for file_id in batch_files:
                        if file_id in files and isinstance(files[file_id], dict):
                            name = files[file_id].get("custom_name", "Unnamed file")
                            file_names.append(name)
                except json.JSONDecodeError:
                    pass
            
            file_list = "\n".join([f"• {name}" for name in file_names]) if file_names else "• Files in batch"
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            link_msg = await context.bot.send_message(
                chat_id=LINKS_CHANNEL,
                text=f"🔗 Batch Link (contains {len(batch_files)} files)\n\n"
                     f"Date: {date_str}\n"
                     f"Files:\n{file_list}\n\n"
                     f"Link: {batch_link}\n\n"
                     f"#batch_{batch_id}"  # Add hashtag for easier searching
            )
            link_msg_id = link_msg.message_id
            logging.info(f"Stored batch link in links channel, message ID: {link_msg_id}")
            
            # Update batch info with link to links channel message
            with open(BATCHES_FILE, 'r+') as f:
                try:
                    batches = json.load(f)
                    if batch_id in batches:
                        # Convert to dict if it's a list
                        if isinstance(batches[batch_id], list):
                            file_list = batches[batch_id]
                            batches[batch_id] = {
                                "files": file_list,
                                "links_channel_msg_id": link_msg_id
                            }
                        # Or update if it's already a dict
                        elif isinstance(batches[batch_id], dict):
                            batches[batch_id]["links_channel_msg_id"] = link_msg_id
                        
                        f.seek(0)
                        f.truncate()
                        json.dump(batches, f)
                except Exception as e:
                    logging.error(f"Error updating batch with links channel message ID: {e}")
        except Exception as e:
            logging.error(f"Failed to store batch link in links channel: {e}")
    
    await update.message.reply_text(
        mikasa_reply('success') + f"Batch stored!\nShare link:\n{batch_link}"
    )
    context.user_data.pop('batch')

# ========== FILE HANDLING ========== #
@admin_only
async def rename_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to rename the next file to be stored"""
    context.user_data['awaiting_rename'] = True
    await update.message.reply_text(
        mikasa_reply('info') + "Please enter the new name for the next file:"
    )
    return ConversationHandler.END

@admin_only
async def store_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if update.message exists
    if not update.message:
        logging.warning("Received update with no message attribute in store_file")
        return
        
    # Check if this is a text message for rename
    if 'awaiting_rename' in context.user_data and context.user_data['awaiting_rename']:
        if hasattr(update.message, 'text') and update.message.text and not update.message.text.startswith('/'):
            new_name = update.message.text
            context.user_data['custom_filename'] = new_name
            context.user_data['awaiting_rename'] = False
            logging.info(f"Set custom filename '{new_name}' in user data")
            
            await update.message.reply_text(
                mikasa_reply('success') + f"Next file will be renamed to: {new_name}\n\nNow send the file."
            )
            return
    
    # Regular file storage
    file_id = str(uuid.uuid4())
    
    # Check if we have a custom filename
    custom_filename = None
    if 'custom_filename' in context.user_data:
        custom_filename = context.user_data.pop('custom_filename')
        logging.info(f"Using custom filename '{custom_filename}' for file {file_id}")
    
    try:
        # Forward the message to the database channel
        msg = await context.bot.forward_message(
            chat_id=DATABASE_CHANNEL,
            from_chat_id=update.message.chat_id,
            message_id=update.message.message_id
        )
        
        file_link = f"t.me/{context.bot.username}?start={file_id}"
        
        # Store complete file metadata in links channel if configured
        link_msg_id = None
        if LINKS_CHANNEL:
            try:
                # Include ALL metadata in the links channel message
                media_type = get_media_type(update.message)
                caption = update.message.caption or ""
                date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # Enhanced metadata format with ALL necessary information
                link_msg = await context.bot.send_message(
                    chat_id=LINKS_CHANNEL,
                    text=f"🔗 File Link\n\n"
                         f"ID: {file_id}\n"
                         f"Name: {custom_filename if custom_filename else 'Unnamed file'}\n"
                         f"Type: {media_type}\n"
                         f"Date: {date_str}\n"
                         f"Caption: {caption}\n"
                         f"Message ID: {msg.message_id}\n\n"
                         f"Link: {file_link}\n\n"
                         f"#file_{file_id}"  # Add hashtag for easier searching
                )
                link_msg_id = link_msg.message_id
                logging.info(f"Stored complete file metadata in links channel, message ID: {link_msg_id}")
            except Exception as e:
                logging.error(f"Failed to store file metadata in links channel: {e}")
        
        # For backward compatibility, still store minimal metadata locally
        # This will be phased out in future versions
        try:
            with open(FILE_DATABASE, 'r+') as f:
                try:
                    files = json.load(f)
                    if not isinstance(files, dict):
                        files = {}
                except json.JSONDecodeError:
                    files = {}
                
                # Store only essential metadata for link recognition
                files[file_id] = {
                    "message_id": msg.message_id,
                    "custom_name": custom_filename,
                    "media_type": get_media_type(update.message),
                    "file_link": file_link,
                    "links_channel_msg_id": link_msg_id  # Store reference to links channel message
                }
                f.seek(0)
                f.truncate()
                json.dump(files, f)
                logging.info(f"Stored minimal file metadata locally for backward compatibility")
        except Exception as e:
            # If local storage fails, it's not critical anymore since we have complete metadata in links channel
            logging.warning(f"Failed to store local metadata, but links channel storage succeeded: {e}")
        
        # Handle batch
        if 'batch' in context.user_data:
            context.user_data['batch'].append(file_id)
            batch_files = context.user_data['batch']
            logging.info(f"Added file {file_id} to batch, now contains {len(batch_files)} files")
            reply_text = "File added to batch! Send more or /lastbatch"
        else:
            reply_text = f"File stored!\nLink: {file_link}"
        
        await update.message.reply_text(mikasa_reply('success') + reply_text)
    except Exception as e:
        logging.error(f"Error storing file: {e}")
        await update.message.reply_text(mikasa_reply('error') + "Failed to store file!")

def get_media_type(message):
    """Determine the type of media in a message"""
    if message.photo:
        return "photo"
    elif message.video:
        return "video"
    elif message.audio:
        return "audio"
    elif message.document:
        return "document"
    elif message.animation:
        return "animation"
    elif message.voice:
        return "voice"
    elif message.video_note:
        return "video_note"
    elif message.sticker:
        return "sticker"
    else:
        return "unknown"

async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    file_id = context.args[0] if context.args else None
    
    # Check ban status
    try:
        with open(BANNED_USERS_FILE, 'r') as f:
            banned_users = json.load(f)
            if not isinstance(banned_users, list):
                banned_users = []
            
            if str(user_id) in banned_users or user_id in banned_users:
                await update.message.reply_text(mikasa_reply('ban') + "Banned!")
                return
    except Exception as e:
        logging.error(f"Error checking ban status: {e}")
    
    # Check token verification only if enabled
    if TOKEN_VERIFICATION_ENABLED:
        has_valid_token = check_user_token(user_id)
        
        if not has_valid_token:
            # User doesn't have a valid token
            # Create inline keyboard with Get Token button
            keyboard = []
            if GET_TOKEN:
                # Make sure GET_TOKEN is a valid URL
                if not GET_TOKEN.startswith(('http://', 'https://')):
                    logging.warning(f"Invalid GET_TOKEN URL: {GET_TOKEN}")
                    # Generate a new token if GET_TOKEN is invalid
                    _, verification_url = await generate_token(user_id, context)
                    keyboard.append([InlineKeyboardButton("Get Token", url=verification_url)])
                else:
                    keyboard.append([InlineKeyboardButton("Get Token", url=GET_TOKEN)])
            else:
                # Generate a new token if GET_TOKEN is not set
                _, verification_url = await generate_token(user_id, context)
                keyboard.append([InlineKeyboardButton("Get Token", url=verification_url)])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                mikasa_reply('warning') + "You need to verify access to use this bot.\n\n"
                "Click the button below to get a 24-hour access token:",
                reply_markup=reply_markup
            )
            return
    
    # Force subscription check
    if FORCE_SUB != 0:
        try:
            member = await context.bot.get_chat_member(FORCE_SUB, user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                await update.message.reply_text(
                    mikasa_reply('warning') + "Join channel first!",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "Join Channel", url=f"t.me/{FORCE_SUB}"
                    )]])
                )
                return
        except Exception as e:
            logging.error(f"Force sub error: {e}")
    
    if not file_id:
        await update.message.reply_text(mikasa_reply('warning') + "No file ID provided!")
        return
    
    # Handle file/batch sending
    try:
        # First try to get file data from local storage (for backward compatibility)
        file_data = None
        try:
            with open(FILE_DATABASE, 'r') as f:
                try:
                    files = json.load(f)
                    if isinstance(files, dict) and file_id in files:
                        file_data = files[file_id]
                        logging.info(f"Found file {file_id} in local storage")
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            logging.warning(f"Error reading local file database: {e}")
        
        # If file not found in local storage, search in links channel
        if not file_data or not isinstance(file_data, dict):
            logging.info(f"File {file_id} not found in local storage, searching links channel")
            file_data = await search_links_channel_for_file(context, file_id)
            
            if file_data:
                logging.info(f"Found file {file_id} in links channel")
            else:
                logging.warning(f"File {file_id} not found in links channel")
        
        if file_data and isinstance(file_data, dict):
            # Single file found either in local storage or links channel
            message_id = file_data.get("message_id")
            if not message_id:
                await update.message.reply_text(mikasa_reply('warning') + "Invalid file data - missing message ID!")
                return
            
            custom_name = file_data.get("custom_name")
            caption = f"{custom_name}" if custom_name else None
            
            try:
                # Copy the message
                sent_msg = await context.bot.copy_message(
                    chat_id=update.effective_chat.id,
                    from_chat_id=DATABASE_CHANNEL,
                    message_id=message_id,
                    caption=caption,
                    protect_content=True
                )
                logging.info(f"Sent file {file_id} (message ID {message_id}) to user {user_id}")
                
                # Schedule auto-delete if enabled
                if AUTO_DELETE > 0:
                    # Add notification about auto-delete
                    info_msg = await update.message.reply_text(
                        mikasa_reply('info') + f"This file will be auto-deleted in {AUTO_DELETE} minutes."
                    )
                    
                    # Schedule the deletion for both the file and the info message
                    await schedule_message_deletion(context, update.effective_chat.id, sent_msg.message_id, AUTO_DELETE)
                    await schedule_message_deletion(context, update.effective_chat.id, info_msg.message_id, AUTO_DELETE)
            except Exception as e:
                logging.error(f"Error sending file: {e}")
                await update.message.reply_text(mikasa_reply('error') + "Failed to send file!")
        else:
            # File not found, check if it's a batch
            # First try local storage for backward compatibility
            batch_data = None
            try:
                with open(BATCHES_FILE, 'r') as bf:
                    try:
                        batches = json.load(bf)
                        if isinstance(batches, dict) and file_id in batches:
                            batch_data = batches[file_id]
                            logging.info(f"Found batch {file_id} in local storage")
                    except json.JSONDecodeError:
                        pass
            except Exception as e:
                logging.warning(f"Error reading local batch database: {e}")
            
            # If batch not found in local storage, search in links channel
            if not batch_data:
                logging.info(f"Batch {file_id} not found in local storage, searching links channel")
                batch_data = await search_links_channel_for_batch(context, file_id)
                
                if batch_data:
                    logging.info(f"Found batch {file_id} in links channel")
                else:
                    logging.warning(f"Batch {file_id} not found in links channel")
                    await update.message.reply_text(mikasa_reply('warning') + "File or batch not found!")
                    return
            
            if batch_data:
                # Get batch files
                batch_files = []
                
                # Handle different batch data formats
                if isinstance(batch_data, list):
                    # Old format: just a list of file IDs
                    batch_files = batch_data
                    logging.info(f"Processing batch {file_id} with files (old format): {batch_files}")
                elif isinstance(batch_data, dict):
                    # New format: dict with files and metadata
                    if "files" in batch_data and isinstance(batch_data["files"], list):
                        batch_files = batch_data["files"]
                        logging.info(f"Processing batch {file_id} with files (new format): {batch_files}")
                    else:
                        logging.warning(f"Invalid batch data format for {file_id}: {batch_data}")
                else:
                    logging.warning(f"Unrecognized batch data type for {file_id}: {type(batch_data)}")
                
                if not batch_files:
                    await update.message.reply_text(mikasa_reply('warning') + "Invalid batch data!")
                    return
                
                sent_messages = []
                missing_files = []
                
                # Process each file in the batch
                for fid in batch_files:
                    # First try to get file data from local storage
                    file_data = None
                    try:
                        with open(FILE_DATABASE, 'r') as f:
                            try:
                                files = json.load(f)
                                if isinstance(files, dict) and fid in files:
                                    file_data = files[fid]
                                    logging.info(f"Found batch file {fid} in local storage")
                            except json.JSONDecodeError:
                                pass
                    except Exception as e:
                        logging.warning(f"Error reading local file database for batch file {fid}: {e}")
                    
                    # If file not found in local storage, search in links channel
                    if not file_data or not isinstance(file_data, dict):
                        logging.info(f"Batch file {fid} not found in local storage, searching links channel")
                        file_data = await search_links_channel_for_file(context, fid)
                        
                        if file_data:
                            logging.info(f"Found batch file {fid} in links channel")
                        else:
                            logging.warning(f"Batch file {fid} not found in links channel")
                            missing_files.append(fid)
                            continue
                    
                    # Process the file if found
                    if file_data and isinstance(file_data, dict):
                        message_id = file_data.get("message_id")
                        if not message_id:
                            logging.warning(f"Missing message_id for batch file {fid}")
                            missing_files.append(fid)
                            continue
                        
                        custom_name = file_data.get("custom_name")
                        caption = f"{custom_name}" if custom_name else None
                        
                        try:
                            sent_msg = await context.bot.copy_message(
                                chat_id=update.effective_chat.id,
                                from_chat_id=DATABASE_CHANNEL,
                                message_id=message_id,
                                caption=caption,
                                protect_content=True
                            )
                            sent_messages.append(sent_msg.message_id)
                            logging.info(f"Sent batch file {fid} (message ID {message_id}) to user {user_id}")
                        except Exception as e:
                            logging.error(f"Error sending batch file {fid}: {e}")
                            missing_files.append(fid)
                        
                        await asyncio.sleep(1)  # Prevent flooding
                    else:
                        logging.warning(f"Invalid data for batch file {fid}")
                        missing_files.append(fid)
                
                # Notify user about missing files if any
                if missing_files:
                    await update.message.reply_text(
                        mikasa_reply('warning') + f"Some files in this batch ({len(missing_files)} of {len(batch_files)}) could not be found."
                    )
                
                # Schedule auto-delete for batch files if enabled
                if AUTO_DELETE > 0 and sent_messages:
                    # Add notification about auto-delete
                    info_msg = await update.message.reply_text(
                        mikasa_reply('info') + f"These files will be auto-deleted in {AUTO_DELETE} minutes."
                    )
                    
                    # Schedule deletion for each message in the batch and the info message
                    for msg_id in sent_messages:
                        await schedule_message_deletion(context, update.effective_chat.id, msg_id, AUTO_DELETE)
                    
                    await schedule_message_deletion(context, update.effective_chat.id, info_msg.message_id, AUTO_DELETE)
                
                if not sent_messages:
                    await update.message.reply_text(mikasa_reply('warning') + "No valid files in batch!")
            else:
                await update.message.reply_text(mikasa_reply('warning') + "File or batch not found!")
    except Exception as e:
        logging.error(f"Error in send_file: {e}")
        await update.message.reply_text(mikasa_reply('error') + "An error occurred while processing your request!")

# ========== USER INTERFACE ========== #
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the main menu"""
    keyboard = [
        [InlineKeyboardButton("📚 Help", callback_data="help")],
        [InlineKeyboardButton("ℹ️ About", callback_data="about")],
        [InlineKeyboardButton("🔍 Search Files", callback_data="search_menu")]
    ]
    
    # Add admin options if user is admin
    if update.effective_user.id in ADMINS:
        keyboard.extend([
            [InlineKeyboardButton("🔄 Start Batch", callback_data="start_batch"),
             InlineKeyboardButton("✅ End Batch", callback_data="end_batch")],
            [InlineKeyboardButton("✏️ Rename File", callback_data="rename_file")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")]
        ])
    
    # Add owner-only options
    if update.effective_user.id == OWNER_ID:
        keyboard.append([
            InlineKeyboardButton("🛠️ Customize Bot", callback_data="customize"),
            InlineKeyboardButton("🧹 Cleanup", callback_data="cleanup")
        ])
    
    # Add group options if in a group chat
    if update.effective_chat.type in ["group", "supergroup"]:
        keyboard.append([InlineKeyboardButton("📊 Group Stats", callback_data="group_stats")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        mikasa_reply('info') + "Main Menu:",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "menu":
        keyboard = [
            [InlineKeyboardButton("📚 Help", callback_data="help")],
            [InlineKeyboardButton("ℹ️ About", callback_data="about")],
            [InlineKeyboardButton("🔍 Search Files", callback_data="search_menu")]
        ]
        
        # Add admin options if user is admin
        if query.from_user.id in ADMINS:
            keyboard.extend([
                [InlineKeyboardButton("🔄 Start Batch", callback_data="start_batch"),
                 InlineKeyboardButton("✅ End Batch", callback_data="end_batch")],
                [InlineKeyboardButton("✏️ Rename File", callback_data="rename_file")],
                [InlineKeyboardButton("⚙️ Settings", callback_data="settings")]
            ])
        
        # Add owner-only options
        if query.from_user.id == OWNER_ID:
            keyboard.append([
                InlineKeyboardButton("🛠️ Customize Bot", callback_data="customize"),
                InlineKeyboardButton("🧹 Cleanup", callback_data="cleanup")
            ])
        
        # Add group options if in a group chat
        if query.message.chat.type in ["group", "supergroup"]:
            keyboard.append([InlineKeyboardButton("📊 Group Stats", callback_data="group_stats")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            mikasa_reply('info') + "Main Menu:",
            reply_markup=reply_markup
        )
    
    elif query.data == "help":
        help_text = (
            f"{mikasa_reply('info')}Help Information:\n\n"
            "• To access a file, use the provided link\n"
            "• Files will auto-delete after the configured time\n"
            "• You need a valid token to access files\n"
            "• Use /search <keywords> to find files in any chat\n\n"
            "Commands:\n"
            "/start - Start the bot\n"
            "/menu - Show main menu\n"
            "/help - Show this help message\n"
            "/search - Search for files by keywords, caption, link, or date\n"
            "/groupstats - View statistics for this group (in group chats)"
        )
        
        # Add admin commands if user is admin
        if query.from_user.id in ADMINS:
            help_text += (
                "\n\nAdmin Commands:\n"
                "/getlink - Store a file and get sharing link\n"
                "/firstbatch - Start collecting files for a batch\n"
                "/lastbatch - End batch collection and get link\n"
                "/rename - Set custom name for next file\n"
                "/ban <user_id> - Ban a user\n"
                "/unban <user_id> - Unban a user\n"
                "/listbanned - List banned users\n"
                "/settings - Show current settings\n"
                "/restart - Restart the bot"
            )
        
        # Add owner commands if user is owner
        if query.from_user.id == OWNER_ID:
            help_text += (
                "\n\nOwner Commands:\n"
                "/customize - Modify bot settings without redeploying\n"
                "/cleanup - Clean all metadata from owner's device"
            )
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(help_text, reply_markup=reply_markup)
    
    elif query.data == "about":
        about_text = (
            f"{mikasa_reply('info')}About This Bot:\n\n"
            "This is a file sharing bot with auto-delete capability, "
            "token verification, and batch file sharing.\n\n"
            "Features:\n"
            "• Secure file sharing\n"
            "• Auto-deletion after specified time\n"
            "• Token verification system\n"
            "• Batch file sharing\n"
            "• Advanced keyword search functionality\n"
            "• Group chat support\n"
            "• Group statistics tracking\n"
            "• Customizable settings"
        )
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(about_text, reply_markup=reply_markup)
    
    elif query.data == "settings" and query.from_user.id in ADMINS:
        settings_text = f"""
{mikasa_reply('info')}⚙️ Current Settings:
• Auto-delete: {AUTO_DELETE} mins
• Force Sub: {FORCE_SUB if FORCE_SUB else 'Disabled'}
• Admins: {', '.join(map(str, ADMINS))}
• Token Duration: {TOKEN_DURATION} hours
• Rename Template: {RENAME_TEMPLATE if RENAME_TEMPLATE else 'Not configured'}
• Get Token URL: {'Configured' if GET_TOKEN else 'Not configured'}
• Links Channel: {'Configured' if LINKS_CHANNEL else 'Not configured'}
"""
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(settings_text, reply_markup=reply_markup)
    
    elif query.data == "customize" and query.from_user.id == OWNER_ID:
        # Redirect to the customize command
        await query.edit_message_text(mikasa_reply('info') + "Please use /customize command to modify bot settings.")
    
    elif query.data == "cleanup" and query.from_user.id == OWNER_ID:
        # Redirect to the cleanup command
        await query.edit_message_text(mikasa_reply('info') + "Please use /cleanup command to clean metadata from your device.")
    
    elif query.data == "search_menu":
        search_text = (
            f"{mikasa_reply('info')}🔍 Search Options:\n\n"
            "You can search files by:\n"
            "• Keywords in filename or caption\n"
            "• Date (format: YYYY-MM-DD)\n"
            "• Link\n\n"
            "Examples:\n"
            "/search anime\n"
            "/search date:2025-04-01\n"
            "/search link:t.me\n\n"
            "Use /search command followed by your search terms."
        )
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(search_text, reply_markup=reply_markup)
    
    elif query.data == "group_stats":
        # Get group stats
        chat_id = str(query.message.chat.id)
        stats = await get_group_stats(chat_id, context)
        
        stats_text = (
            f"{mikasa_reply('info')}📊 Group Statistics:\n\n"
            f"• Total Files Shared: {stats['total_files']}\n"
            f"• Total Searches: {stats['total_searches']}\n"
            f"• Active Members: {stats['active_members']}\n"
            f"• Most Active User: {stats['most_active_user']}\n"
            f"• Most Searched Term: {stats['most_searched_term']}\n"
            f"• Last Activity: {stats['last_activity']}"
        )
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(stats_text, reply_markup=reply_markup)
    
    elif query.data == "start_batch" and query.from_user.id in ADMINS:
        context.user_data['batch'] = []
        logging.info(f"Started new batch for user {query.from_user.id} via menu")
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            mikasa_reply('success') + "Batch collection started! Send files to add to the batch.",
            reply_markup=reply_markup
        )
    
    elif query.data == "end_batch" and query.from_user.id in ADMINS:
        if 'batch' not in context.user_data or not context.user_data['batch']:
            keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                mikasa_reply('warning') + "No active batch!",
                reply_markup=reply_markup
            )
            return
        
        batch_files = context.user_data['batch']
        logging.info(f"Ending batch with {len(batch_files)} files via menu: {batch_files}")
        
        batch_id = str(uuid.uuid4())
        try:
            with open(BATCHES_FILE, 'r+') as f:
                try:
                    batches = json.load(f)
                    if not isinstance(batches, dict):
                        batches = {}
                except json.JSONDecodeError:
                    batches = {}
                
                batches[batch_id] = batch_files
                f.seek(0)
                f.truncate()
                json.dump(batches, f)
                logging.info(f"Saved batch {batch_id} with files: {batch_files}")
        except Exception as e:
            logging.error(f"Error saving batch: {e}")
            keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                mikasa_reply('error') + "Failed to save batch!",
                reply_markup=reply_markup
            )
            return
        
        batch_link = f"t.me/{context.bot.username}?start={batch_id}"
        
        # Store complete batch metadata in links channel if configured
        link_msg_id = None
        if LINKS_CHANNEL:
            try:
                # Include ALL metadata in the links channel message
                date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # Format the file list for display
                files_str = ", ".join(batch_files)
                if len(files_str) > 100:  # Truncate if too long
                    files_str = files_str[:97] + "..."
                
                # Enhanced metadata format with ALL necessary information
                link_msg = await context.bot.send_message(
                    chat_id=LINKS_CHANNEL,
                    text=f"🔗 Batch Link\n\n"
                         f"ID: {batch_id}\n"
                         f"Files: {files_str}\n"
                         f"Date: {date_str}\n"
                         f"Total Files: {len(batch_files)}\n"
                         f"Link: {batch_link}\n\n"
                         f"#batch_{batch_id}\n"
                         f"#batch_files_{','.join(batch_files)}"  # Store file IDs in a searchable format
                )
                link_msg_id = link_msg.message_id
                logging.info(f"Stored complete batch metadata in links channel, message ID: {link_msg_id}")
            except Exception as e:
                logging.error(f"Failed to store batch metadata in links channel: {e}")
        
        # For backward compatibility, still store batch data locally
        # This will be phased out in future versions
        try:
            with open(BATCHES_FILE, 'r+') as f:
                try:
                    batches = json.load(f)
                    if not isinstance(batches, dict):
                        batches = {}
                except json.JSONDecodeError:
                    batches = {}
                
                # Store batch data with enhanced metadata
                batches[batch_id] = {
                    "files": batch_files,
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "total_files": len(batch_files),
                    "links_channel_msg_id": link_msg_id
                }
                
                f.seek(0)
                f.truncate()
                json.dump(batches, f)
                logging.info(f"Stored batch metadata locally for backward compatibility")
        except Exception as e:
            # If local storage fails, it's not critical anymore since we have complete metadata in links channel
            logging.warning(f"Failed to store local batch metadata, but links channel storage succeeded: {e}")
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            mikasa_reply('success') + f"Batch stored!\nShare link:\n{batch_link}",
            reply_markup=reply_markup
        )
        context.user_data.pop('batch')
    
    elif query.data == "rename_file" and query.from_user.id in ADMINS:
        # Set awaiting_rename flag
        context.user_data['awaiting_rename'] = True
        logging.info(f"User {query.from_user.id} is now awaiting rename via menu")
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            mikasa_reply('info') + "Please send a message with the new name for the next file.",
            reply_markup=reply_markup
        )

# ========== GROUP AUTO-DELETE COMMANDS ========== #
@admin_only
async def set_auto_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to set auto-delete time for a group"""
    # Check if this is a group chat
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            mikasa_reply('warning') + "This command can only be used in group chats."
        )
        return
    
    # Check if user is an admin of the group
    try:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Check if user is in the bot's admin list
        if user_id not in ADMINS:
            await update.message.reply_text(
                mikasa_reply('warning') + "You must be in the bot's admin list to use this command."
            )
            return
        
        # Check if user is also a group admin
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['administrator', 'creator']:
            await update.message.reply_text(
                mikasa_reply('warning') + "You must be a group admin to use this command."
            )
            return
        
        # Get the minutes from command arguments
        if not context.args:
            # If no arguments, show current setting
            current_time = await get_group_auto_delete_time(chat_id)
            if current_time > 0:
                await update.message.reply_text(
                    mikasa_reply('info') + f"Current auto-delete time for this group is {current_time} minutes.\n\n"
                    "To change it, use: /setautodelete <minutes>"
                )
            else:
                await update.message.reply_text(
                    mikasa_reply('info') + "Auto-delete is currently disabled for this group.\n\n"
                    "To enable it, use: /setautodelete <minutes>"
                )
            return
        
        try:
            minutes = int(context.args[0])
            if minutes < 0:
                await update.message.reply_text(
                    mikasa_reply('warning') + "Auto-delete time cannot be negative."
                )
                return
            
            # Set the auto-delete time for this group
            success = await set_group_auto_delete_time(chat_id, minutes)
            
            if success:
                if minutes > 0:
                    await update.message.reply_text(
                        mikasa_reply('success') + f"Auto-delete time for this group has been set to {minutes} minutes."
                    )
                else:
                    await update.message.reply_text(
                        mikasa_reply('success') + "Auto-delete has been disabled for this group."
                    )
            else:
                await update.message.reply_text(
                    mikasa_reply('error') + "Failed to set auto-delete time. Please try again."
                )
        except ValueError:
            await update.message.reply_text(
                mikasa_reply('warning') + "Invalid time value. Please provide a number in minutes."
            )
    except Exception as e:
        logging.error(f"Error in set_auto_delete_command: {e}")
        await update.message.reply_text(
            mikasa_reply('error') + f"An error occurred: {str(e)}"
        )

async def get_auto_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to get current auto-delete time for a group"""
    # Check if this is a group chat
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            mikasa_reply('warning') + "This command can only be used in group chats."
        )
        return
    
    try:
        chat_id = update.effective_chat.id
        
        # Get the current auto-delete time for this group
        current_time = await get_group_auto_delete_time(chat_id)
        
        if current_time > 0:
            await update.message.reply_text(
                mikasa_reply('info') + f"Auto-delete time for this group is set to {current_time} minutes."
            )
        else:
            await update.message.reply_text(
                mikasa_reply('info') + "Auto-delete is currently disabled for this group."
            )
    except Exception as e:
        logging.error(f"Error in get_auto_delete_command: {e}")
        await update.message.reply_text(
            mikasa_reply('error') + f"An error occurred: {str(e)}"
        )

# ========== GROUP FEATURES ========== #
async def group_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome new members to the group"""
    if not update.message or not update.message.new_chat_members:
        return
    
    # Welcome each new member
    for new_member in update.message.new_chat_members:
        # Skip if the new member is the bot itself
        if new_member.id == context.bot.id:
            continue
        
        welcome_message = (
            f"{mikasa_reply('welcome')}Welcome to the group, {new_member.first_name}!\n\n"
            "• Use /search to find files by keywords, date, or link\n"
            "• Use /groupstats to see group statistics\n"
            "• Use /autodelete to check current auto-delete setting\n"
            "• Use /help to see all available commands"
        )
        
        await update.message.reply_text(welcome_message)

async def group_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show group statistics"""
    # Check if this is a group chat
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            mikasa_reply('warning') + "This command can only be used in group chats."
        )
        return
    
    chat_id = str(update.effective_chat.id)
    stats = await get_group_stats(chat_id, context)
    
    # Get auto-delete time for this group
    auto_delete_time = await get_group_auto_delete_time(int(chat_id))
    auto_delete_status = f"{auto_delete_time} minutes" if auto_delete_time > 0 else "Disabled"
    
    stats_text = (
        f"{mikasa_reply('info')}📊 Group Statistics:\n\n"
        f"• Total Files Shared: {stats['total_files']}\n"
        f"• Total Searches: {stats['total_searches']}\n"
        f"• Active Members: {stats['active_members']}\n"
        f"• Most Active User: {stats['most_active_user']}\n"
        f"• Most Searched Term: {stats['most_searched_term']}\n"
        f"• Auto-Delete Setting: {auto_delete_status}\n"
        f"• Last Activity: {stats['last_activity']}"
    )
    
    await update.message.reply_text(stats_text)
async def update_group_stats(chat_id, action_type, user_id=None, search_term=None):
    """Update statistics for a group chat"""
    try:
        with open(GROUP_STATS_FILE, 'r+') as f:
            try:
                stats = json.load(f)
                if not isinstance(stats, dict):
                    stats = {}
            except json.JSONDecodeError:
                stats = {}
            
            # Initialize group stats if not exists
            if chat_id not in stats:
                stats[chat_id] = {
                    "total_files": 0,
                    "total_searches": 0,
                    "active_members": {},
                    "search_terms": {},
                    "last_activity": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            
            # Update stats based on action type
            if action_type == "file":
                stats[chat_id]["total_files"] += 1
            elif action_type == "search":
                stats[chat_id]["total_searches"] += 1
                if search_term:
                    if search_term not in stats[chat_id]["search_terms"]:
                        stats[chat_id]["search_terms"][search_term] = 0
                    stats[chat_id]["search_terms"][search_term] += 1
            
            # Update active members
            if user_id:
                user_id_str = str(user_id)
                if user_id_str not in stats[chat_id]["active_members"]:
                    stats[chat_id]["active_members"][user_id_str] = 0
                stats[chat_id]["active_members"][user_id_str] += 1
            
            # Update last activity
            stats[chat_id]["last_activity"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Write updated stats back to file
            f.seek(0)
            f.truncate()
            json.dump(stats, f)
            
            logging.info(f"Updated group stats for chat {chat_id}, action: {action_type}")
    except Exception as e:
        logging.error(f"Error updating group stats: {e}")

async def get_group_stats(chat_id, context):
    """Get group statistics"""
    try:
        with open(GROUP_STATS_FILE, 'r') as f:
            try:
                stats = json.load(f)
                if not isinstance(stats, dict):
                    stats = {}
            except json.JSONDecodeError:
                stats = {}
            
            # Return default stats if group not found
            if chat_id not in stats:
                return {
                    "total_files": 0,
                    "total_searches": 0,
                    "active_members": 0,
                    "most_active_user": "None",
                    "most_searched_term": "None",
                    "last_activity": "Never"
                }
            
            group_stats = stats[chat_id]
            
            # Get most active user
            most_active_user = "None"
            max_activity = 0
            for user_id, activity in group_stats.get("active_members", {}).items():
                if activity > max_activity:
                    max_activity = activity
                    try:
                        user = await context.bot.get_chat_member(int(chat_id), int(user_id))
                        most_active_user = user.user.first_name
                    except:
                        most_active_user = f"User {user_id}"
            
            # Get most searched term
            most_searched_term = "None"
            max_searches = 0
            for term, count in group_stats.get("search_terms", {}).items():
                if count > max_searches:
                    max_searches = count
                    most_searched_term = term
            
            return {
                "total_files": group_stats.get("total_files", 0),
                "total_searches": group_stats.get("total_searches", 0),
                "active_members": len(group_stats.get("active_members", {})),
                "most_active_user": most_active_user,
                "most_searched_term": most_searched_term,
                "last_activity": group_stats.get("last_activity", "Never")
            }
    except Exception as e:
        logging.error(f"Error getting group stats: {e}")
        return {
            "total_files": 0,
            "total_searches": 0,
            "active_members": 0,
            "most_active_user": "Error",
            "most_searched_term": "Error",
            "last_activity": "Error"
        }

# ========== ADMIN COMMANDS ========== #
@admin_only
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(mikasa_reply('warning') + "Provide user ID!")
        return
    
    try:
        user_id = int(context.args[0])
        with open(BANNED_USERS_FILE, 'r+') as f:
            try:
                banned = json.load(f)
                if not isinstance(banned, list):
                    banned = []
            except json.JSONDecodeError:
                banned = []
            
            if user_id in banned or str(user_id) in banned:
                await update.message.reply_text(mikasa_reply('warning') + "Already banned!")
            else:
                banned.append(user_id)
                f.seek(0)
                f.truncate()
                json.dump(banned, f)
                await update.message.reply_text(mikasa_reply('ban') + f"Banned {user_id}!")
    except ValueError:
        await update.message.reply_text(mikasa_reply('warning') + "Invalid ID!")
    except Exception as e:
        logging.error(f"Error banning user: {e}")
        await update.message.reply_text(mikasa_reply('error') + "Failed to ban user!")

@admin_only
async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(mikasa_reply('warning') + "Provide user ID!")
        return
    
    try:
        user_id = int(context.args[0])
        with open(BANNED_USERS_FILE, 'r+') as f:
            try:
                banned = json.load(f)
                if not isinstance(banned, list):
                    banned = []
            except json.JSONDecodeError:
                banned = []
            
            if user_id in banned:
                banned.remove(user_id)
                f.seek(0)
                f.truncate()
                json.dump(banned, f)
                await update.message.reply_text(mikasa_reply('unban') + f"Unbanned {user_id}!")
            elif str(user_id) in banned:
                banned.remove(str(user_id))
                f.seek(0)
                f.truncate()
                json.dump(banned, f)
                await update.message.reply_text(mikasa_reply('unban') + f"Unbanned {user_id}!")
            else:
                await update.message.reply_text(mikasa_reply('warning') + "User not banned!")
    except ValueError:
        await update.message.reply_text(mikasa_reply('warning') + "Invalid ID!")
    except Exception as e:
        logging.error(f"Error unbanning user: {e}")
        await update.message.reply_text(mikasa_reply('error') + "Failed to unban user!")

@admin_only
async def list_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with open(BANNED_USERS_FILE, 'r') as f:
            try:
                banned = json.load(f)
                if not isinstance(banned, list):
                    banned = []
            except json.JSONDecodeError:
                banned = []
            
            if banned:
                await update.message.reply_text(mikasa_reply('info') + f"Banned users: {', '.join(map(str, banned))}")
            else:
                await update.message.reply_text(mikasa_reply('info') + "No banned users!")
    except Exception as e:
        logging.error(f"Error listing banned users: {e}")
        await update.message.reply_text(mikasa_reply('error') + "Failed to list banned users!")

@admin_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings_msg = f"""
{mikasa_reply('info')}⚙️ Current Settings:
• Auto-delete: {AUTO_DELETE} mins
• Force Sub: {FORCE_SUB if FORCE_SUB else 'Disabled'}
• Admins: {', '.join(map(str, ADMINS))}
• Token Duration: {TOKEN_DURATION} hours
• Rename Template: {RENAME_TEMPLATE if RENAME_TEMPLATE else 'Not configured'}
• Get Token URL: {'Configured' if GET_TOKEN else 'Not configured'}
• Links Channel: {'Configured' if LINKS_CHANNEL else 'Not configured'}
"""
    await update.message.reply_text(settings_msg)

@admin_only
async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(mikasa_reply('default') + "Rebooting...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help information"""
    help_text = (
        f"{mikasa_reply('info')}Help Information:\n\n"
        "• To access a file, use the provided link\n"
        "• Files will auto-delete after the configured time\n"
        "• You need a valid token to access files\n"
        "• Use /search <keywords> to find files in any chat\n\n"
        "Commands:\n"
        "/start - Start the bot\n"
        "/menu - Show main menu\n"
        "/help - Show this help message\n"
        "/search - Search for files by keywords, caption, link, or date\n"
    )
    
    # Add group commands if in a group
    if update.effective_chat.type in ["group", "supergroup"]:
        help_text += (
            "/groupstats - View statistics for this group\n"
            "/autodelete - Check current auto-delete setting for this group\n"
        )
        
        # Check if user is both in admin list and a group admin
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            
            if user_id in ADMINS:
                member = await context.bot.get_chat_member(chat_id, user_id)
                if member.status in ['administrator', 'creator']:
                    help_text += "/setautodelete <minutes> - Set auto-delete time for this group (0 to disable)\n"
        except Exception as e:
            logging.error(f"Error checking admin status in help command: {e}")
    
    # Add admin commands if user is admin
    if update.effective_user.id in ADMINS:
        help_text += (
            "\n\nAdmin Commands:\n"
            "/getlink - Store a file and get sharing link\n"
            "/firstbatch - Start collecting files for a batch\n"
            "/lastbatch - End batch collection and get link\n"
            "/rename - Set custom name for next file\n"
            "/ban <user_id> - Ban a user\n"
            "/unban <user_id> - Unban a user\n"
            "/listbanned - List banned users\n"
            "/settings - Show current settings\n"
            "/restart - Restart the bot"
        )
    
    # Add owner commands if user is owner
    if update.effective_user.id == OWNER_ID:
        help_text += (
            "\n\nOwner Commands:\n"
            "/customize - Modify bot settings without redeploying\n"
            "/cleanup - Clean all metadata from owner's device"
        )
    
    keyboard = [[InlineKeyboardButton("📋 Main Menu", callback_data="menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(help_text, reply_markup=reply_markup)

# ========== INLINE KEYBOARD FOR AUTO-DELETE SETTINGS ========== #
async def auto_delete_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show inline keyboard with auto-delete setting options"""
    # Check if this is a group chat
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            mikasa_reply('warning') + "This command can only be used in group chats."
        )
        return
    
    # Check if user is an admin of the group
    try:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Check if user is in the bot's admin list
        if user_id not in ADMINS:
            await update.message.reply_text(
                mikasa_reply('warning') + "You must be in the bot's admin list to use this command."
            )
            return
        
        # Check if user is also a group admin
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['administrator', 'creator']:
            await update.message.reply_text(
                mikasa_reply('warning') + "You must be a group admin to use this command."
            )
            return
        
        # Get current auto-delete time
        current_time = await get_group_auto_delete_time(chat_id)
        
        # Create keyboard with preset options
        keyboard = [
            [InlineKeyboardButton("5 minutes", callback_data="autodel_5"),
             InlineKeyboardButton("10 minutes", callback_data="autodel_10")],
            [InlineKeyboardButton("30 minutes", callback_data="autodel_30"),
             InlineKeyboardButton("1 hour", callback_data="autodel_60")],
            [InlineKeyboardButton("2 hours", callback_data="autodel_120"),
             InlineKeyboardButton("Disable", callback_data="autodel_0")],
            [InlineKeyboardButton("Custom...", callback_data="autodel_custom")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"{mikasa_reply('info')}Auto-Delete Settings\n\n"
            f"Current setting: {current_time} minutes\n\n"
            "Select a new auto-delete time for messages in this group:",
            reply_markup=reply_markup
        )
    except Exception as e:
        logging.error(f"Error in auto_delete_settings_menu: {e}")
        await update.message.reply_text(
            mikasa_reply('error') + f"An error occurred: {str(e)}"
        )

async def auto_delete_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle auto-delete setting button callbacks"""
    query = update.callback_query
    await query.answer()
    
    # Extract the minutes from the callback data
    match = re.match(r"autodel_(\d+|custom)", query.data)
    if not match:
        return
    
    option = match.group(1)
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    
    # Check if user is in the bot's admin list
    if user_id not in ADMINS:
        await query.edit_message_text(
            mikasa_reply('warning') + "You must be in the bot's admin list to change this setting."
        )
        return
    
    # Check if user is also a group admin
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ['administrator', 'creator']:
            await query.edit_message_text(
                mikasa_reply('warning') + "You must be a group admin to change this setting."
            )
            return
        
        if option == "custom":
            # Ask user to enter custom time
            await query.edit_message_text(
                f"{mikasa_reply('info')}Please use the command:\n\n"
                "/setautodelete <minutes>\n\n"
                "Replace <minutes> with the desired time in minutes (0 to disable)."
            )
            return
        
        # Set the auto-delete time
        minutes = int(option)
        success = await set_group_auto_delete_time(chat_id, minutes)
        
        if success:
            if minutes > 0:
                await query.edit_message_text(
                    mikasa_reply('success') + f"Auto-delete time for this group has been set to {minutes} minutes.\n\n"
                    "All files sent to this group will be automatically deleted after this time."
                )
            else:
                await query.edit_message_text(
                    mikasa_reply('success') + "Auto-delete has been disabled for this group."
                )
        else:
            await query.edit_message_text(
                mikasa_reply('error') + "Failed to set auto-delete time. Please try again."
            )
    except Exception as e:
        logging.error(f"Error in auto_delete_button_handler: {e}")
        await query.edit_message_text(
            mikasa_reply('error') + f"An error occurred: {str(e)}"
        )
# ========== GROUP CHAT SEARCH FEATURE ========== #
async def search_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search for files by keywords, caption, link, or date in any chat"""
    # Get search query
    if not context.args:
        await update.message.reply_text(
            mikasa_reply('warning') + "Please provide search keywords.\n\n"
            "Examples:\n"
            "/search anime\n"
            "/search date:2025-04-01\n"
            "/search link:t.me"
        )
        return
    
    # Join all arguments to form the search query
    search_query = ' '.join(context.args).lower()
    logging.info(f"Searching for files with query: {search_query}")
    
    # Update group stats if in a group
    if update.effective_chat.type in ["group", "supergroup"]:
        chat_id = str(update.effective_chat.id)
        await update_group_stats(chat_id, "search", update.effective_user.id, search_query)
    
    # Check if this is a date search
    date_search = None
    if search_query.startswith("date:"):
        date_pattern = r"date:(\d{4}-\d{2}-\d{2})"
        match = re.search(date_pattern, search_query)
        if match:
            date_search = match.group(1)
            search_query = search_query.replace(f"date:{date_search}", "").strip()
            logging.info(f"Date search detected: {date_search}")
    
    # Check if this is a link search
    link_search = None
    if search_query.startswith("link:"):
        link_pattern = r"link:([^\s]+)"
        match = re.search(link_pattern, search_query)
        if match:
            link_search = match.group(1)
            search_query = search_query.replace(f"link:{link_search}", "").strip()
            logging.info(f"Link search detected: {link_search}")
    
    try:
        # Load file database
        with open(FILE_DATABASE, 'r') as f:
            try:
                files = json.load(f)
                if not isinstance(files, dict):
                    files = {}
            except json.JSONDecodeError:
                files = {}
        
        # Search for matching files
        matching_files = []
        for file_id, file_data in files.items():
            if not isinstance(file_data, dict):
                continue
            
            # Get file metadata
            custom_name = file_data.get("custom_name", "")
            caption = file_data.get("caption", "")
            file_date = file_data.get("date", "")
            file_link = file_data.get("file_link", "")
            
            # Ensure all values are strings to prevent type errors
            if custom_name is None:
                custom_name = ""
            if caption is None:
                caption = ""
            if file_date is None:
                file_date = ""
            if file_link is None:
                file_link = ""
            
            # Check if search criteria match
            name_match = search_query and search_query in custom_name.lower()
            caption_match = search_query and search_query in caption.lower()
            date_match = date_search and date_search in file_date
            link_match = link_search and link_search in file_link
            
            # If any criteria match, add to results
            if name_match or caption_match or date_match or link_match:
                match_type = []
                if name_match:
                    match_type.append("name")
                if caption_match:
                    match_type.append("caption")
                if date_match:
                    match_type.append("date")
                if link_match:
                    match_type.append("link")
                
                matching_files.append({
                    "file_id": file_id,
                    "name": custom_name or "Unnamed file",
                    "caption": caption,
                    "date": file_date,
                    "match_type": match_type,
                    "media_type": file_data.get("media_type", "unknown")
                })
        
        # Load batch database to include batch names in search
        try:
            with open(BATCHES_FILE, 'r') as f:
                try:
                    batches = json.load(f)
                    if not isinstance(batches, dict):
                        batches = {}
                except json.JSONDecodeError:
                    batches = {}
            
            # For each batch, check if it contains files with matching criteria
            for batch_id, batch_files in batches.items():
                if not isinstance(batch_files, list):
                    continue
                
                batch_matches = []
                match_types = set()
                
                for file_id in batch_files:
                    if file_id in files and isinstance(files[file_id], dict):
                        custom_name = files[file_id].get("custom_name", "")
                        caption = files[file_id].get("caption", "")
                        file_date = files[file_id].get("date", "")
                        file_link = files[file_id].get("file_link", "")
                        
                        # Ensure all values are strings to prevent type errors
                        if custom_name is None:
                            custom_name = ""
                        if caption is None:
                            caption = ""
                        if file_date is None:
                            file_date = ""
                        if file_link is None:
                            file_link = ""
                        
                        # Check if search criteria match
                        name_match = search_query and search_query in custom_name.lower()
                        caption_match = search_query and search_query in caption.lower()
                        date_match = date_search and file_date and date_search in file_date
                        link_match = link_search and file_link and link_search in file_link
                        
                        if name_match or caption_match or date_match or link_match:
                            batch_matches.append(custom_name or "Unnamed file")
                            
                            if name_match:
                                match_types.add("name")
                            if caption_match:
                                match_types.add("caption")
                            if date_match:
                                match_types.add("date")
                            if link_match:
                                match_types.add("link")
                
                if batch_matches:
                    # Add batch to matching files
                    matching_files.append({
                        "file_id": batch_id,
                        "name": f"Batch ({len(batch_matches)} files): {', '.join(batch_matches[:3])}" + 
                               (f" and {len(batch_matches) - 3} more" if len(batch_matches) > 3 else ""),
                        "is_batch": True,
                        "match_type": list(match_types)
                    })
        except Exception as e:
            logging.error(f"Error searching batches: {e}")
        
        # Sort matching files by name
        matching_files.sort(key=lambda x: x["name"])
        
        if not matching_files:
            search_criteria = []
            if search_query:
                search_criteria.append(f"keywords: '{search_query}'")
            if date_search:
                search_criteria.append(f"date: '{date_search}'")
            if link_search:
                search_criteria.append(f"link: '{link_search}'")
            
            criteria_text = " and ".join(search_criteria)
            
            await update.message.reply_text(
                mikasa_reply('info') + f"No files found matching {criteria_text}."
            )
            return
        
        # Create a user-friendly response with inline keyboard
        search_criteria = []
        if search_query:
            search_criteria.append(f"keywords: '{search_query}'")
        if date_search:
            search_criteria.append(f"date: '{date_search}'")
        if link_search:
            search_criteria.append(f"link: '{link_search}'")
        
        criteria_text = " and ".join(search_criteria)
        
        response_text = f"{mikasa_reply('success')}🔍 Search Results for {criteria_text}:\n\n"
        
        # Create inline keyboard with file links
        keyboard = []
        for i, file in enumerate(matching_files[:10]):  # Limit to 10 results
            file_id = file["file_id"]
            name = file["name"]
            is_batch = file.get("is_batch", False)
            match_type = file.get("match_type", [])
            media_type = file.get("media_type", "unknown")
            
            # Add file name and match type to response text
            match_type_text = f" (matched in: {', '.join(match_type)})" if match_type else ""
            media_icon = get_media_icon(media_type)
            
            response_text += f"{i+1}. {media_icon} {name}{match_type_text}\n"
            
            # Create button for this file
            file_link = f"t.me/{context.bot.username}?start={file_id}"
            button_text = f"{'📁 Batch' if is_batch else '📄 File'} {i+1}"
            keyboard.append([InlineKeyboardButton(button_text, url=file_link)])
        
        # Add note if there are more results
        if len(matching_files) > 10:
            response_text += f"\n...and {len(matching_files) - 10} more results."
            # Add button to refine search
            keyboard.append([InlineKeyboardButton("🔍 Refine Search", callback_data=f"refine_{search_query}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send response
        await update.message.reply_text(
            response_text,
            reply_markup=reply_markup
        )
    except Exception as e:
        logging.error(f"Error searching files: {e}")
        await update.message.reply_text(
            mikasa_reply('error') + f"An error occurred while searching for files: {str(e)}"
        )

def get_media_icon(media_type):
    """Return an appropriate icon for the media type"""
    icons = {
        "photo": "🖼️",
        "video": "🎬",
        "audio": "🎵",
        "document": "📄",
        "animation": "🎭",
        "voice": "🎤",
        "video_note": "⭕",
        "sticker": "🏷️"
    }
    return icons.get(media_type, "📁")

# Add this function to handle refine search callbacks
async def refine_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle refine search callbacks"""
    query = update.callback_query
    await query.answer()
    
    # Extract the original search query
    match = re.match(r"refine_(.*)", query.data)
    if not match:
        return
    
    original_query = match.group(1)
    
    # Ask user to refine their search
    await query.edit_message_text(
        f"{mikasa_reply('info')}Your search for '{original_query}' returned too many results.\n\n"
        "Try using more specific search options:\n"
        "• /search <specific keywords>\n"
        "• /search date:YYYY-MM-DD\n"
        "• /search link:t.me\n\n"
        "You can also combine search options, for example:\n"
        "/search anime date:2025-04-01"
    )

# ========== OWNER CLEANUP COMMAND ========== #
@owner_only
async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clean all metadata from owner's device while preserving active tokens and link data"""
    try:
        # List of files to clean
        files_to_clean = [
            BANNED_USERS_FILE,
            PENDING_DELETES_FILE,
            GROUP_STATS_FILE,
            GROUP_SETTINGS_FILE
        ]
        
        # Files that contain link data that needs to be preserved
        link_data_files = [
            FILE_DATABASE,
            BATCHES_FILE
        ]
        
        cleaned_files = []
        preserved_files = []
        
        # Backup files before cleaning
        backup_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs("backups", exist_ok=True)
        
        # Handle tokens file separately to preserve active tokens
        active_tokens = {}
        if os.path.exists(TOKENS_FILE):
            try:
                # Create backup of tokens file
                backup_file = f"backups/{TOKENS_FILE}_{backup_time}.bak"
                with open(TOKENS_FILE, 'r') as src, open(backup_file, 'w') as dst:
                    dst.write(src.read())
                
                # Read current tokens
                with open(TOKENS_FILE, 'r') as f:
                    try:
                        tokens = json.load(f)
                        if isinstance(tokens, dict):
                            current_time = int(time.time())
                            # Keep only active tokens
                            for token, data in tokens.items():
                                if isinstance(data, dict) and "expiry" in data:
                                    if data["expiry"] > current_time:
                                        active_tokens[token] = data
                                        logging.info(f"Preserving active token that expires at {datetime.fromtimestamp(data['expiry']).strftime('%Y-%m-%d %H:%M:%S')}")
                    except json.JSONDecodeError:
                        logging.warning("Tokens file contains invalid JSON")
                
                # Write back only active tokens
                with open(TOKENS_FILE, 'w') as f:
                    json.dump(active_tokens, f)
                
                if active_tokens:
                    logging.info(f"Preserved {len(active_tokens)} active tokens")
                else:
                    logging.info("No active tokens to preserve")
                
                preserved_files.append(f"{TOKENS_FILE} (preserved active tokens)")
            except Exception as e:
                logging.error(f"Error handling tokens file: {e}")
        
        # Handle link data files to preserve file links
        for file in link_data_files:
            if os.path.exists(file):
                try:
                    # Create backup
                    backup_file = f"backups/{file}_{backup_time}.bak"
                    with open(file, 'r') as src, open(backup_file, 'w') as dst:
                        dst.write(src.read())
                    
                    # Read current data
                    with open(file, 'r') as f:
                        try:
                            data = json.load(f)
                            if not isinstance(data, dict):
                                data = {}
                        except json.JSONDecodeError:
                            data = {}
                    
                    # For FILE_DATABASE, preserve only essential link data
                    if file == FILE_DATABASE:
                        preserved_data = {}
                        preserved_count = 0
                        
                        for file_id, file_info in data.items():
                            if isinstance(file_info, dict):
                                # Only keep essential fields for link recognition
                                preserved_data[file_id] = {
                                    "message_id": file_info.get("message_id"),
                                    "file_link": file_info.get("file_link", ""),
                                    "custom_name": file_info.get("custom_name", ""),
                                    "media_type": file_info.get("media_type", "unknown"),
                                    "links_channel_msg_id": file_info.get("links_channel_msg_id")
                                }
                                preserved_count += 1
                        
                        # Write back preserved data
                        with open(file, 'w') as f:
                            json.dump(preserved_data, f)
                        
                        logging.info(f"Preserved link data for {preserved_count} files in {file}")
                        preserved_files.append(f"{file} (preserved link data for {preserved_count} files)")
                    
                    # For BATCHES_FILE, keep all batch data as is
                    elif file == BATCHES_FILE:
                        # No changes needed, just keep the backup
                        preserved_files.append(f"{file} (preserved batch data)")
                        
                except Exception as e:
                    logging.error(f"Error preserving link data in {file}: {e}")
        
        # Clean other files
        for file in files_to_clean:
            if os.path.exists(file):
                # Create backup
                backup_file = f"backups/{file}_{backup_time}.bak"
                try:
                    with open(file, 'r') as src, open(backup_file, 'w') as dst:
                        dst.write(src.read())
                    
                    # Clean the file
                    with open(file, 'w') as f:
                        if file == BANNED_USERS_FILE:
                            json.dump([], f)
                        else:
                            json.dump({}, f)
                    
                    cleaned_files.append(file)
                except Exception as e:
                    logging.error(f"Error cleaning file {file}: {e}")
        
        if cleaned_files or preserved_files:
            active_token_msg = f"\n\nPreserved {len(active_tokens)} active tokens." if TOKENS_FILE in ' '.join(preserved_files) else ""
            preserved_msg = f"\n\nPreserved files: {', '.join(preserved_files)}" if preserved_files else ""
            cleaned_msg = f"\n\nCleaned files: {', '.join(cleaned_files)}" if cleaned_files else ""
            
            # Add information about links channel metadata storage
            links_channel_msg = ""
            if LINKS_CHANNEL:
                links_channel_msg = "\n\n📢 IMPORTANT: Your bot now stores all metadata in the links channel, so it will continue to work even after cleanup. Files and batches can be accessed directly from the links channel without requiring local storage on your device."
            
            await update.message.reply_text(
                mikasa_reply('success') + f"Successfully cleaned metadata while preserving link data and active tokens.{preserved_msg}{cleaned_msg}{active_token_msg}{links_channel_msg}\n\n"
                f"Backups saved in 'backups' folder with timestamp {backup_time}."
            )
        else:
            await update.message.reply_text(
                mikasa_reply('warning') + "No files were cleaned or preserved. Check logs for errors."
            )
    except Exception as e:
        logging.error(f"Error in cleanup command: {e}")
        await update.message.reply_text(
            mikasa_reply('error') + f"An error occurred during cleanup: {str(e)}"
        )

# Handle all messages
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all messages"""
    # Check if update.message exists
    if not update.message:
        logging.warning("Received update with no message attribute")
        return
        
    # Skip command messages - safely check if text attribute exists
    if hasattr(update.message, 'text') and update.message.text and update.message.text.startswith('/'):
        return
    
    # In group chats, don't respond to regular messages
    if update.effective_chat.type in ["group", "supergroup"]:
        # Just return without responding to the message or updating stats
        return
    
    # Only process file storage in private chats
    if update.effective_chat.type == "private":
        # Check if user is admin
        if update.effective_user.id in ADMINS:
            await store_file(update, context)
        else:
            # For non-admin users, just show a help message
            await update.message.reply_text(
                mikasa_reply('info') + "Use /menu to access the bot menu or /help for assistance."
            )

# Modified to check for existing valid tokens before generating a new one
async def post_init(application):
    """Run after application initialization"""
    # Restore pending deletes
    await restore_pending_deletes(application)
    
    # Schedule token refresh job
    application.job_queue.run_repeating(
        refresh_token,
        interval=TOKEN_DURATION * 3600,  # Convert hours to seconds
        first=TOKEN_DURATION * 3600,  # First run after TOKEN_DURATION hours
        name="token_refresh"
    )
    logging.info(f"Scheduled token refresh every {TOKEN_DURATION} hours")
    
    # Check if there's already a valid token before generating a new one
    token_info = get_valid_token()
    
    if token_info:
        token, expiry = token_info
        logging.info(f"Using existing valid token that expires at {datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        # No valid token found, generate a new one
        logging.info("No valid token found, generating initial token")
        await generate_token(0, application)

if __name__ == "__main__":
    # Initialize application with post_init
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
# Register sync command - MOVED HERE AFTER APPLICATION INITIALIZATION
    register_sync_command(application)
    
    # Make ADMINS available in bot_data
    application.bot_data["ADMINS"] = ADMINS

    # Command Handlers
    handlers = [
        CommandHandler("start", start_command),
        CommandHandler("menu", menu_command),
        CommandHandler("help", help_command),
        CommandHandler("getlink", store_file),
        CommandHandler("firstbatch", start_batch),
        CommandHandler("lastbatch", end_batch),
        CommandHandler("rename", rename_file),
        CommandHandler("ban", ban_user),
        CommandHandler("unban", unban_user),
        CommandHandler("listbanned", list_banned),
        CommandHandler("settings", settings_command),
        CommandHandler("restart", restart),
        CommandHandler("search", search_files),  # Works in all chat types now
        CommandHandler("cleanup", cleanup_command),  # New cleanup command
        CommandHandler("groupstats", group_stats_command),  # New group stats command
        CommandHandler("tokentoggle", token_toggle_command),  # New token toggle command
        CommandHandler("setautodelete", set_auto_delete_command),  # New auto-delete command
        CommandHandler("autodelete", get_auto_delete_command),  # New auto-delete info command
        CommandHandler("autodeletesetup", auto_delete_settings_menu),  # New auto-delete menu command
        
        # Customize command conversation handler
        ConversationHandler(
            entry_points=[CommandHandler("customize", customize_command)],
            states={
                SELECTING_VARIABLE: [CallbackQueryHandler(customize_button_handler, pattern=r"^customize_")],
                ENTERING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, customize_value_handler)]
            },
            fallbacks=[CallbackQueryHandler(customize_button_handler, pattern=r"^customize_cancel$")],
            per_message=False
        ),
        
        # Group welcome handler
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, group_welcome),
        
        CallbackQueryHandler(auto_delete_button_handler, pattern=r"^autodel_"),
        CallbackQueryHandler(refine_search_handler, pattern=r"^refine_"),
        CallbackQueryHandler(button_handler),
        MessageHandler(filters.ALL & ~filters.COMMAND, message_handler)
    ]
    
    for handler in handlers:
        application.add_handler(handler)
    
    application.add_error_handler(error_handler)
    
    print("⚔️ TATAKAE")
    
    # Start the bot
    application.run_polling()
