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
        
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=3000,
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

def require_auth(f):
    """Simple authentication decorator."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USERNAME or auth.password != ADMIN_PASSWORD:
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- 3. SIMPLE CACHING SYSTEM ---
# Use TTLCache for automatic expiration
content_cache = TTLCache(maxsize=100, ttl=30)  # Cache 100 items for 30 seconds

def get_cache_key():
    """Generate cache key from request path and query parameters."""
    path = request.path
    args = sorted(request.args.items())
    return f"{path}?{str(args)}"

def cached_response(timeout=30):
    """Decorator for caching responses."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Only cache GET requests
            if request.method != 'GET':
                return f(*args, **kwargs)
            
            cache_key = get_cache_key()
            if cache_key in content_cache:
                logger.info(f"Cache hit for {cache_key}")
                return content_cache[cache_key]
            
            # Call the actual function
            response = f(*args, **kwargs)
            
            # Cache successful responses
            if response[1] == 200:
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
            if cache_key in view_count_cache:
                view_count_cache[cache_key] += 1
            else:
                view_count_cache[cache_key] = 1
        
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
CORS(app)

# Global state to track multi-step conversation
USER_STATE = {}

# --- 6. OPTIMIZED BOT FUNCTIONS ---
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
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    
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
        
        # Clear cache when new content is added
        content_cache.clear()
        
        return True
    except Exception as e:
        logger.error(f"MongoDB Save Error: {e}")
        return False

# --- 7. OPTIMIZED FLASK ROUTES ---

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
        if content_collection is not None:
            client.admin.command('ping')
            return jsonify({
                "status": "healthy", 
                "database": "connected",
                "timestamp": datetime.utcnow().isoformat()
            }), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
    
    return jsonify({"status": "unhealthy", "database": "disconnected"}), 503

# --- VIEW COUNT TRACKING ---

@app.route('/api/track-view', methods=['POST'])
def track_view():
    """Fast view count tracking with minimal processing."""
    try:
        data = request.get_json(silent=True) or {}
        content_id = data.get('content_id')
        
        if not content_id:
            return jsonify({"success": False, "error": "Content ID required"}), 400
        
        # Async-like behavior - don't wait for DB write
        increment_view_count(content_id)
        
        return jsonify({
            "success": True, 
            "content_id": content_id,
            "message": "View count updated"
        }), 200
            
    except Exception as e:
        logger.error(f"View tracking error: {e}")
        return jsonify({"success": False, "error": "Tracking failed"}), 500

# --- OPTIMIZED CONTENT ROUTES ---

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Fast webhook handler for Telegram updates."""
    if not BOT_TOKEN:
        return jsonify({"status": "telegram not configured"}), 200
        
    try:
        update = request.get_json(silent=True)
        if not update:
            return jsonify({"status": "no data"}), 200
        
        if 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']
            text = message.get('text', '')
            
            if text == '/start':
                send_message(chat_id, "🚀 Welcome to StreamHub Bot! Use /add to upload content.")
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/api/content', methods=['GET'])
@cached_response(timeout=30)
def get_content():
    """Fast content retrieval with pagination and caching."""
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        # Pagination parameters
        page = max(1, int(request.args.get('page', 1)))
        limit = min(int(request.args.get('limit', 20)), 50)
        skip = (page - 1) * limit
        
        # Filter parameters
        content_type = request.args.get('type')
        tag_filter = request.args.get('tag')
        
        # Build query
        query = {}
        if content_type:
            query['type'] = content_type
        if tag_filter:
            query['tags'] = tag_filter.lower()
        
        # Optimized query with projection
        projection = {
            'title': 1, 
            'type': 1, 
            'thumbnail_url': 1, 
            'tags': 1, 
            'views': 1, 
            'created_at': 1,
            'links': 1
        }
        
        # Get total count first (for pagination)
        total_count = content_collection.count_documents(query)
        
        # Get paginated results
        content_cursor = content_collection.find(
            query, 
            projection
        ).sort("created_at", -1).skip(skip).limit(limit)
        
        # Fast conversion to list
        content_list = []
        for doc in content_cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            content_list.append(doc)
        
        return jsonify({
            "success": True,
            "data": content_list,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total_count,
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
        
        return jsonify({
            "success": True,
            "data": doc
        }), 200
        
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
            
        return jsonify({
            "success": True,
            "data": content_list
        }), 200
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
        
        if save_content(data):
            return jsonify({"success": True, "message": "Content created successfully"}), 201
        else:
            return jsonify({"success": False, "error": "Failed to create content"}), 500
            
    except Exception as e:
        logger.error(f"Admin content creation error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/admin/content/<content_id>', methods=['DELETE'])
@require_auth
def admin_delete_content(content_id):
    """Admin route to delete content."""
    try:
        result = content_collection.delete_one({"_id": ObjectId(content_id)})
        if result.deleted_count > 0:
            # Clear cache when content is deleted
            content_cache.clear()
            return jsonify({"success": True, "message": "Content deleted successfully"}), 200
        else:
            return jsonify({"success": False, "error": "Content not found"}), 404
            
    except Exception as e:
        logger.error(f"Admin content deletion error: {e}")
        return jsonify({"success": False, "error": "Invalid content ID"}), 400

# --- BACKGROUND TASKS ---

def flush_view_cache():
    """Periodically flush view count cache to database."""
    while True:
        time.sleep(30)  # Every 30 seconds
        try:
            with cache_lock:
                if not view_count_cache:
                    continue
                    
                for cache_key, count in list(view_count_cache.items()):
                    if count > 0:
                        content_id = cache_key.replace('views_', '')
                        try:
                            result = content_collection.update_one(
                                {"_id": ObjectId(content_id)},
                                {"$inc": {"views": count}}
                            )
                            if result.modified_count > 0:
                                view_count_cache[cache_key] = 0
                        except Exception as e:
                            logger.error(f"Error updating views for {content_id}: {e}")
                
                # Clean up zero counts
                view_count_cache = {k: v for k, v in view_count_cache.items() if v > 0}
                
        except Exception as e:
            logger.error(f"Error flushing view cache: {e}")

# --- APPLICATION STARTUP ---

def set_webhook():
    """Set the webhook URL for Telegram."""
    if not APP_URL or not BOT_TOKEN:
        logger.warning("APP_URL or BOT_TOKEN not set. Skipping webhook setup.")
        return False
    
    webhook_url = f"{APP_URL.rstrip('/')}/{BOT_TOKEN}"
    url = TELEGRAM_API + "setWebhook"
    
    try:
        response = requests.post(url, json={'url': webhook_url}, timeout=10)
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
    if content_collection is None:
        init_mongodb()

# Start background thread for cache flushing
if __name__ == '__main__':
    logger.info("Starting Optimized StreamHub Application...")
    
    if init_mongodb():
        logger.info("MongoDB initialized successfully with connection pooling")
    else:
        logger.warning("MongoDB initialization failed")
    
    # Start background tasks
    cache_thread = threading.Thread(target=flush_view_cache, daemon=True)
    cache_thread.start()
    
    if APP_URL and BOT_TOKEN:
        set_webhook()
    else:
        logger.warning("APP_URL or BOT_TOKEN not set - webhook not configured")
    
    logger.info(f"Starting optimized Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
