import os
import json
import logging
from random import choice
import ollama
from transformers import pipeline
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

# Configurable paths and constants
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_DEFAULT_TOKEN')
USER_DATA_FILE = 'user_data.json'

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

INITIAL_SYSTEM_MESSAGE = {"role": "system", "content": "You are a mental health support bot named CalmiQ. Respond empathetically, use emojis, and ask follow-up questions."}
FALLBACK_RESPONSE = "Hmm, I didn't quite get that. Could you tell me more? 🤔"

# Load and save user data
def load_user_data():
    try:
        if os.path.exists(USER_DATA_FILE):
            with open(USER_DATA_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading user data: {e}")
    return {}

def save_user_data(user_data):
    try:
        with open(USER_DATA_FILE, 'w') as f:
            json.dump(user_data, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving user data: {e}")

user_data = load_user_data()

# Translation models
translation_pipelines = {
    'en-hi': pipeline("translation_en_to_hi", model="Helsinki-NLP/opus-mt-en-hi"),
    'hi-en': pipeline("translation_hi_to_en", model="Helsinki-NLP/opus-mt-hi-en"),
}
translation_cache = {}

def translate_text(text, source_language, target_language):
    cache_key = f"{source_language}-{target_language}:{text}"
    if cache_key in translation_cache:
        return translation_cache[cache_key]
    try:
        translation_key = f"{source_language}-{target_language}"
        translator = translation_pipelines.get(translation_key)
        if not translator and source_language != 'en' and target_language != 'en':
            intermediate = translate_text(text, source_language, 'en')
            result = translate_text(intermediate, 'en', target_language)
        elif translator:
            result = translator(text)[0]['translation_text']
        else:
            result = text
        translation_cache[cache_key] = result
        return result
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return text

def get_gemma_response(messages):
    try:
        response = ollama.chat(model="gemma2:2b", messages=messages)
        if 'message' in response:
            return response['message']['content'].strip()
    except Exception as e:
        logger.error(f"Error in Gemma response: {e}")
    return FALLBACK_RESPONSE

def generate_dynamic_greeting(name, language='en'):
    prompt = f"Create a warm greeting for someone named {name}. Include emojis and ask how they feel."
    try:
        response = ollama.chat(model="gemma2:2b", messages=[
            {"role": "system", "content": "You generate friendly greetings with emojis."},
            {"role": "user", "content": prompt}
        ])
        if 'message' in response:
            greeting = response['message']['content'].strip()
            return translate_text(greeting, 'en', language) if language != 'en' else greeting
    except Exception as e:
        logger.error(f"Error generating greeting: {e}")
    fallback = f"Hey {name} 😊, great to see you! How have you been? 🌱"
    return translate_text(fallback, 'en', language) if language != 'en' else fallback

async def start(update: Update, context):
    user_id = str(update.message.from_user.id)
    context.user_data.clear()

    if user_id in user_data:
        user_info = user_data[user_id]
        name = user_info.get('name', 'there')
        language = user_info.get('language', 'en')
        greeting = generate_dynamic_greeting(name, language)
    else:
        greeting = "Hello! I'm here to support you. What's your name? 😊"
        user_data[user_id] = {'messages': [INITIAL_SYSTEM_MESSAGE], 'language': 'en'}

    save_user_data(user_data)
    await update.message.reply_text(greeting)

async def ask_name(update: Update, context):
    user_id = str(update.message.from_user.id)
    language = user_data[user_id].get('language', 'en')

    if 'name' not in user_data[user_id]:
        user_data[user_id]['name'] = update.message.text.strip()
        save_user_data(user_data)
        name = user_data[user_id]['name']
        greeting = generate_dynamic_greeting(name, language)
        await update.message.reply_text(greeting)
    else:
        await handle_message(update, context)

async def handle_message(update: Update, context):
    user_id = str(update.message.from_user.id)
    user_input = update.message.text
    language = user_data[user_id].get('language', 'en')

    if user_input.lower() == 'exit':
        exit_message = "It was nice chatting! Take care! 🌼"
        await update.message.reply_text(translate_text(exit_message, 'en', language))
        context.user_data.clear()
        return

    if 'messages' not in user_data[user_id]:
        user_data[user_id]['messages'] = [INITIAL_SYSTEM_MESSAGE]

    # Detect if the user is asking for a poem
    is_poetry_request = any(keyword in user_input.lower() for keyword in ['poem', 'poetry', 'write a poem'])

    user_data[user_id]['messages'].append({"role": "user", "content": user_input})
    bot_response = get_gemma_response(user_data[user_id]['messages'])

    if is_poetry_request:
        bot_response = format_as_poetry(bot_response)

    bot_response_translated = translate_text(bot_response, 'en', language)
    
    user_data[user_id]['messages'].append({"role": "assistant", "content": bot_response_translated})
    save_user_data(user_data)
    await update.message.reply_text(bot_response_translated)

def format_as_poetry(text):
    """Formats a given text as poetry, dividing it into stanzas."""
    # Split response into lines and format as stanzas
    lines = text.split('. ')
    formatted_poetry = '\n'.join(['\n'.join(lines[i:i+2]) for i in range(0, len(lines), 2)])
    return formatted_poetry.strip()

async def reset(update: Update, context):
    user_id = str(update.message.from_user.id)
    if user_id in user_data:
        user_data[user_id].clear()
    save_user_data(user_data)
    await update.message.reply_text("Conversation reset. Let's start fresh! What's your name? 😊")

async def set_language(update: Update, context):
    user_id = str(update.message.from_user.id)
    language = update.message.text.strip().lower()
    user_data[user_id]['language'] = language
    save_user_data(user_data)
    await update.message.reply_text(f"Language set to {language}. 🌍")

def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("setlanguage", set_language))

    text_filter = filters.TEXT & ~filters.COMMAND
    application.add_handler(MessageHandler(text_filter, ask_name))
    application.add_handler(MessageHandler(text_filter, handle_message))

    application.run_polling()

if __name__ == "__main__":
    main()
