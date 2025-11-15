import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS 
from pymongo import MongoClient, ReturnDocument 
from bson import ObjectId
from datetime import datetime
import logging
import time
from functools import wraps
import threading
from cachetools import TTLCache
import pymongo.operations 
import urllib.parse

# --- CONSTANTS & CONFIGURATION ---
ADMIN_TELEGRAM_ID = 1352497419
GROUP_TELEGRAM_ID = -1002541647242
CONTENT_FORWARD_CHANNEL_ID = -1002776780769
PRODUCT_NAME = "Adult-Hub"
ACCESS_URL = "teluguxx.vercel.app"

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- MONGODB SETUP ---
client = None
db = None
content_collection = None
counter_collection = None

def init_mongodb():
    global client, db, content_collection, counter_collection
    
    try:
        MONGODB_URI = os.environ.get("MONGODB_URI")
        if not MONGODB_URI:
            logger.error("MONGODB_URI environment variable is not set.")
            return False
        
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        client.admin.command('ping')
        
        db_name = os.environ.get("DB_NAME", "streamhub")
        collection_name = os.environ.get("COLLECTION_NAME", "content_items")
        
        db = client[db_name]
        content_collection = db[collection_name]
        counter_collection = db["counters"]
        
        # Ensure indexes
        content_collection.create_index([("created_at", -1)])
        content_collection.create_index([("tags", 1)])
        content_collection.create_index([("views", -1)])
        
        logger.info(f"MongoDB connected successfully. Database: {db_name}")
        return True
        
    except Exception as e:
        logger.error(f"MongoDB initialization failed: {e}")
        content_collection = None
        client = None
        db = None
        return False

def get_next_sequence_value(sequence_name):
    if counter_collection is None:
        return 0
    try:
        result = counter_collection.find_one_and_update(
            {'_id': sequence_name},
            {'$inc': {'sequence_value': 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        return result['sequence_value']
    except Exception as e:
        logger.error(f"Error fetching sequence counter: {e}")
        return 0

# --- SIMPLE AUTHENTICATION ---
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USERNAME or auth.password != ADMIN_PASSWORD:
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- CACHING SYSTEM ---
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
                return content_cache[cache_key]
            
            response = f(*args, **kwargs)
            
            if isinstance(response, tuple) and response[1] == 200:
                content_cache[cache_key] = response
            
            return response
        return decorated_function
    return decorator

# --- VIEW COUNT FUNCTIONALITY ---
view_count_cache = {}
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

# --- TELEGRAM AND FLASK SETUP ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
APP_URL = os.environ.get("APP_URL")
PORT = int(os.environ.get("PORT", 8000))

# Clean the bot token and create API URL
if BOT_TOKEN:
    BOT_TOKEN = BOT_TOKEN.strip()
    TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
else:
    TELEGRAM_API = None
    logger.warning("BOT_TOKEN environment variable is not set. Telegram features disabled.")

app = Flask(__name__)
CORS(app)

# Global state
USER_STATE = {}
GROUP_WELCOME_SENT = set()

# Keyboard template - ADDED /repost_10
START_KEYBOARD = {
    'keyboard': [
        [{'text': '/add'}, {'text': '/edit'}, {'text': '/delete'}, {'text': '/files'}], 
        [{'text': '/post_diskwala'}, {'text': '/repost_10'}, {'text': '/broadcast'}, {'text': '/cancel'}] 
    ],
    'resize_keyboard': True,
    'one_time_keyboard': False
}

# --- SIMPLIFIED TELEGRAM FUNCTIONS ---

def send_telegram_request(method, payload):
    """Universal function to send requests to Telegram API with robust error handling"""
    if not TELEGRAM_API:
        logger.warning("Telegram bot token not configured")
        return False
    
    url = TELEGRAM_API + method
    
    # Clean the payload - remove any None values and ensure proper encoding
    clean_payload = {}
    for key, value in payload.items():
        if value is not None:
            if isinstance(value, str):
                # Clean the string of any problematic characters
                value = value.replace('\\u', '\\\\u')  # Fix unicode escape issues
            clean_payload[key] = value
    
    logger.info(f"Sending Telegram {method} to chat {clean_payload.get('chat_id')}")
    
    try:
        response = requests.post(url, json=clean_payload, timeout=15)
        
        if response.status_code == 400:
            # Try without parse_mode if that's the issue
            if 'parse_mode' in clean_payload:
                logger.info(f"Bad request with parse_mode, retrying without...")
                clean_payload.pop('parse_mode', None)
                response = requests.post(url, json=clean_payload, timeout=15)
        
        response.raise_for_status()
        result = response.json()
        
        if result.get('ok'):
            logger.info(f"Telegram {method} successful")
            return result.get('result')
        else:
            logger.error(f"Telegram API error: {result}")
            return False
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Telegram API request failed: {e}")
        return False

def send_message(chat_id, text, reply_markup=None):
    """Send simple text message to Telegram"""
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': None  # Always use plain text to avoid formatting issues
    }
    
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    
    return send_telegram_request('sendMessage', payload)

# NOTE: send_photo is kept simple as it's only used for direct file sending/debug, 
# main posting uses send_telegram_request with file_id

def send_photo(chat_id, photo_file_path, caption=None, reply_markup=None):
    """Send photo file to Telegram (used for files on disk, not file_id)"""
    try:
        url = f"{TELEGRAM_API}sendPhoto"
        
        with open(photo_file_path, 'rb') as photo_file:
            files = {'photo': photo_file}
            data = {'chat_id': chat_id}
            
            if caption:
                data['caption'] = caption
            
            if reply_markup:
                data['reply_markup'] = json.dumps(reply_markup)
            
            response = requests.post(url, files=files, data=data, timeout=30)
            
            if response.ok:
                logger.info("Photo sent successfully")
                return response.json().get('result')
            else:
                logger.error(f"Failed to send photo: {response.text}")
                return False
                
    except Exception as e:
        logger.error(f"Error sending photo: {e}")
        return False

def copy_message(chat_id, from_chat_id, message_id, caption=None):
    """Copy message between chats"""
    payload = {
        'chat_id': chat_id,
        'from_chat_id': from_chat_id,
        'message_id': message_id,
        'caption': caption,
        'parse_mode': None  # Plain text only
    }
    
    return send_telegram_request('copyMessage', payload)

def send_diskwala_post(diskwala_url, title, thumbnail_file_id):
    """Send DiskWala link to group using a Telegram File ID as thumbnail."""
    if not TELEGRAM_API or GROUP_TELEGRAM_ID is None:
        return False

    message_text = (
        f"🔥 NEW RELEASE: {title} 🔥\n\n"
        f"Watch Now: {diskwala_url}\n\n"
        f"Powered by {PRODUCT_NAME}"
    )
    
    # Use sendPhoto with the stored file_id
    success = send_telegram_request('sendPhoto', {
        'chat_id': GROUP_TELEGRAM_ID,
        'photo': thumbnail_file_id,
        'caption': message_text
    })
    
    return success

# --- CONTENT MANAGEMENT FUNCTIONS ---

def get_content_info_for_edit(content_id):
    if content_collection is None:
        return None
    try:
        if not ObjectId.is_valid(content_id):
            return None
        return content_collection.find_one({"_id": ObjectId(content_id)})
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

def get_random_content(limit=10):
    """Fetches random content that has a telegram_file_id for reposting."""
    if content_collection is None:
        return []
    try:
        # Only fetch items that have a telegram_file_id for reposting
        pipeline = [
            {"$match": {"telegram_file_id": {"$exists": True, "$ne": None}}},
            {"$sample": {"size": limit}}
        ]
        random_docs = list(content_collection.aggregate(pipeline))
        
        result = []
        for doc in random_docs:
            doc['_id'] = str(doc['_id']) 
            result.append(doc)
        return result
    except Exception as e:
        logger.error(f"Error fetching random content: {e}")
        return []

def save_content(content_data):
    """Saves content data, including new fields for DiskWala posting."""
    if content_collection is None: 
        return False
    try:
        # Clean tags and normalize links
        tags = [t.strip().lower() for t in content_data.get('tags', '').split(',') if t.strip()]
        links = content_data.get('links', [])
        
        document = {
            "title": content_data.get('title'),
            "type": content_data.get('type'),
            "thumbnail_url": content_data.get('thumbnail_url'),
            "telegram_file_id": content_data.get('telegram_file_id'), # NEW: Stored for reposting
            "diskwala_url": content_data.get('diskwala_url'),         # NEW: Stored for reposting
            "tags": tags,
            "links": links,
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

def repost_single_content(doc):
    """Sends a single content item as a new post to the group."""
    if GROUP_TELEGRAM_ID is None: return False

    title = doc.get('title', 'Untitled Content')
    diskwala_url = doc.get('diskwala_url')
    thumbnail_file_id = doc.get('telegram_file_id') # Use the stored File ID

    if not diskwala_url or not thumbnail_file_id:
        logger.warning(f"Skipping repost for {doc.get('_id', 'unknown')}: Missing URL or thumbnail.")
        return False

    message_text = (
        f"🔄 REPOST: {title}\n\n"
        f"Watch Now: {diskwala_url}\n\n"
        f"Powered by {PRODUCT_NAME}"
    )

    success = send_telegram_request('sendPhoto', {
        'chat_id': GROUP_TELEGRAM_ID,
        'photo': thumbnail_file_id,
        'caption': message_text
    })

    return success

# --- FLASK ROUTES (Unchanged) ---

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": PRODUCT_NAME, 
        "status": "online",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

@app.route('/health', methods=['GET'])
def health():
    try:
        if client is not None:
            client.admin.command('ping')
            return jsonify({
                "status": "healthy", 
                "database": "connected",
                "timestamp": datetime.utcnow().isoformat()
            }), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
    
    return jsonify({"status": "unhealthy", "database": "disconnected"}), 503

@app.route('/api/track-view', methods=['POST'])
def track_view():
    try:
        data = request.get_json(silent=True) or {}
        content_id = data.get('content_id')
        
        if not content_id:
            return jsonify({"success": False, "error": "Content ID required"}), 400
        
        increment_view_count(content_id)
        return jsonify({"success": True, "content_id": content_id, "message": "View count updated"}), 200
            
    except Exception as e:
        logger.error(f"View tracking error: {e}")
        return jsonify({"success": False, "error": "Tracking failed"}), 500

@app.route('/api/content', methods=['GET'])
@cached_response(timeout=30)
def get_content():
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        page = max(1, int(request.args.get('page', 1)))
        limit = min(int(request.args.get('limit', 20)), 50)
        skip = (page - 1) * limit
        
        content_type = request.args.get('type')
        tag_filter = request.args.get('tag')
        search_query = request.args.get('q') 
        
        query = {}
        
        if content_type:
            query['type'] = content_type
            
        if tag_filter:
            query['tags'] = tag_filter.lower()
            
        if search_query:
            search_regex = {"$regex": search_query, "$options": "i"}
            query['$or'] = [
                {"title": search_regex},
                {"tags": search_regex},
            ]

        projection = {
            'title': 1, 'type': 1, 'thumbnail_url': 1, 'tags': 1, 
            'views': 1, 'created_at': 1, 'links': 1
        }
        
        total_count = content_collection.count_documents(query)
        
        content_cursor = content_collection.find(
            query, 
            projection
        ).sort("created_at", -1).skip(skip).limit(limit)
        
        content_list = []
        for doc in content_cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            content_list.append(doc)
        
        return jsonify({
            "success": True, "data": content_list,
            "pagination": {
                "page": page, "limit": limit, "total": total_count,
                "pages": (total_count + limit - 1) // limit
            }
        }), 200
        
    except Exception as e:
        logger.error(f"API Fetch Error: {e}")
        return jsonify({"success": False, "error": "Failed to retrieve content."}), 500

@app.route('/api/content/<content_id>', methods=['GET'])
@cached_response(timeout=30)
def get_content_by_id(content_id):
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        doc = content_collection.find_one({"_id": ObjectId(content_id)})
        if not doc:
            return jsonify({"success": False, "error": "Content not found"}), 404
        
        doc['_id'] = str(doc['_id'])
        if 'created_at' in doc:
            doc['created_at'] = doc['created_at'].isoformat()
        
        return jsonify({"success": True, "data": doc}), 200
        
    except Exception as e:
        logger.error(f"API Single Fetch Error: {e}")
        return jsonify({"success": False, "error": "Invalid content ID"}), 400

@app.route('/api/content/similar/<tags>', methods=['GET'])
@cached_response(timeout=30)
def get_similar_content(tags):
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    target_tags = [t.strip().lower() for t in tags.split(',') if t.strip()]

    if not target_tags:
        return jsonify({"success": True, "data": []}), 200

    try:
        query = {"tags": {"$in": target_tags}}
        content_cursor = content_collection.find(query).sort("views", -1).limit(10)
        
        content_list = []
        for doc in content_cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            content_list.append(doc)
            
        return jsonify({"success": True, "data": content_list}), 200
    except Exception as e:
        logger.error(f"API Similar Fetch Error: {e}")
        return jsonify({"success": False, "error": "Failed to retrieve similar content."}), 500

# --- ADMIN ROUTES (Unchanged) ---

@app.route('/api/admin/content', methods=['POST'])
@require_auth
def admin_create_content():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400
        
        content_id = save_content(data)
        if content_id:
            return jsonify({"success": True, "message": "Content created successfully", "id": content_id}), 201
        else:
            return jsonify({"success": False, "error": "Failed to create content"}), 500
            
    except Exception as e:
        logger.error(f"Admin content creation error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/content/<content_id>', methods=['PUT'])
@require_auth
def admin_update_content(content_id):
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No update data provided"}), 400
        
        if update_content(content_id, data):
            return jsonify({"success": True, "message": f"Content {content_id} updated successfully"}), 200
        else:
            return jsonify({"success": False, "error": "Content not found or no changes made"}), 404
            
    except Exception as e:
        logger.error(f"Admin content update error: {e}")
        return jsonify({"success": False, "error": "Invalid content ID or update failed"}), 400

@app.route('/api/admin/content/<content_id>', methods=['DELETE'])
@require_auth
def admin_delete_content(content_id):
    if content_collection is None:
        return jsonify({"success": False, "error": "Database not configured."}), 503
        
    try:
        result = content_collection.delete_one({"_id": ObjectId(content_id)})
        if result.deleted_count > 0:
            content_cache.clear()
            return jsonify({"success": True, "message": "Content deleted successfully"}), 200
        else:
            return jsonify({"success": False, "error": "Content not found"}), 404
            
    except Exception as e:
        logger.error(f"Admin content deletion error: {e}")
        return jsonify({"success": False, "error": "Invalid content ID"}), 400

# --- COMPLETE TELEGRAM WEBHOOK HANDLER (Updated) ---

@app.route('/webhook', methods=['POST'])
def webhook():
    """Complete webhook handler with new DiskWala and repost logic."""
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
        user_id = message['from']['id']
        user_state = USER_STATE.get(chat_id, {'step': 'main'})
        
        # Only respond to admin in private chats
        if chat_id > 0 and user_id != ADMIN_TELEGRAM_ID:
            send_message(chat_id, "❌ Access Denied. Only administrator can use this bot.")
            return jsonify({"status": "unauthorized"}), 200
        
        # --- NEW DiskWala Multi-step conversation handlers ---

        if user_state['step'] == 'awaiting_diskwala_photo':
            if 'photo' in message:
                # Get the largest photo file ID
                photo_file_id = message['photo'][-1]['file_id']
                # Initialize data dictionary for this flow
                USER_STATE[chat_id]['data'] = {'telegram_file_id': photo_file_id} 
                USER_STATE[chat_id]['step'] = 'awaiting_diskwala_title'
                send_message(chat_id, "✅ Thumbnail saved. Now send the **Title** for the post.")
            else:
                send_message(chat_id, "❌ Please send a photo file for the thumbnail.")
            return jsonify({"status": "ok"}), 200

        elif user_state['step'] == 'awaiting_diskwala_title':
            USER_STATE[chat_id]['data']['title'] = text.strip()
            USER_STATE[chat_id]['step'] = 'awaiting_diskwala_url'
            send_message(chat_id, "✅ Title saved. Now send the **DiskWala URL**.")
            return jsonify({"status": "ok"}), 200

        elif user_state['step'] == 'awaiting_diskwala_url':
            diskwala_url = text.strip()
            
            if not diskwala_url.startswith('http'):
                send_message(chat_id, "❌ Please send a valid URL starting with http:// or https://")
                return jsonify({"status": "ok"}), 200
            
            title = user_state['data']['title']
            thumbnail_file_id = user_state['data']['telegram_file_id']
            
            send_message(chat_id, "⏳ Posting to group and saving to database...")

            # 1. Post to group
            post_result = send_diskwala_post(diskwala_url, title, thumbnail_file_id)
            
            if post_result:
                # 2. Save post details to database (Simplified save)
                content_data = {
                    "title": title,
                    "type": "video", # Default to video type for DiskWala posts
                    "thumbnail_url": f"telegram_file_id:{thumbnail_file_id}", # Reference the file ID
                    "telegram_file_id": thumbnail_file_id, 
                    "diskwala_url": diskwala_url,
                    "tags": title.lower().split(), # Use title words as basic tags
                    "links": [{"url": diskwala_url, "episode_title": "Watch Now"}],
                }
                content_id = save_content(content_data)

                if content_id:
                    send_message(chat_id, 
                                 f"🎉 Success! Post '{title}' published and saved with ID: {content_id}.", 
                                 START_KEYBOARD)
                else:
                    send_message(chat_id, 
                                 f"✅ Post published to group. ❌ Failed to save to database.", 
                                 START_KEYBOARD)
            else:
                send_message(chat_id, "❌ Failed to post to Telegram group.", START_KEYBOARD)

            USER_STATE[chat_id] = {'step': 'main'}
            return jsonify({"status": "diskwala posted"}), 200

        # --- Handle Commands ---
        if text.startswith('/start'):
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(chat_id, f"🚀 Welcome to {PRODUCT_NAME} Admin Bot!\n\nUse /post_diskwala to start the new posting workflow, or /repost_10 to quickly refresh content. Or use the buttons below:", START_KEYBOARD)
            
        elif text.startswith('/post_diskwala'):
            USER_STATE[chat_id] = {'step': 'awaiting_diskwala_photo'}
            send_message(chat_id, "➡️ POST DiskWala: Please send the **Thumbnail Photo** for the new post now.")

        elif text.startswith('/repost_10'):
            send_message(chat_id, "⏳ Fetching 10 random content items for reposting...")
            random_items = get_random_content(limit=10)
            
            if not random_items:
                send_message(chat_id, "❌ Could not find any content eligible for repost (must be posted via DiskWala flow).", START_KEYBOARD)
            else:
                reposted_count = 0
                for item in random_items:
                    if repost_single_content(item):
                        reposted_count += 1
                        time.sleep(1) # Delay to avoid flood limits
                
                send_message(chat_id, f"✅ Repost complete. {reposted_count}/{len(random_items)} items successfully reposted.", START_KEYBOARD)
            
            USER_STATE[chat_id] = {'step': 'main'}

        elif text.startswith('/add'):
            USER_STATE[chat_id] = {'step': 'add_title', 'data': {'links': []}}
            send_message(chat_id, "➡️ ADD Content: Please send the Title of the content (for web app database only).")
            
        elif text.startswith('/edit'):
            USER_STATE[chat_id] = {'step': 'edit_id', 'data': {}}
            send_message(chat_id, "➡️ EDIT Content: Please send the Content ID you want to edit.")
            
        elif text.startswith('/delete'):
            USER_STATE[chat_id] = {'step': 'delete_id', 'data': {}}
            send_message(chat_id, "➡️ DELETE Content: Please send the Content ID to confirm deletion.")
            
        elif text.startswith('/files'):
            if content_collection is None:
                send_message(chat_id, "❌ Database is currently unavailable.")
                return jsonify({"status": "ok"}), 200
                
            try:
                # Fetch top 10 most recent titles and IDs
                content_cursor = content_collection.find({}, {'title': 1, 'created_at': 1}).sort("created_at", -1).limit(10)
                
                content_list_text = []
                for i, doc in enumerate(content_cursor):
                    title = doc.get('title', 'No Title')
                    _id = str(doc['_id'])
                    content_list_text.append(f"{i+1}. {title} ({_id})")
                    
                if content_list_text:
                    response_text = "📚 Latest 10 Content Items (Title & ID):\n\n" + "\n".join(content_list_text)
                else:
                    response_text = "No content has been uploaded yet."
                
                send_message(chat_id, response_text, START_KEYBOARD)
            except Exception as e:
                logger.error(f"Error fetching files list: {e}")
                send_message(chat_id, "❌ An error occurred while fetching the file list.")
            
        elif text.startswith('/broadcast'):
            USER_STATE[chat_id] = {'step': 'broadcast_message'}
            send_message(chat_id, "➡️ BROADCAST: Send the message you want to broadcast to the group.")
            
        elif text.startswith('/cancel'):
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(chat_id, "❌ Operation cancelled. Choose a new action:", START_KEYBOARD)
            
        # Multi-step conversation handlers (from original /add flow)
        elif user_state['step'] == 'broadcast_message':
            broadcast_text = text
            send_message(GROUP_TELEGRAM_ID, f"📢 ADMIN ANNOUNCEMENT:\n\n{broadcast_text}")
            send_message(chat_id, "✅ Message broadcasted to the group successfully.", START_KEYBOARD)
            USER_STATE[chat_id] = {'step': 'main'}

        elif user_state['step'] == 'add_title':
            user_state['data']['title'] = text
            user_state['step'] = 'add_type'
            send_message(chat_id, "✅ Title saved. Now send the Type (e.g., movie or series).")
            
        elif user_state['step'] == 'add_type':
            user_state['data']['type'] = text
            user_state['step'] = 'add_thumbnail'
            send_message(chat_id, "✅ Type saved. Now send the Thumbnail URL.")
            
        elif user_state['step'] == 'add_thumbnail':
            user_state['data']['thumbnail_url'] = text
            user_state['step'] = 'add_tags'
            send_message(chat_id, "✅ URL saved. Now send Tags (comma-separated, e.g., action, thriller, new).")
            
        elif user_state['step'] == 'add_tags':
            user_state['data']['tags'] = text
            user_state['step'] = 'add_episode_title'
            send_message(chat_id, "✅ Tags saved. Now send the Episode Title (e.g., 'Episode 1' or 'Main Link'). Type DONE to finish.")
            
        elif user_state['step'] == 'add_episode_title':
            if text.upper() == 'DONE':
                if not user_state['data']['links']:
                    send_message(chat_id, "❌ Cannot save content without any links. Please add at least one link or type /cancel.")
                    return jsonify({"status": "ok"}), 200

                content_id = save_content(user_state['data'])
                if content_id:
                    send_message(chat_id, 
                                 f"🎉 Success! Content '{user_state['data']['title']}' added to {PRODUCT_NAME} with ID: {content_id}.\n"
                                 f"Access content at: https://{ACCESS_URL}/content/{content_id}", 
                                 START_KEYBOARD)
                else:
                    send_message(chat_id, "❌ Save Failed. Check server logs for MongoDB error.", START_KEYBOARD)
                
                USER_STATE[chat_id] = {'step': 'main'}
            else:
                user_state['data']['current_episode_title'] = text
                user_state['step'] = 'add_episode_url'
                send_message(chat_id, f"✅ Episode Title saved. Now send the URL for '{text}'.")

        elif user_state['step'] == 'add_episode_url':
            episode_title = user_state['data'].pop('current_episode_title', 'Link')
            submitted_url = text
            
            # LULUVid URL modification
            LULUVID_DOMAIN = 'https://luluvid.com/'
            LULUVID_EMBED = 'https://luluvid.com/e/'
            modified_url = submitted_url

            if submitted_url.startswith(LULUVID_DOMAIN) and not submitted_url.startswith(LULUVID_EMBED):
                content_path = submitted_url[len(LULUVID_DOMAIN):].strip('/')
                if content_path and '/' not in content_path:
                    modified_url = LULUVID_EMBED + content_path
                    logger.info(f"LULUVID modification: {submitted_url} -> {modified_url}")
                
            user_state['data']['links'].append({
                "url": modified_url, 
                "episode_title": episode_title
            })
            
            user_state['step'] = 'add_episode_title'
            send_message(chat_id, f"✅ Link saved. Current links: {len(user_state['data']['links'])}. Send the NEXT Episode Title or type DONE.")
            
        # EDIT FLOW (Unchanged)
        elif user_state['step'] == 'edit_id':
            content_id = text
            content_doc = get_content_info_for_edit(content_id)
            if content_doc:
                user_state['data']['_id'] = content_id
                user_state['step'] = 'edit_field'
                
                edit_fields = ['Title', 'Type', 'Thumbnail URL', 'Tags', 'Links'] 
                keyboard_buttons = [[{'text': f'/edit_{f.lower().replace(" ", "_")}'}] for f in edit_fields]
                keyboard_buttons.append([{'text': '/cancel'}])

                info_text = (
                    f"🔍 Editing Content ID: {content_id}\n\n"
                    f"Current Title: {content_doc.get('title', 'N/A')}\n"
                    f"Current Type: {content_doc.get('type', 'N/A')}\n\n" 
                    "Please select a field to update:"
                )
                
                send_message(chat_id, info_text, {'keyboard': keyboard_buttons, 'resize_keyboard': True})
            else:
                send_message(chat_id, "❌ Content ID not found or invalid. Try again or type /cancel.")

        elif user_state['step'] == 'edit_field' and text.startswith('/edit_'):
            field = text.split('_', 1)[1]
            user_state['step'] = f'edit_new_{field}'
            prompt = f"➡️ Please send the NEW value for {field.replace('_', ' ').title()}."
            if field == 'links':
                 prompt += "\n(For Links, send the complete, updated list of JSON links, or the single new URL for a movie.)"
            send_message(chat_id, prompt)
            
        elif user_state['step'].startswith('edit_new_'):
            field = user_state['step'].split('_new_')[1]
            content_id = user_state['data']['_id']
            
            update_data = {}
            if field == 'tags':
                update_data['tags'] = [t.strip().lower() for t in text.split(',') if t.strip()]
            elif field == 'links':
                modified_link_text = text
                
                LULUVID_DOMAIN = 'https://luluvid.com/'
                LULUVID_EMBED = 'https://luluvid.com/e/'

                if modified_link_text.startswith(LULUVID_DOMAIN) and not modified_link_text.startswith(LULUVID_EMBED):
                    content_path = modified_link_text[len(LULUVID_DOMAIN):].strip('/')
                    if content_path and '/' not in content_path:
                        modified_link_text = LULUVID_EMBED + content_path

                try:
                    links = json.loads(modified_link_text)
                    if not isinstance(links, list): raise ValueError
                    update_data['links'] = links
                except:
                    update_data['links'] = [{"url": modified_link_text, "episode_title": "Watch Link"}]
                    
            elif field == 'title':
                update_data['title'] = text
            elif field == 'type':
                update_data['type'] = text
            elif field == 'thumbnail_url':
                update_data['thumbnail_url'] = text
            
            if update_content(content_id, update_data):
                send_message(chat_id, f"✅ Success! {field.replace('_', ' ').title()} for ID {content_id} updated.", START_KEYBOARD)
            else:
                send_message(chat_id, "❌ Update Failed. Content not found or no changes made.", START_KEYBOARD)

            USER_STATE[chat_id] = {'step': 'main'}

        # DELETE FLOW (Unchanged)
        elif user_state['step'] == 'delete_id':
            content_id = text
            try:
                result = content_collection.delete_one({"_id": ObjectId(content_id)})
                if result.deleted_count > 0:
                    content_cache.clear()
                    send_message(chat_id, f"🗑️ Success! Content ID {content_id} has been deleted.", START_KEYBOARD)
                else:
                    send_message(chat_id, f"❌ Error: Content ID {content_id} not found.", START_KEYBOARD)
            except Exception:
                send_message(chat_id, "❌ Error: Invalid Content ID format.", START_KEYBOARD)

            USER_STATE[chat_id] = {'step': 'main'}
            
        else:
            # If no command matched, show help
            send_message(chat_id, "🤔 I don't recognize that command or state. Use /start to see available commands.")
            
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        send_message(ADMIN_TELEGRAM_ID, f"🚨 Critical Webhook Error: {e}")
        return jsonify({"status": "error"}), 500

# --- BACKGROUND TASKS (Unchanged) ---

def flush_view_cache():
    global view_count_cache
    while True:
        time.sleep(30)  # Reduced frequency
        try:
            with cache_lock:
                if not view_count_cache or content_collection is None:
                    continue
                
                bulk_ops = []
                for cache_key, count in list(view_count_cache.items()):
                    if count > 0:
                        content_id = cache_key.replace('views_', '')
                        if ObjectId.is_valid(content_id):
                            bulk_ops.append(
                                pymongo.operations.UpdateOne(
                                    {"_id": ObjectId(content_id)},
                                    {
                                        "$inc": {"views": count},
                                        "$set": {"last_viewed": datetime.utcnow()}
                                    }
                                )
                            )
                
                if bulk_ops:
                    result = content_collection.bulk_write(bulk_ops, ordered=False)
                    logger.info(f"Flushed {result.modified_count} view count updates")
                    view_count_cache.clear()
                    
        except Exception as e:
            logger.error(f"Error flushing view cache: {e}")

# --- APPLICATION STARTUP (Unchanged) ---

def set_webhook():
    if not APP_URL or not BOT_TOKEN:
        logger.warning("APP_URL or BOT_TOKEN not set. Skipping webhook setup.")
        return False
    
    # Use the simplified webhook endpoint
    webhook_url = f"{APP_URL.rstrip('/')}/webhook"
    
    payload = {
        'url': webhook_url,
        'max_connections': 10,
        'drop_pending_updates': True
    }
    
    return send_telegram_request('setWebhook', payload)

@app.before_request
def before_request():
    global content_collection
    if content_collection is None:
        init_mongodb()

if __name__ == '__main__':
    logger.info("Starting StreamHub Application...")
    
    if not init_mongodb():
        logger.error("Failed to initialize MongoDB. Continuing without database...")
    
    # Start background tasks
    cache_thread = threading.Thread(target=flush_view_cache, daemon=True)
    cache_thread.start()
    
    if APP_URL and BOT_TOKEN:
        if set_webhook():
            logger.info("Webhook set successfully")
        else:
            logger.error("Failed to set webhook")
    else:
        logger.warning("APP_URL or BOT_TOKEN not set - webhook not configured")
    
    logger.info(f"Starting Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)

