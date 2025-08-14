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
# Railway provides the path to the persistent volume in this variable
DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")) / "bot_data"
# This will create /data/bot_data inside the volume, which is a permitted operation
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

# Check for essential environment variables
if not all([TELEGRAM_BOT_TOKEN, GOOGLE_DRIVE_ROOT_FOLDER_ID, SERVICE_ACCOUNT_JSON]):
    raise ValueError("One or more required environment variables are missing.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Management (Caching with Year) ---
DB_FILE = DATA_DIR / "file_cache.db"

# --- Google Drive API Logic ---
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_SERVICE = None

def get_drive_service():
    """Initializes and returns the Google Drive API service from an environment variable."""
    global DRIVE_SERVICE
    if DRIVE_SERVICE:
        return DRIVE_SERVICE
    try:
        from google.oauth2 import service_account
        # Load credentials from the environment variable string
        creds_json = json.loads(SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_json, scopes=SCOPES)
        service = build("drive", "v3", credentials=creds)
        DRIVE_SERVICE = service
        logger.info("Google Drive service initialized successfully from variable.")
        return service
    except Exception as e:
        logger.error(f"An error occurred initializing the Drive service: {e}")
        return None

# (The rest of the script, including setup_database, all your command handlers, and main(), remains exactly the same)
# ...
# ... (all your other functions go here) ...
# ...

def main():
    if not get_drive_service():
        logger.critical("Could not initialize Google Drive service. Exiting.")
        return
    # The rest of the main function is the same...
    setup_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    # ...add your handlers...
    application.run_polling()

if __name__ == "__main__":
    main()
