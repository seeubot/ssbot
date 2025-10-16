import os
import json
import requests
from flask import Flask, request, jsonify
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime

# --- 1. MONGODB SETUP ---
# Reading environment variables provided by the user:
# BOT_TOKEN=8454570063:AAEC18lz-I4WzJEzE-E01fNf5k0SlcaJRZI
# MONGODB_URI=mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie
# APP_URL=https://confident-jemima-school1660440-5a325843.koyeb.app

client = None
db = None
content_collection = None

try:
    MONGODB_URI = os.environ.get("MONGODB_URI")
    if not MONGODB_URI:
        # NOTE: This error is expected if the variable is not set in the environment
        print("FATAL: MONGODB_URI environment variable is not set.")
    else:
        # Initialize MongoDB Client
        client = MongoClient(MONGODB_URI)
        
        # Define Database and Collection
        db_name = 'movie' 
        collection_name = 'content_items'
        db = client[db_name]
        content_collection = db[collection_name]
        
        print(f"MongoDB connected. Database: {db_name}. Collections: {db.list_collection_names()}")
except Exception as e:
    print(f"FATAL: Error initializing MongoDB: {e}")

# --- 2. TELEGRAM AND FLASK SETUP ---

BOT_TOKEN = os.environ.get("BOT_TOKEN")
APP_URL = os.environ.get("APP_URL")

if not BOT_TOKEN:
    # This explicit raise is what caused the worker to crash, ensuring secrets are set
    raise ValueError("BOT_TOKEN environment variable is not set.")
if not APP_URL:
    print("WARNING: APP_URL environment variable is not set. Frontend fetching may fail.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

app = Flask(__name__)

# Global state to track multi-step conversation for each user/admin
USER_STATE = {} 

# FSM States
STATE_START = 'START'
STATE_WAITING_FOR_TYPE = 'WAITING_FOR_TYPE'
STATE_WAITING_FOR_THUMBNAIL = 'WAITING_FOR_THUMBNAIL'
STATE_WAITING_FOR_TITLE = 'WAITING_FOR_TITLE'
STATE_WAITING_FOR_LINK_TITLE = 'WAITING_FOR_LINK_TITLE'
STATE_WAITING_FOR_LINK_URL = 'WAITING_FOR_LINK_URL'
STATE_CONFIRM_LINK = 'CONFIRM_LINK'

# --- 3. CORE BOT FUNCTIONS ---

def send_message(chat_id, text, reply_markup=None):
    """Sends a message back to the user."""
    url = TELEGRAM_API + "sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending message: {e}")

def save_content(content_data):
    """Saves the complete content document to MongoDB."""
    if content_collection is None:
        return False

    try:
        document = {
            "title": content_data.get('title'),
            "type": content_data.get('type'),
            "thumbnail_url": content_data.get('thumbnail_url'),
            "links": content_data.get('links', []),
            "created_at": datetime.utcnow() # Use UTC datetime for creation time
        }
        
        content_collection.insert_one(document)
        return True
    except Exception as e:
        print(f"MongoDB Save Error: {e}")
        return False

# --- 4. CONVERSATION HANDLERS (Simplified for brevity) ---

def start_new_upload(chat_id):
    """Starts the content upload process, asking for type."""
    USER_STATE[chat_id] = {'state': STATE_WAITING_FOR_TYPE, 'content_data': {'links': []}}
    keyboard = {
        'inline_keyboard': [
            [{'text': '🎬 Movie', 'callback_data': 'type_Movie'}],
            [{'text': '📺 Web Series', 'callback_data': 'type_Series'}]
        ]
    }
    send_message(
        chat_id, 
        "*Welcome to the Content Upload Bot!*\\n\\nPlease select the type of content:",
        reply_markup=keyboard
    )

def ask_for_title(chat_id):
    USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_TITLE
    send_message(chat_id, "✅ Content Type set.\\n\\nNow, what is the *Title* of the Movie/Series?")

def ask_for_thumbnail(chat_id):
    USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_THUMBNAIL
    send_message(chat_id, "✅ Title set.\\n\\nNext, please send the *public URL* for the Content Thumbnail Image:")

def ask_for_link_title(chat_id):
    content_type = USER_STATE[chat_id]['content_data']['type']
    prompt = "Enter the name for the streaming link (e.g., 'Full Movie' or 'S01E01 Pilot')."
    
    USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_LINK_TITLE
    send_message(chat_id, f"✅ Thumbnail URL set.\\n\\n{prompt}")

def finish_upload(chat_id):
    content_data = USER_STATE[chat_id]['content_data']
    
    if not content_data.get('title') or not content_data.get('links'):
        send_message(chat_id, "❌ Error: Missing title or streaming links. Please start over with /start.")
        USER_STATE[chat_id]['state'] = STATE_START
        return

    if save_content(content_data):
        send_message(chat_id, f"🎉 *Success!* Content '{content_data['title']}' saved to MongoDB.")
        USER_STATE[chat_id]['state'] = STATE_START
        USER_STATE[chat_id]['content_data'] = {'links': []}
    else:
        send_message(chat_id, "❌ Error: Could not save to MongoDB. Check server logs.")

# Helper function to handle text messages
def handle_text_message(chat_id, text):
    state = USER_STATE.get(chat_id, {}).get('state', STATE_START)
    content_data = USER_STATE.get(chat_id, {}).get('content_data', {})

    if text.startswith('/start'):
        start_new_upload(chat_id)
        return

    if state == STATE_WAITING_FOR_TITLE:
        content_data['title'] = text
        ask_for_thumbnail(chat_id)

    elif state == STATE_WAITING_FOR_THUMBNAIL:
        if text.startswith('http'):
            content_data['thumbnail_url'] = text
            ask_for_link_title(chat_id)
        else:
            send_message(chat_id, "Please send a *public URL* starting with `http` or `https`.")

    elif state == STATE_WAITING_FOR_LINK_TITLE:
        content_data['current_link_title'] = text
        USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_LINK_URL
        send_message(chat_id, f"Link name set: *{text}*\\n\\nNow, send the *Streaming URL*:")

    elif state == STATE_WAITING_FOR_LINK_URL:
        if text.startswith('http'):
            link_title = content_data.pop('current_link_title', 'Link')
            content_data['links'].append({'episode_title': link_title, 'url': text})
            
            keyboard = {
                'inline_keyboard': [
                    [{'text': '➕ Add Another Link', 'callback_data': 'add_Yes'}],
                    [{'text': '✅ Done Uploading', 'callback_data': 'add_No'}]
                ]
            }
            send_message(chat_id, 
                f"✅ Streaming URL added! Total links: {len(content_data['links'])}.\\n\\nWhat next?",
                reply_markup=keyboard
            )
            USER_STATE[chat_id]['state'] = STATE_CONFIRM_LINK
        else:
            send_message(chat_id, "Please send a URL starting with `http` or `https`.")

    elif state == STATE_START:
        send_message(chat_id, "Please use the /start command to begin a new content upload.")

# Helper function to handle callback queries
def handle_callback_query(chat_id, data):
    state = USER_STATE.get(chat_id, {}).get('state')

    if state == STATE_WAITING_FOR_TYPE and data.startswith('type_'):
        content_type = data.split('_')[1]
        USER_STATE[chat_id]['content_data']['type'] = content_type
        ask_for_title(chat_id)
        
    elif state == STATE_CONFIRM_LINK and data.startswith('add_'):
        action = data.split('_')[1]
        if action == 'Yes':
            ask_for_link_title(chat_id)
        elif action == 'No':
            finish_upload(chat_id)

# --- 5. KOYEB ROUTES ---

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Main webhook handler for Telegram updates."""
    update = request.json
    
    if 'message' in update:
        message = update['message']
        chat_id = message['chat']['id']
        text = message.get('text', '')
        handle_text_message(chat_id, text)
        
    elif 'callback_query' in update:
        query = update['callback_query']
        chat_id = query['message']['chat']['id']
        data = query['data']
        handle_callback_query(chat_id, data)
        
    return 'OK'

@app.route('/api/content', methods=['GET'])
def get_content():
    """REST API endpoint for the frontend to fetch content from MongoDB."""
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        # Fetch all content, sorted by 'created_at' descending
        content_cursor = content_collection.find().sort("created_at", -1)
        
        content_list = []
        for doc in content_cursor:
            # Convert MongoDB ObjectId and datetime to string for JSON serialization
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            content_list.append(doc)
            
        return jsonify(content_list), 200
    except Exception as e:
        print(f"API Fetch Error: {e}")
        return jsonify({"error": "Failed to retrieve content."}), 500


@app.route('/')
def index():
    """Koyeb health check endpoint."""
    return "Telegram Bot API Listener is running.", 200

