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
GROUP_TELEGRAM_ID = -1002541647242  # DiskWala Channel
CONTENT_FORWARD_CHANNEL_ID = -1002776780769  # Video Files Channel
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

# --- AUTHENTICATION ---
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

if BOT_TOKEN:
    BOT_TOKEN = BOT_TOKEN.strip()
    TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
else:
    TELEGRAM_API = None
    logger.warning("BOT_TOKEN environment variable is not set. Telegram features disabled.")

app = Flask(__name__)
CORS(app)

USER_STATE = {}

# Updated Keyboard with new buttons
MAIN_KEYBOARD = {
    'keyboard': [
        [{'text': '📁 DiskWala'}, {'text': '🎬 Video Files'}],
        [{'text': '🔄 Auto Repost (5)'}, {'text': '❌ Cancel'}]
    ],
    'resize_keyboard': True,
    'one_time_keyboard': False
}

DISKWALA_MODE_KEYBOARD = {
    'keyboard': [
        [{'text': '📤 Single Post'}, {'text': '📨 Forward Multiple'}],
        [{'text': '⬅️ Back to Main'}]
    ],
    'resize_keyboard': True,
    'one_time_keyboard': False
}

# --- TELEGRAM FUNCTIONS ---

def send_telegram_request(method, payload):
    """Universal function to send requests to Telegram API"""
    if not TELEGRAM_API:
        logger.warning("Telegram bot token not configured")
        return False
    
    url = TELEGRAM_API + method
    
    clean_payload = {}
    for key, value in payload.items():
        if value is not None:
            if isinstance(value, str):
                value = value.replace('\\u', '\\\\u') 
            clean_payload[key] = value
    
    logger.info(f"Sending Telegram {method} to chat {clean_payload.get('chat_id')}")
    
    try:
        response = requests.post(url, json=clean_payload, timeout=15)
        
        if response.status_code == 400:
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
        'parse_mode': None
    }
    
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    
    return send_telegram_request('sendMessage', payload)

def send_media_post(title, media_id, media_type, links):
    """Send DiskWala post with media and links"""
    if not TELEGRAM_API or GROUP_TELEGRAM_ID is None:
        return False

    link_text = ""
    for i, link in enumerate(links):
        link_title = link.get('episode_title', f'Link {i+1}')
        link_text += f"🔗 {link_title}: {link.get('url')}\n"

    message_text = (
        f"🔥 {title} 🔥\n\n"
        f"{link_text}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Powered by {PRODUCT_NAME}"
    )
    
    method = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
    media_key = 'photo' if media_type == 'photo' else 'video'

    payload = {
        'chat_id': GROUP_TELEGRAM_ID,
        media_key: media_id,
        'caption': message_text
    }
    
    logger.info(f"Sending DiskWala post ({media_type}) to group...")
    return send_telegram_request(method, payload)

# --- CONTENT MANAGEMENT FUNCTIONS ---

def save_content(content_data):
    """Saves content data to MongoDB"""
    if content_collection is None: 
        return False
    try:
        tags = [t.strip().lower() for t in content_data.get('tags', '').split(',') if t.strip()]
        
        document = {
            "title": content_data.get('title'),
            "type": content_data.get('type'),
            "thumbnail_url": content_data.get('thumbnail_url'),
            "post_type": content_data.get('post_type'), 
            "telegram_media_id": content_data.get('telegram_media_id'), 
            "telegram_message_id": content_data.get('telegram_message_id'),
            "telegram_chat_id": content_data.get('telegram_chat_id'),
            "diskwala_url": content_data.get('diskwala_url'),
            "tags": tags,
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

def get_random_diskwala_content(limit=5):
    """Fetches random DiskWala content for reposting"""
    if content_collection is None:
        return []
    try:
        pipeline = [
            {"$match": {
                "post_type": {"$in": ["diskwala_photo", "diskwala_video"]},
                "telegram_media_id": {"$exists": True, "$ne": None}
            }},
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

def repost_diskwala_content(doc):
    """Reposts a single DiskWala content item"""
    if GROUP_TELEGRAM_ID is None:
        return False

    title = doc.get('title', 'Untitled Content')
    media_id = doc.get('telegram_media_id')
    post_type = doc.get('post_type', '')
    links = doc.get('links', [])
    
    if not media_id or not post_type.startswith('diskwala_'):
        return False
    
    media_type = post_type.split('_')[1]
    
    link_text = ""
    for i, link in enumerate(links):
        link_title = link.get('episode_title', f'Link {i+1}')
        link_text += f"🔗 {link_title}: {link.get('url')}\n"
    
    message_text = (
        f"🔄 {title}\n\n"
        f"{link_text}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Powered by {PRODUCT_NAME}"
    )
    
    method = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
    media_key = 'photo' if media_type == 'photo' else 'video'
    
    payload = {
        'chat_id': GROUP_TELEGRAM_ID,
        media_key: media_id,
        'caption': message_text
    }
    
    return send_telegram_request(method, payload)

# --- TELEGRAM UPDATE PROCESSING ---

def process_telegram_update(update):
    """Handles Telegram updates in background thread"""
    try:
        message = update.get('message')
        if not message:
            return
            
        chat_id = message['chat']['id']
        text = message.get('text', '').strip()
        user_id = message['from']['id']
        
        # ADMIN ONLY ACCESS
        if user_id != ADMIN_TELEGRAM_ID:
            send_message(chat_id, "❌ Access Denied. Only administrator can use this bot.")
            return
        
        user_state = USER_STATE.get(chat_id, {'step': 'main'})
        
        # --- MAIN MENU HANDLERS ---
        
        if text == '/start' or text == '⬅️ Back to Main':
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(
                chat_id, 
                f"🚀 Welcome to {PRODUCT_NAME} Admin Bot!\n\n"
                f"Choose an option:",
                MAIN_KEYBOARD
            )
            return
        
        # --- DISKWALA BUTTON ---
        if text == '📁 DiskWala':
            USER_STATE[chat_id] = {'step': 'diskwala_mode'}
            send_message(
                chat_id,
                "📁 DiskWala Mode\n\n"
                "Choose posting method:",
                DISKWALA_MODE_KEYBOARD
            )
            return
        
        # --- VIDEO FILES BUTTON ---
        if text == '🎬 Video Files':
            USER_STATE[chat_id] = {'step': 'video_files_waiting', 'file_count': 0}
            send_message(
                chat_id,
                "🎬 Video Files Mode\n\n"
                "Send me the video files (photo/video/document) one by one.\n"
                "I'll forward them to the channel with file numbers.\n\n"
                "Type '❌ Cancel' when done."
            )
            return
        
        # --- AUTO REPOST BUTTON ---
        if text == '🔄 Auto Repost (5)':
            send_message(chat_id, "⏳ Fetching 5 random DiskWala posts for reposting...")
            random_items = get_random_diskwala_content(limit=5)
            
            if not random_items:
                send_message(chat_id, "❌ No DiskWala content found for repost.", MAIN_KEYBOARD)
            else:
                reposted_count = 0
                for item in random_items:
                    if repost_diskwala_content(item):
                        reposted_count += 1
                        time.sleep(2)
                
                send_message(
                    chat_id, 
                    f"✅ Repost complete!\n{reposted_count}/{len(random_items)} posts successfully reposted.",
                    MAIN_KEYBOARD
                )
            
            USER_STATE[chat_id] = {'step': 'main'}
            return
        
        # --- CANCEL BUTTON ---
        if text == '❌ Cancel':
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(chat_id, "❌ Operation cancelled.", MAIN_KEYBOARD)
            return
        
        # --- DISKWALA MODE HANDLERS ---
        
        if user_state['step'] == 'diskwala_mode':
            if text == '📤 Single Post':
                USER_STATE[chat_id] = {'step': 'diskwala_single_media', 'data': {}}
                send_message(
                    chat_id,
                    "📤 Single DiskWala Post\n\n"
                    "Step 1/3: Send me a thumbnail (photo or video clip)"
                )
                return
            
            elif text == '📨 Forward Multiple':
                USER_STATE[chat_id] = {'step': 'diskwala_forward_multiple'}
                send_message(
                    chat_id,
                    "📨 Forward Multiple Messages\n\n"
                    "Forward multiple messages to me from any chat.\n"
                    "I'll forward them directly to the DiskWala channel without headers.\n\n"
                    "Type '❌ Cancel' when done."
                )
                return
        
        # --- DISKWALA SINGLE POST FLOW ---
        
        if user_state['step'] == 'diskwala_single_media':
            media_id = None
            media_type = None
            
            if 'video' in message:
                media_id = message['video']['file_id']
                media_type = 'video'
            elif 'photo' in message:
                media_id = message['photo'][-1]['file_id']
                media_type = 'photo'
            
            if media_id:
                USER_STATE[chat_id]['data'] = {
                    'telegram_media_id': media_id,
                    'media_type': media_type
                }
                USER_STATE[chat_id]['step'] = 'diskwala_single_title'
                send_message(chat_id, f"✅ {media_type.title()} saved!\n\nStep 2/3: Send me the title for this post")
            else:
                send_message(chat_id, "❌ Please send a photo or video clip.")
            return
        
        elif user_state['step'] == 'diskwala_single_title':
            USER_STATE[chat_id]['data']['title'] = text.strip()
            USER_STATE[chat_id]['step'] = 'diskwala_single_urls'
            send_message(
                chat_id,
                "✅ Title saved!\n\n"
                "Step 3/3: Send me the DiskWala URLs\n"
                "(One URL per line if multiple)"
            )
            return
        
        elif user_state['step'] == 'diskwala_single_urls':
            raw_urls = text.strip().split('\n')
            valid_urls = [url.strip() for url in raw_urls if url.strip().startswith('http')]
            
            if not valid_urls:
                send_message(chat_id, "❌ Please send at least one valid URL starting with http:// or https://")
                return
            
            title = user_state['data']['title']
            media_id = user_state['data']['telegram_media_id']
            media_type = user_state['data']['media_type']
            
            diskwala_links = []
            for i, url in enumerate(valid_urls):
                link_title = f"Watch Now" if i == 0 else f"Link {i+1}"
                diskwala_links.append({"url": url, "episode_title": link_title})
            
            send_message(chat_id, "⏳ Posting to DiskWala channel...")
            
            post_result = send_media_post(title, media_id, media_type, diskwala_links)
            
            if post_result:
                content_data = {
                    "title": title,
                    "type": "video",
                    "thumbnail_url": f"telegram_file_id:{media_id}",
                    "post_type": f"diskwala_{media_type}",
                    "telegram_media_id": media_id,
                    "diskwala_url": valid_urls[0],
                    "tags": title.lower().split(),
                    "links": diskwala_links,
                }
                content_id = save_content(content_data)
                
                if content_id:
                    send_message(
                        chat_id,
                        f"🎉 Success!\n\n"
                        f"Post '{title}' published to DiskWala channel and saved to database.\n"
                        f"ID: {content_id}",
                        MAIN_KEYBOARD
                    )
                else:
                    send_message(
                        chat_id,
                        f"✅ Post published to DiskWala channel.\n"
                        f"❌ Failed to save to database.",
                        MAIN_KEYBOARD
                    )
            else:
                send_message(chat_id, "❌ Failed to post to Telegram channel.", MAIN_KEYBOARD)
            
            USER_STATE[chat_id] = {'step': 'main'}
            return
        
        # --- DISKWALA FORWARD MULTIPLE ---
        
        elif user_state['step'] == 'diskwala_forward_multiple':
            # Check if message is forwarded
            if 'forward_from' in message or 'forward_from_chat' in message:
                original_message_id = message['message_id']
                
                # Copy message to DiskWala channel without header
                copy_result = send_telegram_request('copyMessage', {
                    'chat_id': GROUP_TELEGRAM_ID,
                    'from_chat_id': chat_id,
                    'message_id': original_message_id,
                })
                
                if copy_result:
                    send_message(chat_id, "✅ Forwarded to DiskWala channel (no header)")
                else:
                    send_message(chat_id, "❌ Failed to forward message")
            else:
                send_message(chat_id, "⚠️ Please forward messages from other chats.\n\nType '❌ Cancel' to exit.")
            return
        
        # --- VIDEO FILES HANDLER ---
        
        elif user_state['step'] == 'video_files_waiting':
            is_media = 'photo' in message or 'video' in message or 'document' in message
            
            if is_media:
                file_count = user_state.get('file_count', 0) + 1
                USER_STATE[chat_id]['file_count'] = file_count
                
                original_message_id = message['message_id']
                
                # Generate file caption
                file_caption = f"📁 File #{file_count}\n━━━━━━━━━━━━━━━━━\nPowered by {PRODUCT_NAME}"
                
                # Copy to Video Files channel
                copy_result = send_telegram_request('copyMessage', {
                    'chat_id': CONTENT_FORWARD_CHANNEL_ID,
                    'from_chat_id': chat_id,
                    'message_id': original_message_id,
                    'caption': file_caption
                })
                
                if copy_result:
                    send_message(chat_id, f"✅ File #{file_count} forwarded to Video Files channel!")
                else:
                    send_message(chat_id, "❌ Failed to forward file")
            else:
                send_message(chat_id, "⚠️ Please send photo, video, or document files.\n\nType '❌ Cancel' to finish.")
            return
        
        # Unknown command
        send_message(chat_id, "🤔 I don't understand. Use the buttons below.", MAIN_KEYBOARD)
        
    except Exception as e:
        logger.error(f"Processing error: {e}")
        try:
            send_message(chat_id, f"🚨 Error: {str(e)}", MAIN_KEYBOARD)
        except:
            pass

# --- FLASK ROUTES ---

@app.route('/webhook', methods=['POST'])
def webhook():
    """Telegram webhook endpoint"""
    if not BOT_TOKEN:
        return jsonify({"status": "telegram not configured"}), 200
        
    try:
        update = request.get_json(silent=True)
        if not update:
            return jsonify({"status": "no data"}), 200
        
        threading.Thread(target=process_telegram_update, args=(update,)).start()
        
        return jsonify({"status": "received and processing"}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

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
        return jsonify({"success": True, "content_id": content_id}), 200
            
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

# --- BACKGROUND TASKS ---

def flush_view_cache():
    global view_count_cache
    while True:
        time.sleep(30)
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

# --- APPLICATION STARTUP ---

def set_webhook():
    if not APP_URL or not BOT_TOKEN:
        logger.warning("APP_URL or BOT_TOKEN not set. Skipping webhook setup.")
        return False
    
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
