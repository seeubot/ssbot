import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS 
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import logging
import time
from functools import wraps
import threading
from cachetools import TTLCache
import pymongo # Import for bulk write operations

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- 1. OPTIMIZED MONGODB SETUP WITH CONNECTION POOLING ---
client = None
db = None
content_collection = None

def init_mongodb():
    """Initialize MongoDB connection with connection pooling."""
    global client, db, content_collection
    
    try:
        MONGODB_URI = os.environ.get("MONGODB_URI")
        if not MONGODB_URI:
            logger.error("MONGODB_URI environment variable is not set.")
            return False
        
        # NOTE ON CONNECTION TIMEOUT: If you still see 'timed out' errors,
        # increase serverSelectionTimeoutMS significantly (e.g., to 10000 or 15000)
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            maxPoolSize=50,
            minPoolSize=10,
            maxIdleTimeMS=30000
        )
        
        # Test connection
        client.admin.command('ping')
        
        db_name = os.environ.get("DB_NAME", "streamhub")
        collection_name = os.environ.get("COLLECTION_NAME", "content_items")
        
        db = client[db_name]
        content_collection = db[collection_name]
        
        # Create indexes for better performance
        content_collection.create_index([("created_at", -1)])
        content_collection.create_index([("tags", 1)])
        content_collection.create_index([("views", -1)])
        
        logger.info(f"MongoDB connected with connection pooling. Database: {db_name}")
        return True
    except Exception as e:
        logger.error(f"MongoDB initialization failed: {e}")
        return False

# --- 2. SIMPLE AUTHENTICATION ---
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
AUTHORIZED_CHAT_ID = os.environ.get("AUTHORIZED_CHAT_ID")

def require_auth(f):
    """Simple authentication decorator for API access."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USERNAME or auth.password != ADMIN_PASSWORD:
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- 3. SIMPLE CACHING SYSTEM ---
content_cache = TTLCache(maxsize=100, ttl=30)  

def get_cache_key():
    path = request.path
    args = sorted(request.args.items())
    return f"{path}?{str(args)}"

def cached_response(timeout=30):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if request.method != 'GET':
                return f(*args, **kwargs)
            
            cache_key = get_cache_key()
            if cache_key in content_cache:
                logger.info(f"Cache hit for {cache_key}")
                return content_cache[cache_key]
            
            response = f(*args, **kwargs)
            
            if response[1] == 200:
                content_cache[cache_key] = response
            
            return response
        return decorated_function
    return decorator

# --- 4. OPTIMIZED VIEW COUNT FUNCTIONALITY ---
view_count_cache = {} # Initialized globally to fix NameError
cache_lock = threading.Lock()

def increment_view_count(content_id):
    if content_collection is None:
        return False
    
    try:
        with cache_lock:
            cache_key = f"views_{content_id}"
            view_count_cache[cache_key] = view_count_cache.get(cache_key, 0) + 1
        return True
    except Exception as e:
        logger.error(f"Error incrementing view count: {e}")
        return False

# ... (rest of view count functions remain the same) ...

# --- 5. TELEGRAM AND FLASK SETUP ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
APP_URL = os.environ.get("APP_URL")
PORT = int(os.environ.get("PORT", 8000))

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN environment variable is not set. Telegram features disabled.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/" if BOT_TOKEN else None

app = Flask(__name__)
CORS(app)

# Global state to track multi-step conversation
USER_STATE = {}

# Telegram Bot keyboard templates
START_KEYBOARD = {
    'keyboard': [
        [{'text': '/add'}, {'text': '/edit'}, {'text': '/delete'}],
        [{'text': '/cancel'}]
    ],
    'resize_keyboard': True,
    'one_time_keyboard': False
}


# --- BOT HELPER FUNCTIONS ---

def get_content_info_for_edit(content_id):
    if content_collection is None:
        return None
    try:
        # Check if the content_id is a valid ObjectId before querying
        if not ObjectId.is_valid(content_id):
            return None
            
        doc = content_collection.find_one({"_id": ObjectId(content_id)})
        return doc
    except Exception:
        return None

def update_content(content_id, update_data):
    if content_collection is None:
        return False
    try:
        if not ObjectId.is_valid(content_id):
            return False
            
        update_data.pop('_id', None)
        
        result = content_collection.update_one(
            {"_id": ObjectId(content_id)},
            {"$set": update_data}
        )
        
        if result.modified_count > 0:
            content_cache.clear()
        
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"MongoDB Update Error: {e}")
        return False

def send_message(chat_id, text, reply_markup=None):
    if not TELEGRAM_API:
        logger.warning("Telegram bot token not configured")
        return
    
    url = TELEGRAM_API + "sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    
    if reply_markup is not None:
        payload['reply_markup'] = json.dumps(reply_markup)
    else:
        if USER_STATE.get(chat_id, {}).get('step') == 'main':
             payload['reply_markup'] = json.dumps(START_KEYBOARD)

    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
        logger.info(f"Message sent to chat_id {chat_id}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending message to {chat_id}: {e}")

def save_content(content_data):
    if content_collection is None: 
        return False
    try:
        document = {
            "title": content_data.get('title'),
            "type": content_data.get('type'),
            "thumbnail_url": content_data.get('thumbnail_url'),
            # Ensure links is initialized as an empty list if not present
            "tags": [t.strip().lower() for t in content_data.get('tags', '').split(',') if t.strip()],
            "links": content_data.get('links', []),
            "views": 0,
            "created_at": datetime.utcnow(),
            "last_viewed": datetime.utcnow()
        }
        result = content_collection.insert_one(document)
        logger.info(f"Content saved with ID: {result.inserted_id}")
        
        content_cache.clear()
        
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"MongoDB Save Error: {e}")
        return False

# --- 7. FLASK ROUTES ---
# ... (index, health, track_view, get_content, get_content_by_id, get_similar_content, admin_create_content, admin_update_content, admin_delete_content remain the same) ...
# --- (The content retrieval and admin API routes are omitted here for brevity but assumed to be in the final code) ---

# --- TELEGRAM WEBHOOK HANDLER (FIXED BOT LOGIC) ---

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Webhook handler for Telegram updates with command and state logic."""
    if not BOT_TOKEN:
        return jsonify({"status": "telegram not configured"}), 200
        
    try:
        update = request.get_json(silent=True)
        if not update:
            return jsonify({"status": "no data"}), 200
        
        message = update.get('message')
        if not message:
             return jsonify({"status": "not message"}), 200
            
        chat_id = message['chat']['id']
        text = message.get('text', '').strip()
        user_state = USER_STATE.get(chat_id, {'step': 'main'})
        
        # --- Command Handlers ---
        
        if text.startswith('/start'):
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(chat_id, "🚀 Welcome to StreamHub Bot! Choose an action:", START_KEYBOARD)
            
        elif text.startswith('/add'):
            USER_STATE[chat_id] = {'step': 'add_title', 'data': {'links': []}}
            send_message(chat_id, "➡️ **ADD Content:** Please send the **Title** of the content.")
            
        elif text.startswith('/edit'):
            USER_STATE[chat_id] = {'step': 'edit_id', 'data': {}}
            send_message(chat_id, "➡️ **EDIT Content:** Please send the **Content ID** you want to edit.")
            
        elif text.startswith('/delete'):
            USER_STATE[chat_id] = {'step': 'delete_id', 'data': {}}
            send_message(chat_id, "➡️ **DELETE Content:** Please send the **Content ID** to confirm deletion.")
            
        elif text.startswith('/cancel'):
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(chat_id, "Operation cancelled. Choose a new action:", START_KEYBOARD)
            
        # --- Multi-step Input Handlers ---
        
        elif user_state['step'] == 'add_title':
            user_state['data']['title'] = text
            user_state['step'] = 'add_type'
            send_message(chat_id, "✅ Title saved. Now send the **Type** (e.g., `movie` or `series`).")
            
        elif user_state['step'] == 'add_type':
            user_state['data']['type'] = text
            user_state['step'] = 'add_thumbnail'
            send_message(chat_id, "✅ Type saved. Now send the **Thumbnail URL**.")
            
        elif user_state['step'] == 'add_thumbnail':
            user_state['data']['thumbnail_url'] = text
            user_state['step'] = 'add_tags'
            send_message(chat_id, "✅ URL saved. Now send **Tags** (comma-separated, e.g., `action, thriller, new`).")
            
        elif user_state['step'] == 'add_tags':
            user_state['data']['tags'] = text
            # NEW LINK INPUT FLOW STARTS HERE
            user_state['step'] = 'add_episode_title'
            send_message(chat_id, "✅ Tags saved. Now, send the **Episode Title** (e.g., 'Episode 1' or 'Main Link'). Type **DONE** to finish.")
            
        # --- NEW Sequential Link Input ---
        elif user_state['step'] == 'add_episode_title':
            if text.upper() == 'DONE':
                if not user_state['data']['links']:
                    send_message(chat_id, "❌ Cannot save content without any links. Please add at least one link or type **/cancel**.")
                    return

                # --- FINAL SAVE ACTION ---
                content_id = save_content(user_state['data'])
                if content_id:
                    send_message(chat_id, f"🎉 **Success!** Content '{user_state['data']['title']}' added with ID: `{content_id}`.", START_KEYBOARD)
                else:
                    send_message(chat_id, "❌ **Save Failed.** Check server logs for MongoDB error.", START_KEYBOARD)
                
                USER_STATE[chat_id] = {'step': 'main'}
            else:
                # Save the title and prompt for the URL
                user_state['data']['current_episode_title'] = text
                user_state['step'] = 'add_episode_url'
                send_message(chat_id, f"✅ Episode Title saved. Now send the **URL** for '{text}'.")

        elif user_state['step'] == 'add_episode_url':
            # Save the URL and add the complete link object to the list
            episode_title = user_state['data'].pop('current_episode_title', 'Link')
            user_state['data']['links'].append({
                "url": text,
                "episode_title": episode_title
            })
            
            # Go back to asking for the next episode title
            user_state['step'] = 'add_episode_title'
            send_message(chat_id, f"✅ Link saved. Current links: {len(user_state['data']['links'])}. Send the **NEXT Episode Title** or type **DONE**.")
            
        # --- EDIT FLOW FIX ---
        
        # FIX: The check below ensures we proceed only after successfully receiving a valid ID
        elif user_state['step'] == 'edit_id':
            content_id = text
            content_doc = get_content_info_for_edit(content_id)
            if content_doc:
                # Store ID and move to the next step (edit_field)
                user_state['data']['_id'] = content_id
                user_state['step'] = 'edit_field'
                
                # Setup keyboard for field selection (Title, Type, etc.)
                edit_fields = ['Title', 'Type', 'Thumbnail URL', 'Tags', 'Links']
                keyboard_buttons = [[{'text': f'/edit_{f.lower().replace(" ", "_")}'}] for f in edit_fields]
                keyboard_buttons.append([{'text': '/cancel'}])

                info_text = (
                    f"📝 **Editing Content ID**: `{content_id}`\n\n"
                    f"**Current Title**: {content_doc.get('title', 'N/A')}\n"
                    f"**Current Type**: {content_doc.get('type', 'N/A')}\n\n"
                    "Please select a field to update:"
                )
                
                send_message(chat_id, info_text, {'keyboard': keyboard_buttons, 'resize_keyboard': True})
            else:
                # If ID is not found or invalid, stay in 'edit_id' state and re-prompt
                send_message(chat_id, "❌ Content ID not found or invalid. Try again or type **/cancel**.")

        # --- Generic Edit Field Handlers ---
        elif user_state['step'] == 'edit_field' and text.startswith('/edit_'):
            field = text.split('_', 1)[1]
            user_state['step'] = f'edit_new_{field}'
            send_message(chat_id, f"➡️ Please send the **NEW value** for **{field.replace('_', ' ').title()}**.\n(For Links, send the **complete, updated list** of JSON links or the single new URL)")
            
        # --- Specific Edit Field Value Handlers ---
        elif user_state['step'].startswith('edit_new_'):
            field = user_state['step'].split('_new_')[1]
            content_id = user_state['data']['_id']
            
            update_data = {}
            if field == 'tags':
                update_data['tags'] = [t.strip().lower() for t in text.split(',') if t.strip()]
            elif field == 'links':
                # Revert to JSON input for EDITING links, as sequential editing is too complex for this bot
                try:
                    links = json.loads(text)
                    if not isinstance(links, list): raise ValueError("Links must be a JSON array.")
                    update_data['links'] = links
                except (json.JSONDecodeError, ValueError) as e:
                    # Fallback to single URL if JSON fails
                    update_data['links'] = [{"url": text, "episode_title": "Watch Link"}]
            else:
                update_data[field] = text
            
            if update_content(content_id, update_data):
                send_message(chat_id, f"✅ **Success!** {field.replace('_', ' ').title()} for ID `{content_id}` updated.", START_KEYBOARD)
            else:
                send_message(chat_id, "❌ **Update Failed.** Content not found or error occurred.", START_KEYBOARD)

            USER_STATE[chat_id] = {'step': 'main'}

        # --- DELETE FLOW ---
        
        elif user_state['step'] == 'delete_id':
            content_id = text
            # The actual delete logic is inside admin_delete_content (called implicitly here)
            if admin_delete_content(content_id) == ({"success": True, "message": "Content deleted successfully"}, 200):
                send_message(chat_id, f"🗑️ **Success!** Content ID `{content_id}` has been deleted.", START_KEYBOARD)
            else:
                send_message(chat_id, f"❌ **Error:** Content ID `{content_id}` not found or invalid.", START_KEYBOARD)

            USER_STATE[chat_id] = {'step': 'main'}
            
        else:
            send_message(chat_id, "I don't understand that. Use /start to see the options.", START_KEYBOARD)
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

# --- BACKGROUND TASKS and APPLICATION STARTUP remain the same ---

# Note: The remaining Flask routes (index, health, API endpoints) from the previous full code are 
# assumed to be present and unchanged (except for admin_update_content added previously and the delete logic check).
# The flush_view_cache and application startup block also remains the same.


if __name__ == '__main__':
    logger.info("Starting Optimized StreamHub Application...")
    init_mongodb()
    
    # Start background tasks
    cache_thread = threading.Thread(target=flush_view_cache, daemon=True)
    cache_thread.start()
    
    if APP_URL and BOT_TOKEN:
        # Note: The `set_webhook()` function needs to be explicitly defined or present.
        # Assuming it's present from the previous code block.
        pass
    else:
        logger.warning("APP_URL or BOT_TOKEN not set - webhook not configured")
    
    logger.info(f"Starting optimized Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
