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
# UPDATED: Path for Railway persistent storage
DATA_DIR = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")) / "bot_data"
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
            id INTEGER PRIMARY KEY, year TEXT, group_name TEXT, subject TEXT, assignment_number INTEGER,
            telegram_file_id TEXT, UNIQUE(year, group_name, subject, assignment_number)
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS note_cache (
            id INTEGER PRIMARY KEY, year TEXT, group_name TEXT, subject TEXT, note_number INTEGER,
            telegram_file_id TEXT, UNIQUE(year, group_name, subject, note_number)
        )""")
    conn.commit()
    conn.close()
    logger.info(f"Database initialized at: {DB_FILE}")

def get_cached_assignment_id(year, group_name, subject, assignment_number):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT telegram_file_id FROM assignment_cache WHERE year = ? AND group_name = ? AND subject = ? AND assignment_number = ?",
        (year, group_name.upper(), subject.upper(), assignment_number)
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def cache_assignment_id(year, group_name, subject, assignment_number, file_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO assignment_cache (year, group_name, subject, assignment_number, telegram_file_id) VALUES (?, ?, ?, ?, ?)",
        (year, group_name.upper(), subject.upper(), assignment_number, file_id)
    )
    conn.commit()
    conn.close()

def get_cached_note_id(year, group_name, subject, note_number):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT telegram_file_id FROM note_cache WHERE year = ? AND group_name = ? AND subject = ? AND note_number = ?",
        (year, group_name.upper(), subject.upper(), note_number)
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def cache_note_id(year, group_name, subject, note_number, file_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO note_cache (year, group_name, subject, note_number, telegram_file_id) VALUES (?, ?, ?, ?, ?)",
        (year, group_name.upper(), subject.upper(), note_number, file_id)
    )
    conn.commit()
    conn.close()

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

# --- Google Drive Helper Functions ---
async def find_item_id_in_parent(name, parent_id, is_folder=True):
    service = get_drive_service()
    if not service: return None
    mime_type_query = "mimeType = 'application/vnd.google-apps.folder'" if is_folder else "mimeType != 'application/vnd.google-apps.folder'"
    try:
        query = f"name = '{name}' and '{parent_id}' in parents and trashed = false and {mime_type_query}"
        response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = response.get('files', [])
        return files[0].get('id') if files else None
    except HttpError as error:
        logger.error(f"API Error finding '{name}': {error}")
        return None

async def list_folders_in_parent(parent_id):
    service = get_drive_service()
    if not service: return []
    try:
        query = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        response = service.files().list(q=query, spaces='drive', fields='files(name)').execute()
        return [item['name'] for item in response.get('files', [])]
    except HttpError as error:
        logger.error(f"API Error listing folders: {error}")
        return []

async def download_file_from_drive(file_id):
    service = get_drive_service()
    try:
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)
        return fh
    except HttpError as error:
        logger.error(f"API Error downloading file: {error}")
        return None

async def resolve_path_to_id(path_parts):
    current_id = GOOGLE_DRIVE_ROOT_FOLDER_ID
    for part in path_parts:
        next_id = await find_item_id_in_parent(part, current_id, is_folder=True)
        if not next_id:
            logger.warning(f"Could not find folder part: '{part}' in path '{'/'.join(path_parts)}'")
            return None
        current_id = next_id
    return current_id

# --- Command Handlers ---
async def check_user_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if 'year' not in context.user_data:
        await update.message.reply_text("Welcome\\! Please start by using the /start command to set your year and name\\.")
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reply_keyboard = [["1st Year", "2nd Year"], ["3rd Year", "4th Year"]]
    await update.message.reply_text(
        "👋 Welcome\\! Let's get you set up\\.\n\n"
        "First, please select your academic year\\.",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode='MarkdownV2'
    )
    return SELECT_YEAR

async def select_year(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    year_display = update.message.text
    year_folder_name = year_display.replace(" ", "_")
    context.user_data['year'] = year_folder_name
    context.user_data['year_display'] = year_display
    await update.message.reply_text(
        f"Great\\! You've selected *{escape_markdown(year_display)}*\\.\n\n"
        "Now, what's your name?",
        parse_mode='MarkdownV2',
        reply_markup=ReplyKeyboardRemove(),
    )
    return GET_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text
    context.user_data['name'] = name
    await update.message.reply_text(
        f"Hi {escape_markdown(name)}\\! You're all set up\\. You can now use the bot commands\\.\n\n"
        "Type /help to see what I can do\\.",
        parse_mode='MarkdownV2'
    )
    return MAIN_MENU

# UPDATED: Help command is now dynamic
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_setup(update, context): return
    
    name = context.user_data.get('name', 'User')
    year_display = context.user_data.get('year_display', 'N/A')
    year = context.user_data.get('year')

    if year == '1st_Year':
        group_cmd = "divisions"
        group_name = "DIVISION"
        group_name_plural = "Divisions"
    else:
        group_cmd = "branches"
        group_name = "BRANCH"
        group_name_plural = "Branches"
    
    help_text = (
        f"👋 Hello {escape_markdown(name)}\\! Your current year is set to *{escape_markdown(year_display)}*\\.\n\n"
        "*Available Commands:*\n"
        f"• `/{group_cmd}` \\- Lists available {group_name_plural}\\.\n"
        f"• `/subjects <{group_name}>` \\- Lists subjects for a {group_name.lower()}\\.\n"
        f"• `/assignments <{group_name}> <SUBJECT>` \\- Lists available assignment numbers\\.\n"
        f"• `/notes <{group_name}> <SUBJECT>` \\- Lists available note/unit numbers\\.\n"
        f"• `/get <{group_name}> <SUBJECT> <NUMBER>` \\- Fetches an assignment file\\.\n"
        f"• `/getnote <{group_name}> <SUBJECT> <NUMBER>` \\- Fetches a note/unit file\\.\n"
        "• `/suggestion` \\- Send a suggestion or feedback\\.\n"
        "• `/start` \\- To reset your year and name\\.\n"
        "• `/cancel` \\- To end any active command\\."
    )
    await update.message.reply_text(help_text, parse_mode='MarkdownV2')

# UPDATED: This function now lists branches OR divisions
async def list_branches_or_divisions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_setup(update, context): return
    year = context.user_data['year']
    year_display = context.user_data['year_display']
    
    year_folder_id = await find_item_id_in_parent(year, GOOGLE_DRIVE_ROOT_FOLDER_ID)
    if not year_folder_id:
        await update.message.reply_text(f"🤷 No folder found for your year: `{escape_markdown(year_display)}`\\.", parse_mode='MarkdownV2')
        return

    items = await list_folders_in_parent(year_folder_id)
    
    if year == '1st_Year':
        group_name_plural = "Divisions"
    else:
        group_name_plural = "Branches"

    if not items:
        await update.message.reply_text(f"🤷 No {group_name_plural.lower()} found for `{escape_markdown(year_display)}`\\.", parse_mode='MarkdownV2')
        return

    item_list = "\n".join(f"• `{escape_markdown(item)}`" for item in sorted(items))
    message = f"📚 *Available {group_name_plural} for {escape_markdown(year_display)}:*\n\n{item_list}"
    await update.message.reply_text(message, parse_mode='MarkdownV2')

# UPDATED: Logic is now dynamic
async def list_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_setup(update, context): return
    
    year = context.user_data['year']
    year_display = context.user_data['year_display']

    if year == '1st_Year':
        usage = "`/subjects <DIVISION>`"
        group_name_singular = "division"
    else:
        usage = "`/subjects <BRANCH>`"
        group_name_singular = "branch"

    if not context.args:
        await update.message.reply_text(f"⚠️ Usage: {usage}")
        return
        
    group_name = context.args[0].upper()
    
    group_folder_id = await resolve_path_to_id([year, group_name])
    if not group_folder_id:
        await update.message.reply_text(f"❌ {group_name_singular.capitalize()} folder for `{escape_markdown(group_name)}` not found\\.", parse_mode='MarkdownV2')
        return

    subjects = await list_folders_in_parent(group_folder_id)
    if not subjects:
        await update.message.reply_text(f"🤷 No subjects found for {group_name_singular} `{escape_markdown(group_name)}`\\.", parse_mode='MarkdownV2')
        return

    subject_list = "\n".join(f"• `{escape_markdown(item)}`" for item in sorted(subjects))
    message = f"📖 *Subjects for {escape_markdown(year_display)}/{escape_markdown(group_name)}:*\n\n{subject_list}"
    await update.message.reply_text(message, parse_mode='MarkdownV2')

# UPDATED: Logic is now dynamic
async def list_assignments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_setup(update, context): return
    
    year = context.user_data['year']

    if year == '1st_Year':
        usage = "`/assignments <DIVISION> <SUBJECT>`"
    else:
        usage = "`/assignments <BRANCH> <SUBJECT>`"

    if len(context.args) != 2:
        await update.message.reply_text(f"⚠️ Usage: {usage}")
        return
        
    group_name, subject = context.args[0].upper(), context.args[1].upper()
    
    assignments_folder_id = await resolve_path_to_id([year, group_name, subject, "Assignments"])
    if not assignments_folder_id:
        await update.message.reply_text(f"❌ No `Assignments` folder found for `{escape_markdown(group_name)}/{escape_markdown(subject)}`\\.", parse_mode='MarkdownV2')
        return

    service = get_drive_service()
    query = f"'{assignments_folder_id}' in parents and trashed = false"
    response = service.files().list(q=query, spaces='drive', fields='files(name)').execute()
    files = response.get('files', [])
    
    assignment_numbers = {int(m.group(1)) for item in files if (m := re.search(r'assignment_(\d+)', item['name'], re.IGNORECASE))}
    if not assignment_numbers:
        await update.message.reply_text(f"🤷 No assignments found for `{escape_markdown(group_name)}/{escape_markdown(subject)}`\\.", parse_mode='MarkdownV2')
        return

    number_list = "\n".join(f"• `Assignment {num}`" for num in sorted(list(assignment_numbers)))
    message = f"📄 *Assignments for {escape_markdown(group_name)}/{escape_markdown(subject)}:*\n\n{number_list}"
    await update.message.reply_text(message, parse_mode='MarkdownV2')

# UPDATED: Logic is now dynamic
async def get_assignment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_setup(update, context): return
    
    year = context.user_data['year']

    if year == '1st_Year':
        usage = "`/get <DIVISION> <SUBJECT> <NUMBER>`"
    else:
        usage = "`/get <BRANCH> <SUBJECT> <NUMBER>`"

    if len(context.args) != 3:
        await update.message.reply_text(f"⚠️ Usage: {usage}")
        return
        
    group_name, subject, number_str = context.args[0].upper(), context.args[1].upper(), context.args[2]

    try:
        assignment_number = int(number_str)
    except ValueError:
        await update.message.reply_text("⚠️ Assignment number must be an integer\\.")
        return

    placeholder_message = await update.message.reply_text("⏳ Getting your file, please wait\\.\\.\\.", parse_mode='MarkdownV2')

    cached_file_id = get_cached_assignment_id(year, group_name, subject, assignment_number)
    if cached_file_id:
        try:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=cached_file_id)
            await placeholder_message.delete()
            return
        except TelegramError as e:
            logger.warning(f"Failed to send cached file {cached_file_id}, re-downloading. Error: {e}")

    assignments_folder_id = await resolve_path_to_id([year, group_name, subject, "Assignments"])
    if not assignments_folder_id:
        await placeholder_message.edit_text("❌ Assignment folder not found\\.", parse_mode='MarkdownV2')
        return

    service = get_drive_service()
    query = f"'{assignments_folder_id}' in parents and trashed = false and name ~* 'assignment_?{assignment_number}[^0-9]'"
    response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = response.get('files', [])
    if not files:
        await placeholder_message.edit_text("❌ Assignment not found\\.", parse_mode='MarkdownV2')
        return

    file_to_send = files[0]
    file_id, file_name = file_to_send['id'], file_to_send['name']
    
    file_content = await download_file_from_drive(file_id)
    if file_content:
        sent_message = await context.bot.send_document(chat_id=update.effective_chat.id, document=file_content, filename=file_name)
        cache_assignment_id(year, group_name, subject, assignment_number, sent_message.document.file_id)
        await placeholder_message.delete()
    else:
        await placeholder_message.edit_text("⚠️ Error downloading the file from Google Drive\\.", parse_mode='MarkdownV2')

# UPDATED: Logic is now dynamic
async def list_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_setup(update, context): return

    year = context.user_data['year']

    if year == '1st_Year':
        usage = "`/notes <DIVISION> <SUBJECT>`"
    else:
        usage = "`/notes <BRANCH> <SUBJECT>`"

    if len(context.args) != 2:
        await update.message.reply_text(f"⚠️ Usage: {usage}")
        return
        
    group_name, subject = context.args[0].upper(), context.args[1].upper()
    
    notes_folder_id = await resolve_path_to_id([year, group_name, subject, "Notes"])
    if not notes_folder_id:
        await update.message.reply_text(f"❌ No `Notes` folder found for `{escape_markdown(group_name)}/{escape_markdown(subject)}`\\.", parse_mode='MarkdownV2')
        return

    service = get_drive_service()
    query = f"'{notes_folder_id}' in parents and trashed = false"
    response = service.files().list(q=query, spaces='drive', fields='files(name)').execute()
    files = response.get('files', [])
    
    note_numbers = {int(m.group(1)) for item in files if (m := re.search(r'(?:unit|note)_(\d+)', item['name'], re.IGNORECASE))}
    
    if not note_numbers:
        await update.message.reply_text(f"🤷 No notes found for `{escape_markdown(group_name)}/{escape_markdown(subject)}`\\.", parse_mode='MarkdownV2')
        return
        
    note_list = "\n".join(f"• `Unit {num}`" for num in sorted(list(note_numbers)))
    message = f"📝 *Available Notes/Units for {escape_markdown(group_name)}/{escape_markdown(subject)}:*\n\n{note_list}"
    await update.message.reply_text(message, parse_mode='MarkdownV2')

# UPDATED: Logic is now dynamic
async def get_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_setup(update, context): return
    
    year = context.user_data['year']

    if year == '1st_Year':
        usage = "`/getnote <DIVISION> <SUBJECT> <NUMBER>`"
    else:
        usage = "`/getnote <BRANCH> <SUBJECT> <NUMBER>`"
    
    if len(context.args) != 3:
        await update.message.reply_text(f"⚠️ Usage: {usage}")
        return
    
    group_name, subject, number_str = context.args[0].upper(), context.args[1].upper(), context.args[2]

    try:
        note_number = int(number_str)
    except ValueError:
        await update.message.reply_text("⚠️ Note number must be an integer\\.")
        return

    placeholder_message = await update.message.reply_text("⏳ Getting your file, please wait\\.\\.\\.", parse_mode='MarkdownV2')

    cached_file_id = get_cached_note_id(year, group_name, subject, note_number)
    if cached_file_id:
        try:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=cached_file_id)
            await placeholder_message.delete()
            return
        except TelegramError as e:
            logger.warning(f"Failed to send cached file {cached_file_id}, re-downloading. Error: {e}")

    notes_folder_id = await resolve_path_to_id([year, group_name, subject, "Notes"])
    if not notes_folder_id:
        await placeholder_message.edit_text("❌ Notes folder not found\\.", parse_mode='MarkdownV2')
        return
    
    service = get_drive_service()
    query = f"'{notes_folder_id}' in parents and trashed = false and (name ~* '(unit|note)_?{note_number}[^0-9]')"
    response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = response.get('files', [])
    
    if not files:
        await placeholder_message.edit_text("❌ Note not found\\.", parse_mode='MarkdownV2')
        return

    file_to_send = files[0]
    file_id, file_name = file_to_send['id'], file_to_send['name']
    
    file_content = await download_file_from_drive(file_id)
    if file_content:
        sent_message = await context.bot.send_document(chat_id=update.effective_chat.id, document=file_content, filename=file_name)
        cache_note_id(year, group_name, subject, note_number, sent_message.document.file_id)
        await placeholder_message.delete()
    else:
        await placeholder_message.edit_text("⚠️ Error downloading the file from Google Drive\\.", parse_mode='MarkdownV2')

async def suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Got a suggestion or want to report an issue? Please fill out this form:\n\n"
        "https://forms.gle/FecbVJn69qDcsKcP8"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Operation cancelled\\.", reply_markup=ReplyKeyboardRemove(), parse_mode='MarkdownV2'
    )
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(context.error, TimedOut):
        if update and hasattr(update, 'message'):
            await update.message.reply_text("We're experiencing a delay. Please try your request again in a moment.")
        return
    if isinstance(context.error, TelegramError):
        logger.warning(f"Telegram API Error: {context.error.message}")
        return

# --- Main Bot Execution ---
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

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_YEAR: [MessageHandler(filters.Regex(r"^(1st|2nd|3rd|4th) Year$"), select_year)],
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            MAIN_MENU: [
                CommandHandler("help", help_command),
                # UPDATED: New command handlers for dynamic structure
                CommandHandler("branches", list_branches_or_divisions),
                CommandHandler("divisions", list_branches_or_divisions),
                CommandHandler("subjects", list_subjects),
                CommandHandler("assignments", list_assignments),
                CommandHandler("get", get_assignment),
                CommandHandler("notes", list_notes),
                CommandHandler("getnote", get_note),
                CommandHandler("suggestion", suggestion),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        per_user=True,
        per_chat=True
    )

    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)
    
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
