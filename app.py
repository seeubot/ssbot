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
import pymongo.operations 

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
        
        # NOTE: maxPoolSize and minPoolSize set for high concurrency
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=5000, 
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            maxPoolSize=50,
            minPoolSize=10,
            maxIdleTimeMS=30000
        )
        
        client.admin.command('ping')
        
        db_name = os.environ.get("DB_NAME", "streamhub")
        collection_name = os.environ.get("COLLECTION_NAME", "content_items")
        
        db = client[db_name]
        content_collection = db[collection_name]
        
        # Ensure indexes exist for performance
        content_collection.create_index([("created_at", -1)])
        content_collection.create_index([("tags", 1)])
        content_collection.create_index([("views", -1)])
        
        logger.info(f"MongoDB connected with connection pooling. Database: {db_name}")
        return True
    except Exception as e:
        logger.error(f"MongoDB initialization failed: {e}")
        # Ensure collection is None if connection failed
        content_collection = None
        client = None
        db = None
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
# TTLCache ensures cache items expire after 30 seconds
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
            
            # Execute the function to get the response
            response = f(*args, **kwargs)
            
            # Cache only successful 200 responses
            if isinstance(response, tuple) and response[1] == 200:
                content_cache[cache_key] = response
            
            return response
        return decorated_function
    return decorator

# --- 4. OPTIMIZED VIEW COUNT FUNCTIONALITY ---
view_count_cache = {}
cache_lock = threading.Lock()

def increment_view_count(content_id):
    """Increment view count for a content item with thread-safe caching."""
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

def get_view_count(content_id):
    """Get view count for a content item."""
    if content_collection is None:
        return 0
    
    try:
        doc = content_collection.find_one(
            {"_id": ObjectId(content_id)}, 
            {"views": 1}
        )
        return doc.get('views', 0) if doc else 0
    except Exception as e:
        logger.error(f"Error getting view count: {e}")
        return 0

# --- 5. TELEGRAM AND FLASK SETUP ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
APP_URL = os.environ.get("APP_URL")
PORT = int(os.environ.get("PORT", 8000))

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN environment variable is not set. Telegram features disabled.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/" if BOT_TOKEN else None

app = Flask(__name__)
# Enable CORS for all routes
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
    """Retrieves content details for display/editing."""
    if content_collection is None:
        return None
    try:
        if not ObjectId.is_valid(content_id):
            return None
        doc = content_collection.find_one({"_id": ObjectId(content_id)})
        return doc
    except Exception:
        return None

def update_content(content_id, update_data):
    """Updates an existing content document in MongoDB."""
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
    """Sends a message back to the user with timeout."""
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
        # Default to main keyboard if in main step
        if USER_STATE.get(chat_id, {}).get('step') == 'main':
             payload['reply_markup'] = json.dumps(START_KEYBOARD)

    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
        logger.info(f"Message sent to chat_id {chat_id}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending message to {chat_id}: {e}")

def save_content(content_data):
    """Saves the complete content document to MongoDB."""
    if content_collection is None: 
        return False
    try:
        document = {
            "title": content_data.get('title'),
            "type": content_data.get('type'),
            "thumbnail_url": content_data.get('thumbnail_url'),
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

# -------------------------------------------------------------
# --- 6. FLASK ROUTES ---
# -------------------------------------------------------------

@app.route('/', methods=['GET'])
def index():
    """Simple status check."""
    return jsonify({
        "service": "StreamHub", 
        "status": "online",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

@app.route('/health', methods=['GET'])
def health():
    """Fast health check endpoint."""
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

@app.route('/api/track-view', methods=['POST', 'OPTIONS'])
def track_view():
    """Fast view count tracking with minimal processing."""
    if request.method == 'OPTIONS':
        # Handle CORS preflight request
        return '', 200
        
    try:
        data = request.get_json(silent=True) or {}
        content_id = data.get('content_id')
        
        if not content_id:
            return jsonify({"success": False, "error": "Content ID required"}), 400
        
        increment_view_count(content_id)
        
        return jsonify({
            "success": True, 
            "content_id": content_id,
            "message": "View count updated"
        }), 200
            
    except Exception as e:
        logger.error(f"View tracking error: {e}")
        return jsonify({"success": False, "error": "Tracking failed"}), 500

@app.route('/api/content', methods=['GET'])
@cached_response(timeout=30)
def get_content():
    """Fast content retrieval with pagination and caching."""
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        page = max(1, int(request.args.get('page', 1)))
        limit = min(int(request.args.get('limit', 20)), 50)
        skip = (page - 1) * limit
        
        content_type = request.args.get('type')
        tag_filter = request.args.get('tag')
        
        query = {}
        if content_type:
            query['type'] = content_type
        if tag_filter:
            query['tags'] = tag_filter.lower()
        
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
    """Fast single content retrieval."""
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
    """Fast similar content retrieval."""
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

# --- ADMIN ROUTES WITH SIMPLE AUTH ---

@app.route('/api/admin/content', methods=['POST'])
@require_auth
def admin_create_content():
    """Admin route to create content."""
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
    """Admin route to update content."""
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
    """Admin route to delete content."""
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

# --- TELEGRAM WEBHOOK HANDLER ---

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
            user_state['step'] = 'add_episode_title'
            send_message(chat_id, "✅ Tags saved. Now, send the **Episode Title** (e.g., 'Episode 1' or 'Main Link'). Type **DONE** to finish.")
            
        # --- Sequential Link Input ---
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
                user_state['data']['current_episode_title'] = text
                user_state['step'] = 'add_episode_url'
                send_message(chat_id, f"✅ Episode Title saved. Now send the **URL** for '{text}'.")

        elif user_state['step'] == 'add_episode_url':
            episode_title = user_state['data'].pop('current_episode_title', 'Link')
            user_state['data']['links'].append({
                "url": text,
                "episode_title": episode_title
            })
            
            user_state['step'] = 'add_episode_title'
            send_message(chat_id, f"✅ Link saved. Current links: {len(user_state['data']['links'])}. Send the **NEXT Episode Title** or type **DONE**.")
            
        # --- EDIT FLOW ---
        
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
                    f"📝 **Editing Content ID**: `{content_id}`\n\n"
                    f"**Current Title**: {content_doc.get('title', 'N/A')}\n"
                    f"**Current Type**: {content_doc.get('type', 'N/A')}\n\n"
                    "Please select a field to update:"
                )
                
                send_message(chat_id, info_text, {'keyboard': keyboard_buttons, 'resize_keyboard': True})
            else:
                send_message(chat_id, "❌ Content ID not found or invalid. Try again or type **/cancel**.")

        # --- Generic Edit Field Handlers ---
        elif user_state['step'] == 'edit_field' and text.startswith('/edit_'):
            field = text.split('_', 1)[1]
            user_state['step'] = f'edit_new_{field}'
            
            prompt = f"➡️ Please send the **NEW value** for **{field.replace('_', ' ').title()}**."
            if field == 'links':
                 prompt += "\n(For Links, send the **complete, updated list** of JSON links, or the single new URL for a movie.)"
                 
            send_message(chat_id, prompt)
            
        # --- Specific Edit Field Value Handlers ---
        elif user_state['step'].startswith('edit_new_'):
            field = user_state['step'].split('_new_')[1]
            content_id = user_state['data']['_id']
            
            update_data = {}
            if field == 'tags':
                update_data['tags'] = [t.strip().lower() for t in text.split(',') if t.strip()]
            elif field == 'links':
                try:
                    links = json.loads(text)
                    if not isinstance(links, list): raise ValueError
                    update_data['links'] = links
                except:
                    # Fallback for simple URL
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
            try:
                result = content_collection.delete_one({"_id": ObjectId(content_id)})
                if result.deleted_count > 0:
                    content_cache.clear()
                    send_message(chat_id, f"🗑️ **Success!** Content ID `{content_id}` has been deleted.", START_KEYBOARD)
                else:
                    send_message(chat_id, f"❌ **Error:** Content ID `{content_id}` not found.", START_KEYBOARD)
            except Exception:
                send_message(chat_id, "❌ **Error:** Invalid Content ID format.", START_KEYBOARD)

            USER_STATE[chat_id] = {'step': 'main'}
            
        else:
            send_message(chat_id, "I don't understand that. Use /start to see the options.", START_KEYBOARD)
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

# -------------------------------------------------------------
# --- BACKGROUND TASKS (Fixed: Global and None Check) ---
# -------------------------------------------------------------

def flush_view_cache():
    """Periodically flush view count cache to database."""
    global view_count_cache 
    while True:
        time.sleep(30)
        try:
            with cache_lock:
                if not view_count_cache:
                    continue
                
                # Explicitly check against None for Collection object
                if content_collection is None:
                    logger.warning("Skipping view cache flush: MongoDB collection is not available.")
                    continue
                    
                bulk_ops = []
                keys_to_delete = [] 
                
                # Iterate over a copy to safely modify the original dictionary later
                for cache_key, count in list(view_count_cache.items()): 
                    if count > 0:
                        content_id = cache_key.replace('views_', '')
                        bulk_ops.append(
                            pymongo.operations.UpdateOne(
                                {"_id": ObjectId(content_id)},
                                {"$inc": {"views": count}}
                            )
                        )
                        keys_to_delete.append(cache_key)

                if bulk_ops: 
                    content_collection.bulk_write(bulk_ops)
                    
                    # Remove the flushed keys from the global cache
                    for key in keys_to_delete:
                        if key in view_count_cache:
                            del view_count_cache[key] 
                
        except Exception as e:
            logger.error(f"Error flushing view cache: {e}")

# -------------------------------------------------------------
# --- APPLICATION STARTUP ---
# -------------------------------------------------------------

def set_webhook():
    """Set the webhook URL for Telegram."""
    if not APP_URL or not BOT_TOKEN:
        logger.
