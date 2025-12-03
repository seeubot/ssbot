import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS 
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import logging
import threading
from cachetools import TTLCache
from functools import wraps

# --- CONSTANTS & CONFIGURATION ---
ADMIN_TELEGRAM_ID = 1352497419
GROUP_TELEGRAM_ID = -1002541647242  # DiskWala Channel
CONTENT_FORWARD_CHANNEL_ID = -1002776780769  # Video Files Channel
PRODUCT_NAME = "Adult-Hub"

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- MONGODB SETUP ---
MONGODB_URI = "mongodb+srv://room:room@room.4vris.mongodb.net/?appName=room"
client = None
db = None
content_collection = None
counter_collection = None

def init_mongodb():
    global client, db, content_collection, counter_collection
    
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
        client.admin.command('ping')
        
        db_name = os.environ.get("DB_NAME", "streamhub")
        collection_name = os.environ.get("COLLECTION_NAME", "diskwala_posts")
        
        db = client[db_name]
        content_collection = db[collection_name]
        counter_collection = db["counters"]
        
        # Create indexes
        content_collection.create_index([("created_at", -1)])
        content_collection.create_index([("tags", 1)])
        
        logger.info(f"MongoDB connected: {db_name}.{collection_name}")
        return True
        
    except Exception as e:
        logger.error(f"MongoDB init failed: {e}")
        return False

def get_next_sequence(sequence_name):
    try:
        result = counter_collection.find_one_and_update(
            {'_id': sequence_name},
            {'$inc': {'sequence_value': 1}},
            upsert=True,
            return_document=True
        )
        return result['sequence_value']
    except:
        return 0

# --- AUTHENTICATION ---
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USERNAME or auth.password != ADMIN_PASSWORD:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated

# --- CACHING ---
content_cache = TTLCache(maxsize=100, ttl=30)

def cached_response(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method != 'GET':
            return f(*args, **kwargs)
        
        cache_key = f"{request.path}?{str(sorted(request.args.items()))}"
        if cache_key in content_cache:
            return content_cache[cache_key]
        
        response = f(*args, **kwargs)
        if isinstance(response, tuple) and response[1] == 200:
            content_cache[cache_key] = response
        
        return response
    return decorated

# --- VIEW TRACKING ---
view_cache = {}
view_lock = threading.Lock()

def track_view(content_id):
    with view_lock:
        view_cache[content_id] = view_cache.get(content_id, 0) + 1

# --- TELEGRAM SETUP ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
APP_URL = os.environ.get("APP_URL")
PORT = int(os.environ.get("PORT", 8000))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/" if BOT_TOKEN else None

app = Flask(__name__)
CORS(app)

USER_STATE = {}

# Simplified keyboards
MAIN_KEYBOARD = {
    'keyboard': [
        [{'text': 'DiskWala Posts'}, {'text': 'Video Files'}],
        [{'text': 'Cancel'}]
    ],
    'resize_keyboard': True
}

DISKWALA_MODE_KEYBOARD = {
    'keyboard': [
        [{'text': 'Single Post'}, {'text': 'Forward Multiple'}],
        [{'text': 'Back to Menu'}]
    ],
    'resize_keyboard': True
}

# --- TELEGRAM FUNCTIONS ---

def send_telegram(method, payload):
    if not TELEGRAM_API:
        return False
    
    # Clean payload
    clean = {k: v for k, v in payload.items() if v is not None}
    
    try:
        response = requests.post(TELEGRAM_API + method, json=clean, timeout=10)
        
        if response.status_code == 400 and 'parse_mode' in clean:
            clean.pop('parse_mode')
            response = requests.post(TELEGRAM_API + method, json=clean, timeout=10)
        
        response.raise_for_status()
        result = response.json()
        return result.get('result') if result.get('ok') else False
            
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

def send_message(chat_id, text, keyboard=None):
    payload = {'chat_id': chat_id, 'text': text}
    if keyboard:
        payload['reply_markup'] = json.dumps(keyboard)
    return send_telegram('sendMessage', payload)

def send_media_post(title, media_id, media_type, links):
    if not TELEGRAM_API:
        return False

    link_text = "\n".join([f"🔗 {link.get('episode_title', 'Link')}: {link['url']}" for link in links])
    caption = f"🔥 {title} 🔥\n\n{link_text}\n━━━━━━━━━━━━━━━━━\nPowered by {PRODUCT_NAME}"
    
    method = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
    key = 'photo' if media_type == 'photo' else 'video'
    
    return send_telegram(method, {'chat_id': GROUP_TELEGRAM_ID, key: media_id, 'caption': caption})

# --- CONTENT MANAGEMENT ---

def save_content(data):
    if not content_collection:
        return False
    
    try:
        tags = [t.strip().lower() for t in data.get('tags', '').split(',') if t.strip()]
        
        doc = {
            "title": data.get('title'),
            "type": data.get('type'),
            "thumbnail_url": data.get('thumbnail_url'),
            "post_type": data.get('post_type'),
            "telegram_media_id": data.get('telegram_media_id'),
            "telegram_message_id": data.get('telegram_message_id'),
            "telegram_chat_id": data.get('telegram_chat_id'),
            "diskwala_url": data.get('diskwala_url'),
            "tags": tags,
            "links": data.get('links', []),
            "views": 0,
            "created_at": datetime.utcnow()
        }
        
        result = content_collection.insert_one(doc)
        content_cache.clear()
        return str(result.inserted_id)
    except Exception as e:
        logger.error(f"Save error: {e}")
        return False

# --- TELEGRAM UPDATE HANDLER ---

def process_update(update):
    try:
        message = update.get('message')
        if not message:
            return
            
        chat_id = message['chat']['id']
        
        # Ignore messages from DiskWala channel
        if chat_id == GROUP_TELEGRAM_ID:
            return
        
        text = message.get('text', '').strip()
        user_id = message['from']['id']
        
        # Admin only
        if user_id != ADMIN_TELEGRAM_ID:
            send_message(chat_id, "❌ Access Denied. Administrator only.")
            return
        
        state = USER_STATE.get(chat_id, {'step': 'main'})
        
        # --- MAIN MENU ---
        if text in ['/start', 'Back to Menu', 'Cancel']:
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(
                chat_id, 
                f"🚀 {PRODUCT_NAME} Admin Bot\n\n"
                f"📁 DiskWala Posts - Post content to channel\n"
                f"🎬 Video Files - Forward files to video channel\n\n"
                f"Choose an option:",
                MAIN_KEYBOARD
            )
            return
        
        # --- DISKWALA POSTS ---
        if text == 'DiskWala Posts':
            USER_STATE[chat_id] = {'step': 'diskwala_mode'}
            send_message(
                chat_id,
                "📁 DiskWala Posts\n\n"
                "✏️ Single Post - Create one post\n"
                "📨 Forward Multiple - Forward directly\n\n"
                "Choose method:",
                DISKWALA_MODE_KEYBOARD
            )
            return
        
        # --- VIDEO FILES ---
        if text == 'Video Files':
            USER_STATE[chat_id] = {'step': 'video_files'}
            send_message(
                chat_id,
                "🎬 Video Files Mode\n\n"
                "Send files one by one.\n"
                "Type 'Cancel' when done."
            )
            return
        
        # --- DISKWALA MODE ---
        if state['step'] == 'diskwala_mode':
            if text == 'Single Post':
                USER_STATE[chat_id] = {'step': 'diskwala_media', 'data': {}}
                send_message(chat_id, "📤 Step 1/3: Send thumbnail (photo/video)")
                return
            
            elif text == 'Forward Multiple':
                USER_STATE[chat_id] = {'step': 'diskwala_forward'}
                send_message(
                    chat_id,
                    "📨 Forward messages to me.\n"
                    "I'll forward them to DiskWala channel.\n\n"
                    "Type 'Cancel' when done."
                )
                return
        
        # --- SINGLE POST FLOW ---
        if state['step'] == 'diskwala_media':
            media_id = None
            media_type = None
            
            if 'video' in message:
                media_id = message['video']['file_id']
                media_type = 'video'
            elif 'photo' in message:
                media_id = message['photo'][-1]['file_id']
                media_type = 'photo'
            
            if media_id:
                USER_STATE[chat_id]['data'] = {'telegram_media_id': media_id, 'media_type': media_type}
                USER_STATE[chat_id]['step'] = 'diskwala_title'
                send_message(chat_id, f"✅ {media_type.title()} saved!\n\nStep 2/3: Send title")
            else:
                send_message(chat_id, "❌ Send photo or video")
            return
        
        elif state['step'] == 'diskwala_title':
            USER_STATE[chat_id]['data']['title'] = text.strip()
            USER_STATE[chat_id]['step'] = 'diskwala_urls'
            send_message(chat_id, "✅ Title saved!\n\nStep 3/3: Send URLs (one per line)")
            return
        
        elif state['step'] == 'diskwala_urls':
            urls = [url.strip() for url in text.strip().split('\n') if url.strip().startswith('http')]
            
            if not urls:
                send_message(chat_id, "❌ Send valid URLs")
                return
            
            title = state['data']['title']
            media_id = state['data']['telegram_media_id']
            media_type = state['data']['media_type']
            
            links = [{"url": url, "episode_title": "Watch Now" if i == 0 else f"Link {i+1}"} 
                     for i, url in enumerate(urls)]
            
            send_message(chat_id, "⏳ Posting...")
            
            result = send_media_post(title, media_id, media_type, links)
            
            if result:
                content_id = save_content({
                    "title": title,
                    "type": "video",
                    "thumbnail_url": f"telegram_file_id:{media_id}",
                    "post_type": f"diskwala_{media_type}",
                    "telegram_media_id": media_id,
                    "telegram_message_id": result.get('message_id') if isinstance(result, dict) else None,
                    "telegram_chat_id": GROUP_TELEGRAM_ID,
                    "diskwala_url": urls[0],
                    "tags": title.lower().split(),
                    "links": links,
                })
                
                msg = f"🎉 Success!\n✅ Posted to channel"
                if content_id:
                    msg += f"\n📊 ID: {content_id}"
                send_message(chat_id, msg, MAIN_KEYBOARD)
            else:
                send_message(chat_id, "❌ Failed to post", MAIN_KEYBOARD)
            
            USER_STATE[chat_id] = {'step': 'main'}
            return
        
        # --- FORWARD MULTIPLE ---
        elif state['step'] == 'diskwala_forward':
            if 'forward_from' in message or 'forward_from_chat' in message:
                result = send_telegram('copyMessage', {
                    'chat_id': GROUP_TELEGRAM_ID,
                    'from_chat_id': chat_id,
                    'message_id': message['message_id'],
                })
                
                send_message(chat_id, "✅ Forwarded" if result else "❌ Failed")
            else:
                send_message(chat_id, "⚠️ Forward messages from other chats")
            return
        
        # --- VIDEO FILES ---
        elif state['step'] == 'video_files':
            if 'photo' in message or 'video' in message or 'document' in message:
                file_num = get_next_sequence('video_files_counter')
                caption = f"📁 File #{file_num}\n━━━━━━━━━━━━━━━━━\nPowered by {PRODUCT_NAME}"
                
                result = send_telegram('copyMessage', {
                    'chat_id': CONTENT_FORWARD_CHANNEL_ID,
                    'from_chat_id': chat_id,
                    'message_id': message['message_id'],
                    'caption': caption
                })
                
                send_message(chat_id, f"✅ File #{file_num} forwarded" if result else "❌ Failed")
            else:
                send_message(chat_id, "⚠️ Send photo/video/document")
            return
        
    except Exception as e:
        logger.error(f"Process error: {e}")
        send_message(chat_id, "🚨 Error occurred", MAIN_KEYBOARD)

# --- FLASK ROUTES ---

@app.route('/webhook', methods=['POST'])
def webhook():
    if not BOT_TOKEN:
        return jsonify({"status": "not configured"}), 200
        
    try:
        update = request.get_json(silent=True)
        if update:
            threading.Thread(target=process_update, args=(update,), daemon=True).start()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def index():
    return jsonify({"service": PRODUCT_NAME, "status": "online"}), 200

@app.route('/health', methods=['GET'])
def health():
    try:
        if client:
            client.admin.command('ping')
            return jsonify({"status": "healthy", "db": "connected"}), 200
    except:
        pass
    return jsonify({"status": "unhealthy"}), 503

@app.route('/api/track-view', methods=['POST'])
def api_track_view():
    try:
        data = request.get_json() or {}
        content_id = data.get('content_id')
        
        if not content_id:
            return jsonify({"error": "ID required"}), 400
        
        track_view(content_id)
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error(f"Track error: {e}")
        return jsonify({"error": "Failed"}), 500

@app.route('/api/content', methods=['GET'])
@cached_response
def get_content():
    if not content_collection:
        return jsonify({"error": "DB not configured"}), 503

    try:
        page = max(1, int(request.args.get('page', 1)))
        limit = min(int(request.args.get('limit', 20)), 50)
        skip = (page - 1) * limit
        
        query = {}
        
        if content_type := request.args.get('type'):
            query['type'] = content_type
            
        if tag := request.args.get('tag'):
            query['tags'] = tag.lower()
            
        if search := request.args.get('q'):
            regex = {"$regex": search, "$options": "i"}
            query['$or'] = [{"title": regex}, {"tags": regex}]

        projection = {'title': 1, 'type': 1, 'thumbnail_url': 1, 'tags': 1, 'views': 1, 'created_at': 1, 'links': 1}
        
        total = content_collection.count_documents(query)
        cursor = content_collection.find(query, projection).sort("created_at", -1).skip(skip).limit(limit)
        
        items = []
        for doc in cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            items.append(doc)
        
        return jsonify({
            "success": True, 
            "data": items,
            "pagination": {"page": page, "limit": limit, "total": total, "pages": (total + limit - 1) // limit}
        }), 200
        
    except Exception as e:
        logger.error(f"Fetch error: {e}")
        return jsonify({"error": "Failed"}), 500

@app.route('/api/content/<content_id>', methods=['GET'])
@cached_response
def get_content_by_id(content_id):
    if not content_collection:
        return jsonify({"error": "DB not configured"}), 503

    try:
        doc = content_collection.find_one({"_id": ObjectId(content_id)})
        if not doc:
            return jsonify({"error": "Not found"}), 404
        
        doc['_id'] = str(doc['_id'])
        if 'created_at' in doc:
            doc['created_at'] = doc['created_at'].isoformat()
        
        return jsonify({"success": True, "data": doc}), 200
    except:
        return jsonify({"error": "Invalid ID"}), 400

@app.route('/api/content/similar/<tags>', methods=['GET'])
@cached_response
def get_similar(tags):
    if not content_collection:
        return jsonify({"error": "DB not configured"}), 503

    target_tags = [t.strip().lower() for t in tags.split(',') if t.strip()]
    if not target_tags:
        return jsonify({"success": True, "data": []}), 200

    try:
        cursor = content_collection.find({"tags": {"$in": target_tags}}).sort("views", -1).limit(10)
        
        items = []
        for doc in cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            items.append(doc)
            
        return jsonify({"success": True, "data": items}), 200
    except Exception as e:
        logger.error(f"Similar error: {e}")
        return jsonify({"error": "Failed"}), 500

# --- BACKGROUND TASKS ---

def flush_views():
    while True:
        threading.Event().wait(30)  # Flush every 30 seconds
        try:
            with view_lock:
                if not view_cache or not content_collection:
                    continue
                
                for content_id, count in list(view_cache.items()):
                    if count > 0 and ObjectId.is_valid(content_id):
                        content_collection.update_one(
                            {"_id": ObjectId(content_id)},
                            {"$inc": {"views": count}}
                        )
                
                view_cache.clear()
                logger.info("Views flushed")
        except Exception as e:
            logger.error(f"Flush error: {e}")

def set_webhook():
    if not APP_URL or not BOT_TOKEN:
        return False
    
    webhook_url = f"{APP_URL.rstrip('/')}/webhook"
    return send_telegram('setWebhook', {
        'url': webhook_url,
        'max_connections': 10,
        'drop_pending_updates': True
    })

# --- STARTUP ---

if __name__ == '__main__':
    logger.info("Starting StreamHub...")
    
    init_mongodb()
    
    # Start background flush
    threading.Thread(target=flush_views, daemon=True).start()
    
    if APP_URL and BOT_TOKEN:
        if set_webhook():
            logger.info("Webhook set")
        else:
            logger.error("Webhook failed")
    
    logger.info(f"Starting on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
