import os
import logging
import sqlite3
import re
import io
import json
from pathlib import Path

# Third-party libraries
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import TimedOut, TelegramError

# Google Drive Imports
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

# --- Configuration and Setup ---
# Use a data directory that works with Render's persistent disk
DATA_DIR = Path(os.getenv("RENDER_DISK_PATH", ".")) / "bot_data"
# This will create /var/data/bot_data, which is a permitted operation
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID")

# Check for essential environment variables
if not all([TELEGRAM_BOT_TOKEN, GOOGLE_DRIVE_ROOT_FOLDER_ID]):
    raise ValueError("TELEGRAM_BOT_TOKEN or GOOGLE_DRIVE_ROOT_FOLDER_ID is missing from your environment variables.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- State definitions for ConversationHandler ---
SELECT_YEAR, GET_NAME, MAIN_MENU = range(3)

# --- Helper Function for Markdown ---
def escape_markdown(text: str) -> str:
    """Escapes special characters for Telegram's MarkdownV2."""
    if not isinstance(text, str):
        text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# --- Database Management (Caching with Year) ---
DB_FILE = DATA_DIR / "file_cache.db"

def setup_database():
    """Initializes the SQLite database for caching."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS assignment_cache (
            id INTEGER PRIMARY KEY, year TEXT, branch TEXT, subject TEXT, assignment_number INTEGER,
            telegram_file_id TEXT, UNIQUE(year, branch, subject, assignment_number)
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS note_cache (
            id INTEGER PRIMARY KEY, year TEXT, branch TEXT, subject TEXT, note_number INTEGER,
            telegram_file_id TEXT, UNIQUE(year, branch, subject, note_number)
        )""")
    conn.commit()
    conn.close()
    logger.info(f"Database initialized at: {DB_FILE}")

# ... (The rest of your script remains the same as the last full version) ...
# --- Google Drive API Logic ---
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_SERVICE = None

def get_drive_service():
    """Initializes and returns the Google Drive API service from a secret file path."""
    global DRIVE_SERVICE
    if DRIVE_SERVICE:
        return DRIVE_SERVICE

    secret_file_path = Path("/etc/secrets/service_account.json")

    try:
        from google.oauth2 import service_account
        
        if not secret_file_path.is_file():
            logger.error(f"Secret file not found at {secret_file_path}")
            return None

        creds = service_account.Credentials.from_service_account_file(
            secret_file_path, scopes=SCOPES)
        service = build("drive", "v3", credentials=creds)
        DRIVE_SERVICE = service
        logger.info("Google Drive service initialized successfully from file path.")
        return service
        
    except Exception as e:
        logger.error(f"An error occurred initializing the Drive service from file: {e}")
        return None

# (All other functions like find_item_id_in_parent, start, get_assignment, etc., follow here)

def main():
    if not get_drive_service():
        logger.critical("Could not initialize Google Drive service. Exiting.")
        return
    setup_database()
    
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .build()
    )

    # (Your ConversationHandler setup goes here)

    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)
    
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
