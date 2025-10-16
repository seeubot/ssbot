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
            serverSelectionTimeoutMS=3000,  # Reduced timeout
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            maxPoolSize=50,  # Connection pooling
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

# --- 2. SIMPLE AUTHENTICATION (NO 2FA) ---
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

# --- 3. OPTIMIZED VIEW COUNT FUNCTIONALITY ---
# Cache for view counts to reduce database writes
view_count_cache = {}

def increment_view_count(content_id):
    """Increment view count for a content item with caching."""
    if content_collection is None:
        return False
    
    try:
        # Use cache key
        cache_key = f"views_{content_id}"
        if cache_key in view_count_cache:
            view_count_cache[cache_key] += 1
        else:
            view_count_cache[cache_key] = get_view_count(content_id) + 1
        
        # Batch update every 10 views or after 30 seconds
        if view_count_cache[cache_key] % 10 == 0:
            result = content_collection.update_one(
                {"_id": ObjectId(content_id)},
                {
                    "$inc": {"views": view_count_cache[cache_key]},
                    "$set": {"last_viewed": datetime.utcnow()}
                }
            )
            if result.modified_count > 0:
                view_count_cache[cache_key] = 0  # Reset after successful update
        
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
            {"views": 1}  # Projection for performance
        )
        return doc.get('views', 0) if doc else 0
    except Exception as e:
        logger.error(f"Error getting view count: {e}")
        return 0

# --- 4. TELEGRAM AND FLASK SETUP ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
APP_URL = os.environ.get("APP_URL")
PORT = int(os.environ.get("PORT", 8000))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

app = Flask(__name__)
CORS(app)

# Response caching
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.wrappers import Response

class CacheMiddleware:
    def __init__(self, app):
        self.app = app
        self.cache = {}
        self.cache_timeout = 30  # seconds

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        method = environ.get('REQUEST_METHOD', 'GET')
        
        # Cache GET requests to /api/content
        if method == 'GET' and path.startswith('/api/content'):
            cache_key = path + '?' + environ.get('QUERY_STRING', '')
            current_time = time.time()
            
            if cache_key in self.cache:
                cached_data, timestamp = self.cache[cache_key]
                if current_time - timestamp < self.cache_timeout:
                    # Return cached response
                    response = Response(cached_data, content_type='application/json')
                    return response(environ, start_response)
        
        # Call the actual app
        response = self.app(environ, start_response)
        
        # Cache successful responses
        if method == 'GET' and path.startswith('/api/content') and response[1] == '200 OK':
            # Store response data in cache
            self.cache[cache_key] = (response[0], current_time)
        
        return response

# Apply caching middleware
app.wsgi_app = CacheMiddleware(app.wsgi_app)

# Global state to track multi-step conversation
USER_STATE = {}

# FSM States
STATE_START = 'START'
STATE_WAITING_FOR_TYPE = 'WAITING_FOR_TYPE'
STATE_WAITING_FOR_TITLE = 'WAITING_FOR_TITLE'
STATE_WAITING_FOR_THUMBNAIL = 'WAITING_FOR_THUMBNAIL'
STATE_WAITING_FOR_TAGS = 'WAITING_FOR_TAGS'
STATE_WAITING_FOR_LINK_TITLE = 'WAITING_FOR_LINK_TITLE'
STATE_WAITING_FOR_LINK_URL = 'WAITING_FOR_LINK_URL'
STATE_CONFIRM_LINK = 'CONFIRM_LINK'
STATE_WAITING_FOR_EDIT_FIELD = 'WAITING_FOR_EDIT_FIELD'
STATE_WAITING_FOR_NEW_VALUE = 'WAITING_FOR_NEW_VALUE'
STATE_CONFIRM_DELETE = 'CONFIRM_DELETE'

# --- 5. OPTIMIZED BOT FUNCTIONS ---
def send_message(chat_id, text, reply_markup=None):
    """Sends a message back to the user with timeout."""
    url = TELEGRAM_API + "sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    
    try:
        response = requests.post(url, json=payload, timeout=5)  # Reduced timeout
        response.raise_for_status()
        logger.info(f"Message sent to chat_id {chat_id}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending message to {chat_id}: {e}")

def save_content(content_data):
    """Saves the complete content document to MongoDB with optimized write."""
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
        return True
    except Exception as e:
        logger.error(f"MongoDB Save Error: {e}")
        return False

# --- 6. OPTIMIZED FLASK ROUTES ---

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
            # Quick ping without full command
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
def get_content():
    """Fast content retrieval with pagination and caching."""
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        # Pagination parameters
        page = int(request.args.get('page', 1))
        limit = min(int(request.args.get('limit', 20)), 50)  # Max 50 items
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
            'created_at': 1
        }
        
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
        
        # Get total count for pagination
        total_count = content_collection.count_documents(query)
        
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
            return jsonify({"success": True, "message": "Content deleted successfully"}), 200
        else:
            return jsonify({"success": False, "error": "Content not found"}), 404
            
    except Exception as e:
        logger.error(f"Admin content deletion error: {e}")
        return jsonify({"success": False, "error": "Invalid content ID"}), 400

# --- APPLICATION STARTUP ---

def set_webhook():
    """Set the webhook URL for Telegram."""
    if not APP_URL:
        logger.warning("APP_URL not set. Skipping webhook setup.")
        return False
    
    webhook_url = f"{APP_URL.rstrip('/')}/{BOT_TOKEN}"
    url = TELEGRAM_API + "setWebhook"
    
    try:
        response = requests.post(url, json={'url': webhook_url}, timeout=5)
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

# Background task to flush view count cache
def flush_view_cache():
    """Periodically flush view count cache to database."""
    while True:
        time.sleep(30)  # Every 30 seconds
        try:
            for cache_key, count in view_count_cache.items():
                if count > 0:
                    content_id = cache_key.replace('views_', '')
                    content_collection.update_one(
                        {"_id": ObjectId(content_id)},
                        {"$inc": {"views": count}}
                    )
                    view_count_cache[cache_key] = 0
        except Exception as e:
            logger.error(f"Error flushing view cache: {e}")

# Start background thread for cache flushing
import threading
cache_thread = threading.Thread(target=flush_view_cache, daemon=True)
cache_thread.start()

if __name__ == '__main__':
    logger.info("Starting Optimized StreamHub Application...")
    
    if init_mongodb():
        logger.info("MongoDB initialized successfully with connection pooling")
    else:
        logger.warning("MongoDB initialization failed")
    
    if APP_URL:
        set_webhook()
    else:
        logger.warning("APP_URL not set - webhook not configured")
    
    logger.info(f"Starting optimized Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
