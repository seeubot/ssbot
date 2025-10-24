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

# --- CONSTANTS & CONFIGURATION ---
ADMIN_TELEGRAM_ID = 1352497419  # User's specified admin ID for Telegram commands
GROUP_TELEGRAM_ID = -1002541647242 # User's specified target group ID for notifications
CONTENT_FORWARD_CHANNEL_ID = -1002776780769 # ID for forwarding uploaded/sent files
PRODUCT_NAME = "Adult-Hub"
ACCESS_URL = "teluguxx.vercel.app" # Base URL for the front end

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- 1. OPTIMIZED MONGODB SETUP WITH CONNECTION POOLING AND RETRY LOGIC ---
client = None
db = None
content_collection = None
counter_collection = None # For sequence numbering

def init_mongodb_with_retry(max_retries=3, retry_delay=2):
    """Initialize MongoDB connection with retry logic for network failures."""
    global client, db, content_collection, counter_collection
    
    for attempt in range(max_retries):
        try:
            MONGODB_URI = os.environ.get("MONGODB_URI")
            if not MONGODB_URI:
                logger.error("MONGODB_URI environment variable is not set.")
                return False
            
            # Enhanced connection settings for better reliability
            client = MongoClient(
                MONGODB_URI,
                serverSelectionTimeoutMS=10000,  # Increased from 5000
                connectTimeoutMS=10000,          # Increased from 5000
                socketTimeoutMS=20000,           # Increased from 10000
                maxPoolSize=50,
                minPoolSize=5,                   # Reduced from 10 to save resources
                maxIdleTimeMS=45000,             # Increased from 30000
                retryWrites=True,                # Enable automatic retry for writes
                retryReads=True,                 # Enable automatic retry for reads
                w='majority'                     # Write concern for durability
            )
            
            # Test connection
            client.admin.command('ping')
            
            db_name = os.environ.get("DB_NAME", "streamhub")
            collection_name = os.environ.get("COLLECTION_NAME", "content_items")
            
            db = client[db_name]
            content_collection = db[collection_name]
            counter_collection = db["counters"]
            
            # Ensure indexes exist for performance 
            content_collection.create_index([("created_at", -1)])
            content_collection.create_index([("tags", 1)])
            content_collection.create_index([("views", -1)])
            
            logger.info(f"MongoDB connected successfully (attempt {attempt + 1}). Database: {db_name}")
            return True
            
        except Exception as e:
            logger.error(f"MongoDB initialization attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
            else:
                content_collection = None
                client = None
                db = None
                return False
    
    return False

def init_mongodb():
    """Legacy wrapper for init_mongodb_with_retry."""
    return init_mongodb_with_retry()

def get_next_sequence_value(sequence_name):
    """Atomically increments and returns the next sequence value from MongoDB."""
    if counter_collection is None:
        logger.warning("Counter collection not initialized.")
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


# --- 2. SIMPLE AUTHENTICATION ---
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

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
GROUP_WELCOME_SENT = set() # To track users welcomed in the group

# Telegram Bot keyboard templates
START_KEYBOARD = {
    'keyboard': [
        [{'text': '/add'}, {'text': '/edit'}, {'text': '/delete'}, {'text': '/files'}], 
        [{'text': '/post'}, {'text': '/broadcast'}, {'text': '/cancel'}] 
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

def get_random_content(limit=5):
    """Fetches a specified number of random content items from MongoDB."""
    if content_collection is None:
        logger.error("MongoDB collection not available for random content fetch.")
        return []
    try:
        # Use the $sample aggregation stage for efficient random document selection
        pipeline = [{"$sample": {"size": limit}}]
        
        random_docs = list(content_collection.aggregate(pipeline))
        
        # Format the documents to include necessary fields
        result = []
        for doc in random_docs:
            # Ensure the _id is converted to a string for consistency
            doc['_id'] = str(doc['_id']) 
            result.append(doc)
        return result
    except Exception as e:
        logger.error(f"Error fetching random content: {e}")
        return []

def is_valid_image_url(url):
    """Validate if URL is likely to work for Telegram photo sending."""
    try:
        if not url or not url.startswith(('http://', 'https://')):
            return False
        
        # Skip HEAD requests for lulucdn.com as they might be blocked
        if 'lulucdn.com' in url:
            # For lulucdn, assume it's valid and let Telegram handle it
            return True
        
        # For other domains, check if URL is accessible and returns an image
        response = requests.head(url, timeout=3, allow_redirects=True)
        content_type = response.headers.get('content-type', '').lower()
        
        # Verify it's an image and under Telegram's 5MB limit
        if 'image' in content_type:
            content_length = response.headers.get('content-length')
            if content_length and int(content_length) > 5 * 1024 * 1024:  # 5MB
                logger.warning(f"Image too large: {url} ({content_length} bytes)")
                return False
            return True
        return False
    except Exception as e:
        logger.warning(f"Image validation failed for {url}: {e}")
        # If validation fails but URL looks reasonable, let Telegram try it
        return any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp'])

def send_message(chat_id, text, reply_markup=None, parse_mode='Markdown'):
    """Sends a message back to the user with timeout and better error handling."""
    if not TELEGRAM_API:
        logger.warning("Telegram bot token not configured")
        return False
    
    url = TELEGRAM_API + "sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': parse_mode
    }
    
    if reply_markup is not None:
        payload['reply_markup'] = json.dumps(reply_markup)
    else:
        # Only show keyboard if we are in the main state of a private chat (chat_id > 0)
        # and the user is the admin.
        if chat_id > 0 and USER_STATE.get(chat_id, {}).get('step') == 'main':
             payload['reply_markup'] = json.dumps(START_KEYBOARD)

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Message sent to chat_id {chat_id}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending message to {chat_id}: {e}")
        
        # Try without Markdown if that was the issue
        if parse_mode == 'Markdown':
            logger.info(f"Retrying without Markdown formatting for chat_id {chat_id}")
            payload['parse_mode'] = None
            try:
                response = requests.post(url, json=payload, timeout=10)
                response.raise_for_status()
                logger.info(f"Message sent to chat_id {chat_id} (without Markdown)")
                return True
            except requests.exceptions.RequestException as e2:
                logger.error(f"Error sending message without Markdown to {chat_id}: {e2}")
        
        return False

def send_group_notification(title, thumbnail_url, content_id):
    """
    Sends notification to group with improved error handling and validation.
    """
    if not TELEGRAM_API or GROUP_TELEGRAM_ID is None:
        logger.warning("Telegram bot or group ID not configured for notification.")
        return

    watch_link = f"https://{ACCESS_URL}"
    
    # Validate the image URL first
    is_valid_image = is_valid_image_url(thumbnail_url)
    
    if is_valid_image:
        # Try sending with photo
        caption_text = (
            f"🔥 **NEW RELEASE!** 🔥\n\n"
            f"*{title}* has been added to {PRODUCT_NAME}!\n\n"
            f"🔗 *Access Site:* `{ACCESS_URL}`"
        )

        inline_keyboard = {
            'inline_keyboard': [
                [{'text': '🎬 Watch Now (In Chat)', 'web_app': {'url': watch_link}}],
            ]
        }

        url = TELEGRAM_API + "sendPhoto"
        payload = {
            'chat_id': GROUP_TELEGRAM_ID,
            'photo': thumbnail_url,
            'caption': caption_text,
            'parse_mode': 'Markdown',
            'disable_notification': False,
            'reply_markup': json.dumps(inline_keyboard)
        }

        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            logger.info(f"Photo notification sent successfully for content ID {content_id}.")
            return
        except requests.exceptions.RequestException as e:
            logger.warning(f"Photo notification failed: {e}. Falling back to text.")
            
            # Try without Markdown in caption
            try:
                payload['parse_mode'] = None
                response = requests.post(url, json=payload, timeout=15)
                response.raise_for_status()
                logger.info(f"Photo notification sent successfully without Markdown for content ID {content_id}.")
                return
            except requests.exceptions.RequestException as e2:
                logger.warning(f"Photo notification without Markdown also failed: {e2}. Falling back to text.")
    else:
        logger.info(f"Invalid/inaccessible image URL, using text notification for: {thumbnail_url}")
    
    # Fallback to text message with inline button
    fallback_text = (
        f"🔥 NEW RELEASE! 🔥\n\n"
        f"{title} has been added to {PRODUCT_NAME}!\n\n"
        f"Access Site: {ACCESS_URL}"
    )
    
    inline_keyboard = {
        'inline_keyboard': [
            [{'text': '🎬 Watch Now', 'web_app': {'url': watch_link}}],
        ]
    }
    
    # Use text-only without Markdown for better compatibility
    send_message(GROUP_TELEGRAM_ID, fallback_text, reply_markup=inline_keyboard, parse_mode=None)

def save_content(content_data):
    """Saves the complete content document to MongoDB (Category removed)."""
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
        "service": PRODUCT_NAME, 
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

@app.route('/api/track-view', methods=['POST'])
def track_view():
    """Fast view count tracking with minimal processing."""
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
    """
    Fast content retrieval with pagination, caching, and flexible search.
    """
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        page = max(1, int(request.args.get('page', 1)))
        limit = min(int(request.args.get('limit', 20)), 50)
        skip = (page - 1) * limit
        
        content_type = request.args.get('type')
        tag_filter = request.args.get('tag')
        
        # New flexible search parameter
        search_query = request.args.get('q') 
        
        query = {}
        
        if content_type:
            query['type'] = content_type
            
        if tag_filter:
            # Traditional exact tag search (for backward compatibility)
            query['tags'] = tag_filter.lower()
            
        # --- FLEXIBLE SEARCH IMPLEMENTATION ---
        if search_query:
            if 'tags' in query:
                del query['tags']
                
            search_regex = {"$regex": search_query, "$options": "i"} # Case-insensitive regex
            
            # Use $or to search across 'title' OR 'tags' 
            query['$or'] = [
                {"title": search_regex},
                {"tags": search_regex},
            ]

        # --- END FLEXIBLE SEARCH ---
        
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
        # Use $in for efficient querying of documents matching any of the tags
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
        user_id = message['from']['id'] # Get the user's ID
        user_state = USER_STATE.get(chat_id, {'step': 'main'})
        
        # 1. GROUP WELCOME MESSAGE (Automatic)
        if chat_id == GROUP_TELEGRAM_ID and 'new_chat_members' in message:
            for member in message['new_chat_members']:
                member_id = member['id']
                member_name = member.get('first_name', 'New User')
                
                # Check if we have already welcomed this user in the group (simple set check)
                if member_id not in GROUP_WELCOME_SENT:
                    welcome_text = (
                        f"👋 Welcome, {member_name}, to the official {PRODUCT_NAME} group!\n\n"
                        f"You can access all our content here: {ACCESS_URL}"
                    )
                    send_message(chat_id, welcome_text, reply_markup=None, parse_mode=None)
                    GROUP_WELCOME_SENT.add(member_id)
            return jsonify({"status": "ok"}), 200 

        # 2. ADMIN-ONLY RESTRICTION (Gatekeeper for all Private Chat interactions)
        if chat_id > 0 and user_id != ADMIN_TELEGRAM_ID:
            send_message(chat_id, "❌ Access Denied. Only the administrator can use this bot.", parse_mode=None)
            return jsonify({"status": "unauthorized"}), 200
        
        # --- 3. FILE COPY/NUMBERING LOGIC (Admin Private Chat Only) ---
        has_media = any(key in message for key in ['photo', 'video', 'document', 'audio', 'sticker', 'animation', 'voice'])
        
        if chat_id > 0 and user_id == ADMIN_TELEGRAM_ID and has_media:
            
            # 1. Get the next sequence number for the post
            sequence_number = get_next_sequence_value("content_post_sequence")
            
            # 2. CREATE NEW CAPTION: Remove original text/links, use only number and product name
            new_caption = f"#{sequence_number}\n\n{PRODUCT_NAME}"

            # 3. Use copyMessage to hide the 'Forwarded from' tag and send the numbered content
            url = TELEGRAM_API + "copyMessage"
            payload = {
                'chat_id': CONTENT_FORWARD_CHANNEL_ID,
                'from_chat_id': chat_id,
                'message_id': message['message_id'],
                'caption': new_caption,
                'parse_mode': None  # No markdown for captions to avoid issues
            }
            
            try:
                response = requests.post(url, json=payload, timeout=10)
                response.raise_for_status()
                logger.info(f"Admin file #{sequence_number} successfully copied to channel {CONTENT_FORWARD_CHANNEL_ID}")
                
                # Send confirmation back to admin
                send_message(chat_id, f"✅ File Copied! Media has been sent to the content channel as post #{sequence_number}.", START_KEYBOARD, parse_mode=None)
            except requests.exceptions.RequestException as e:
                logger.error(f"Error copying file: {e}")
                send_message(chat_id, "❌ File Copy Failed. Could not copy file to the content channel.", START_KEYBOARD, parse_mode=None)

            # File processed, exit webhook to prevent command processing
            return jsonify({"status": "file copied"}), 200

        # --- Command Handlers (Admin Private Chat) ---
        
        # 4. ADMIN BROADCAST COMMAND 
        if text.startswith('/broadcast'):
            if user_id == ADMIN_TELEGRAM_ID:
                USER_STATE[chat_id] = {'step': 'broadcast_message'}
                send_message(chat_id, "➡️ ADMIN BROADCAST: Send the message you want to broadcast to the group.", parse_mode=None)
            else:
                send_message(chat_id, "❌ Access Denied. This command is for administrators only.", START_KEYBOARD, parse_mode=None)
            return jsonify({"status": "ok"}), 200 
            
        elif text.startswith('/start'):
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(chat_id, f"🚀 Welcome back, Admin! What would you like to do in {PRODUCT_NAME}?", START_KEYBOARD, parse_mode=None)
            
        elif text.startswith('/add'):
            USER_STATE[chat_id] = {'step': 'add_title', 'data': {'links': []}}
            send_message(chat_id, "➡️ ADD Content: Please send the Title of the content.", parse_mode=None)
            
        elif text.startswith('/edit'):
            USER_STATE[chat_id] = {'step': 'edit_id', 'data': {}}
            send_message(chat_id, "➡️ EDIT Content: Please send the Content ID you want to edit.", parse_mode=None)
            
        elif text.startswith('/delete'):
            USER_STATE[chat_id] = {'step': 'delete_id', 'data': {}}
            send_message(chat_id, "➡️ DELETE Content: Please send the Content ID to confirm deletion.", parse_mode=None)
            
        elif text.startswith('/cancel'):
            USER_STATE[chat_id] = {'step': 'main'}
            send_message(chat_id, "Operation cancelled. Choose a new action:", START_KEYBOARD, parse_mode=None)
            
        # 5. LIST FILES COMMAND
        elif text.startswith('/files'):
            if content_collection is None:
                send_message(chat_id, "❌ Database is currently unavailable.", parse_mode=None)
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
                
                send_message(chat_id, response_text, START_KEYBOARD, parse_mode=None)
            except Exception as e:
                logger.error(f"Error fetching files list: {e}")
                send_message(chat_id, "❌ An error occurred while fetching the file list.", parse_mode=None)
        
        # 6. POST RANDOM CONTENT COMMAND
        elif text.startswith('/post'):
            if content_collection is None:
                send_message(chat_id, "❌ Database is currently unavailable.", parse_mode=None)
                return jsonify({"status": "ok"}), 200

            try:
                # 1. Calculate the dynamic post limit
                total_content = content_collection.count_documents({})
                
                if total_content == 0:
                    send_message(chat_id, "❌ No content found in the database to post.", START_KEYBOARD, parse_mode=None)
                    return jsonify({"status": "ok"}), 200
                    
                # Calculate the limit: 1/5th of total, capped at 5 and floored at 1.
                calculated_limit = total_content // 5
                post_limit = max(1, min(5, calculated_limit)) # Dynamic limit logic
                
                # Inform user about the determined limit
                send_message(chat_id, f"⏳ Total Content: {total_content}. Fetching {post_limit} random items and initiating posts to the group...", parse_mode=None)

                random_items = get_random_content(post_limit) # Use the dynamic limit
                
                if not random_items:
                    send_message(chat_id, "❌ No content found in the database to post (check count).", START_KEYBOARD, parse_mode=None)
                    return jsonify({"status": "ok"}), 200

                posted_count = 0
                for item in random_items:
                    # Use threading to prevent the webhook from timing out while posting multiple times
                    threading.Thread(
                        target=send_group_notification, 
                        args=(item.get('title', 'Untitled Content'), item.get('thumbnail_url', 'http://example.com/placeholder.png'), item['_id'])
                    ).start()
                    posted_count += 1
                    
                send_message(chat_id, f"✅ Success! Initiated posting of {posted_count} random content items to the group. Check the group for updates.", START_KEYBOARD, parse_mode=None)

            except Exception as e:
                logger.error(f"Error handling /post command: {e}")
                send_message(chat_id, "❌ An error occurred while trying to post content.", START_KEYBOARD, parse_mode=None)
                
            return jsonify({"status": "ok"}), 200

        # --- Multi-step Input Handlers ---
        
        # 7. BROADCAST FLOW HANDLER
        elif user_state['step'] == 'broadcast_message':
            broadcast_text = text
            send_message(GROUP_TELEGRAM_ID, f"📢 ADMIN ANNOUNCEMENT:\n\n{broadcast_text}", parse_mode=None)
            send_message(chat_id, "✅ Message broadcasted to the group successfully.", START_KEYBOARD, parse_mode=None)
            USER_STATE[chat_id] = {'step': 'main'}

        elif user_state['step'] == 'add_title':
            user_state['data']['title'] = text
            user_state['step'] = 'add_type'
            send_message(chat_id, "✅ Title saved. Now send the Type (e.g., movie or series).", parse_mode=None)
            
        elif user_state['step'] == 'add_type':
            user_state['data']['type'] = text
            user_state['step'] = 'add_thumbnail' 
            send_message(chat_id, "✅ Type saved. Now send the Thumbnail URL.", parse_mode=None) 
            
        elif user_state['step'] == 'add_thumbnail':
            user_state['data']['thumbnail_url'] = text
            user_state['step'] = 'add_tags'
            send_message(chat_id, "✅ URL saved. Now send Tags (comma-separated, e.g., action, thriller, new).", parse_mode=None)
            
        elif user_state['step'] == 'add_tags':
            user_state['data']['tags'] = text
            user_state['step'] = 'add_episode_title'
            send_message(chat_id, "✅ Tags saved. Now, send the Episode Title (e.g., 'Episode 1' or 'Main Link'). Type DONE to finish.", parse_mode=None)
            
        # --- Sequential Link Input ---
        elif user_state['step'] == 'add_episode_title':
            if text.upper() == 'DONE':
                if not user_state['data']['links']:
                    send_message(chat_id, "❌ Cannot save content without any links. Please add at least one link or type /cancel.", parse_mode=None)
                    return jsonify({"status": "ok"}), 200

                # --- FINAL SAVE ACTION ---
                content_id = save_content(user_state['data'])
                if content_id:
                    # NEW: Auto-forward notification to group with WebApp button
                    threading.Thread(
                        target=send_group_notification,
                        args=(user_state['data']['title'], user_state['data']['thumbnail_url'], content_id)
                    ).start()
                    
                    # NOTE: This link shown to the admin in private chat still includes the ID for convenience
                    send_message(chat_id, 
                                 f"🎉 Success! Content '{user_state['data']['title']}' added to {PRODUCT_NAME} with ID: {content_id}.\n"
                                 f"Access content at: https://{ACCESS_URL}/content/{content_id}", 
                                 START_KEYBOARD, parse_mode=None)
                else:
                    send_message(chat_id, "❌ Save Failed. Check server logs for MongoDB error.", START_KEYBOARD, parse_mode=None)
                
                USER_STATE[chat_id] = {'step': 'main'}
            else:
                user_state['data']['current_episode_title'] = text
                user_state['step'] = 'add_episode_url'
                send_message(chat_id, f"✅ Episode Title saved. Now send the URL for '{text}'.", parse_mode=None)

        elif user_state['step'] == 'add_episode_url':
            episode_title = user_state['data'].pop('current_episode_title', 'Link')
            submitted_url = text
            modified_url = submitted_url
            
            LULUVID_DOMAIN = 'https://luluvid.com/'
            LULUVID_EMBED = 'https://luluvid.com/e/'

            # --- LULUVid URL Modification Logic ---
            if submitted_url.startswith(LULUVID_DOMAIN) and not submitted_url.startswith(LULUVID_EMBED):
                # Check if it's the root domain + content path (e.g., https://luluvid.com/12345)
                content_path = submitted_url[len(LULUVID_DOMAIN):].strip('/')
                
                # If there's no slash in the remaining path, it's likely a direct content link
                if content_path and '/' not in content_path:
                    modified_url = LULUVID_EMBED + content_path
                    logger.info(f"LULUVID modification: {submitted_url} -> {modified_url}")
            # --- End LULUVid URL Modification Logic ---
                
            user_state['data']['links'].append({
                "url": modified_url, 
                "episode_title": episode_title
            })
            
            user_state['step'] = 'add_episode_title'
            send_message(chat_id, f"✅ Link saved. Current links: {len(user_state['data']['links'])}. Send the NEXT Episode Title or type DONE.", parse_mode=None)
            
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
                    f"🔍 Editing Content ID: {content_id}\n\n"
                    f"Current Title: {content_doc.get('title', 'N/A')}\n"
                    f"Current Type: {content_doc.get('type', 'N/A')}\n\n" 
                    "Please select a field to update:"
                )
                
                send_message(chat_id, info_text, {'keyboard': keyboard_buttons, 'resize_keyboard': True}, parse_mode=None)
            else:
                send_message(chat_id, "❌ Content ID not found or invalid. Try again or type /cancel.", parse_mode=None)

        # --- Generic Edit Field Handlers ---
        elif user_state['step'] == 'edit_field' and text.startswith('/edit_'):
            field = text.split('_', 1)[1]
            user_state['step'] = f'edit_new_{field}'
            
            prompt = f"➡️ Please send the NEW value for {field.replace('_', ' ').title()}."
            if field == 'links':
                 prompt += "\n(For Links, send the complete, updated list of JSON links, or the single new URL for a movie.)"
                 
            send_message(chat_id, prompt, parse_mode=None)
            
        # --- Specific Edit Field Value Handlers ---
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

                # Apply LULUVid modification to the single link update as well
                if modified_link_text.startswith(LULUVID_DOMAIN) and not modified_link_text.startswith(LULUVID_EMBED):
                    content_path = modified_link_text[len(LULUVID_DOMAIN):].strip('/')
                    if content_path and '/' not in content_path:
                        modified_link_text = LULUVID_EMBED + content_path
                        logger.info(f"LULUVID Edit modification: {text} -> {modified_link_text}")

                try:
                    # Attempt to parse as JSON list of links
                    links = json.loads(modified_link_text)
                    if not isinstance(links, list): raise ValueError
                    update_data['links'] = links
                except:
                    # Fallback to single link for simpler input
                    update_data['links'] = [{"url": modified_link_text, "episode_title": "Watch Link"}]
                    
            elif field == 'title':
                update_data['title'] = text
            elif field == 'type':
                update_data['type'] = text
            elif field == 'thumbnail_url':
                update_data['thumbnail_url'] = text
            
            if update_content(content_id, update_data):
                send_message(chat_id, f"✅ Success! {field.replace('_', ' ').title()} for ID {content_id} updated.", START_KEYBOARD, parse_mode=None)
            else:
                send_message(chat_id, "❌ Update Failed. Content not found or no changes made.", START_KEYBOARD, parse_mode=None)

            USER_STATE[chat_id] = {'step': 'main'}

        # --- DELETE FLOW ---
        
        elif user_state['step'] == 'delete_id':
            content_id = text
            try:
                result = content_collection.delete_one({"_id": ObjectId(content_id)})
                if result.deleted_count > 0:
                    content_cache.clear()
                    send_message(chat_id, f"🗑️ Success! Content ID {content_id} has been deleted.", START_KEYBOARD, parse_mode=None)
                else:
                    send_message(chat_id, f"❌ Error: Content ID {content_id} not found.", START_KEYBOARD, parse_mode=None)
            except Exception:
                send_message(chat_id, "❌ Error: Invalid Content ID format.", START_KEYBOARD, parse_mode=None)

            USER_STATE[chat_id] = {'step': 'main'}
            
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

# -------------------------------------------------------------
# --- BACKGROUND TASKS ---
# -------------------------------------------------------------

def flush_view_cache():
    """Periodically flush view count cache to database with error recovery."""
    global view_count_cache
    consecutive_errors = 0
    max_consecutive_errors = 5
    
    while True:
        time.sleep(15)
        
        if consecutive_errors >= max_consecutive_errors:
            logger.error("Too many consecutive errors in view cache flush. Attempting MongoDB reconnection...")
            if init_mongodb_with_retry():
                consecutive_errors = 0
                logger.info("MongoDB reconnection successful after view cache errors.")
            else:
                logger.error("MongoDB reconnection failed. Waiting 60 seconds before retry...")
                time.sleep(60)
                continue
        
        try:
            with cache_lock:
                if not view_count_cache or content_collection is None:
                    continue
                
                bulk_ops = []
                keys_to_delete = []
                
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
                            keys_to_delete.append(cache_key)
                
                if bulk_ops:
                    result = content_collection.bulk_write(bulk_ops, ordered=False)
                    logger.info(f"Flushed {result.modified_count} view count updates to database")
                    
                    # Clear processed keys
                    for key in keys_to_delete:
                        if key in view_count_cache:
                            del view_count_cache[key]
                    
                    consecutive_errors = 0  # Reset error counter on success
                    
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Error flushing view cache (error count: {consecutive_errors}/{max_consecutive_errors}): {e}")

# -------------------------------------------------------------
# --- APPLICATION STARTUP ---
# -------------------------------------------------------------

def set_webhook():
    """Set the webhook URL for Telegram."""
    if not APP_URL or not BOT_TOKEN:
        logger.warning("APP_URL or BOT_TOKEN not set. Skipping webhook setup.")
        return False
    
    webhook_url = f"{APP_URL.rstrip('/')}/{BOT_TOKEN}"
    url = TELEGRAM_API + "setWebhook"
    
    try:
        response = requests.post(url, json={'url': webhook_url}, timeout=15) 
        response.raise_for_status()
        result = response.json()
        
        if result.get('ok'):
            logger.info(f"Webhook set successfully: {webhook_url}")
            return True
        else:
            logger.error(f"Failed to set webhook: {result}")
            return False
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")
        return False

@app.before_request
def before_request():
    """Initialize connections before handling requests."""
    global content_collection
    if content_collection is None:
        init_mongodb()

if __name__ == '__main__':
    logger.info("Starting Optimized StreamHub (Adult-Hub) Application...")
    
    # Use the retry-enabled initialization
    if not init_mongodb_with_retry():
        logger.error("Failed to initialize MongoDB after retries. Continuing without database...")
    
    # Start background tasks
    cache_thread = threading.Thread(target=flush_view_cache, daemon=True)
    cache_thread.start()
    
    if APP_URL and BOT_TOKEN:
        set_webhook()
    else:
        logger.warning("APP_URL or BOT_TOKEN not set - webhook not configured")
    
    logger.info(f"Starting optimized Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
