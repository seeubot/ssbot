import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS 
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import logging
import pyotp
import qrcode
import io
import base64

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- 1. MONGODB SETUP ---
client = None
db = None
content_collection = None
users_collection = None

def init_mongodb():
    """Initialize MongoDB connection with error handling."""
    global client, db, content_collection, users_collection
    
    try:
        MONGODB_URI = os.environ.get("MONGODB_URI")
        if not MONGODB_URI:
            logger.error("MONGODB_URI environment variable is not set.")
            return False
        
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000
        )
        
        # Test connection
        client.admin.command('ping')
        
        db_name = os.environ.get("DB_NAME", "streamhub")
        collection_name = os.environ.get("COLLECTION_NAME", "content_items")
        
        db = client[db_name]
        content_collection = db[collection_name]
        users_collection = db["admin_users"]
        
        logger.info(f"MongoDB connected. Database: {db_name}")
        return True
    except Exception as e:
        logger.error(f"MongoDB initialization failed: {e}")
        return False

# --- 2. 2FA AUTHENTICATION SETUP ---
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

def generate_2fa_secret():
    """Generate a new 2FA secret."""
    return pyotp.random_base32()

def get_2fa_secret(username):
    """Retrieve 2FA secret from database."""
    if users_collection is None:
        return None
    
    user = users_collection.find_one({"username": username})
    return user.get('totp_secret') if user else None

def save_2fa_secret(username, secret):
    """Save 2FA secret to database."""
    if users_collection is None:
        return False
    
    try:
        users_collection.update_one(
            {"username": username},
            {
                "$set": {
                    "totp_secret": secret, 
                    "updated_at": datetime.utcnow(),
                    "is_2fa_enabled": True
                }
            },
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error saving 2FA secret: {e}")
        return False

def verify_2fa_token(username, token):
    """Verify 2FA token."""
    secret = get_2fa_secret(username)
    if not secret:
        return False
    
    totp = pyotp.TOTP(secret)
    return totp.verify(token, valid_window=1)  # Allow 30-second window

def generate_qr_code(secret, username):
    """Generate QR code for Google Authenticator."""
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(username, issuer_name="StreamHub Admin")
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(uri)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    
    img_str = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{img_str}"

# --- 3. VIEW COUNT FUNCTIONALITY ---
def increment_view_count(content_id):
    """Increment view count for a content item."""
    if content_collection is None:
        return False
    
    try:
        result = content_collection.update_one(
            {"_id": ObjectId(content_id)},
            {"$inc": {"views": 1}, "$set": {"last_viewed": datetime.utcnow()}}
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"Error incrementing view count: {e}")
        return False

def get_view_count(content_id):
    """Get view count for a content item."""
    if content_collection is None:
        return 0
    
    try:
        doc = content_collection.find_one({"_id": ObjectId(content_id)})
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

# --- 5. BOT FUNCTIONS (SIMPLIFIED FOR BREVITY) ---
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
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Message sent to chat_id {chat_id}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending message to {chat_id}: {e}")

def save_content(content_data):
    """Saves the complete content document to MongoDB."""
    if content_collection is None: return False
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

# --- 6. FLASK ROUTES ---

@app.route('/', methods=['GET'])
def index():
    """Simple status check."""
    return jsonify({
        "service": "StreamHub ", 
        "status": "online"
    }), 200

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    try:
        if content_collection is not None:
            client.admin.command('ping')
            return jsonify({"status": "healthy", "database": "connected"}), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
    
    return jsonify({"status": "unhealthy", "database": "disconnected"}), 503

# --- 2FA AUTHENTICATION ROUTES ---

@app.route('/api/auth/2fa-setup', methods=['GET', 'POST'])
def setup_2fa():
    """Setup 2FA for admin user."""
    try:
        if request.method == 'GET':
            return jsonify({
                "message": "Send POST request with JSON: {'username': 'admin', 'password': 'your_password'}",
                "note": "This will generate a new QR code for Google Authenticator"
            }), 200
        
        data = request.json
        username = data.get('username')
        password = data.get('password')
        
        # Basic authentication
        if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
            return jsonify({"success": False, "error": "Invalid credentials"}), 401
        
        # Generate new secret
        secret = generate_2fa_secret()
        if not save_2fa_secret(username, secret):
            return jsonify({"success": False, "error": "Failed to save 2FA secret"}), 500
        
        # Generate QR code
        qr_code = generate_qr_code(secret, username)
        
        return jsonify({
            "success": True,
            "secret": secret,  # Show secret for manual entry
            "qr_code": qr_code,
            "message": "Scan the QR code with Google Authenticator app or manually enter the secret",
            "manual_entry": f"Secret: {secret}",
            "instructions": [
                "1. Install Google Authenticator on your phone",
                "2. Scan the QR code or manually enter the secret",
                "3. Use the generated 6-digit codes to verify"
            ]
        }), 200
        
    except Exception as e:
        logger.error(f"2FA setup error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/auth/verify-2fa', methods=['POST'])
def verify_2fa():
    """Verify 2FA token."""
    try:
        data = request.json
        username = data.get('username')
        token = data.get('token')
        
        if not username or not token:
            return jsonify({"success": False, "error": "Username and token required"}), 400
        
        if verify_2fa_token(username, token):
            return jsonify({
                "success": True, 
                "message": "2FA verification successful!",
                "access_granted": True
            }), 200
        else:
            return jsonify({
                "success": False, 
                "error": "Invalid 2FA token",
                "tip": "Make sure your phone time is synchronized"
            }), 401
            
    except Exception as e:
        logger.error(f"2FA verification error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/auth/status', methods=['GET'])
def get_2fa_status():
    """Check if 2FA is setup for user."""
    try:
        username = request.args.get('username', ADMIN_USERNAME)
        secret = get_2fa_secret(username)
        
        return jsonify({
            "success": True,
            "username": username,
            "is_2fa_enabled": secret is not None,
            "has_secret": bool(secret)
        }), 200
    except Exception as e:
        logger.error(f"2FA status error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/auth/protected-test', methods=['GET'])
def protected_test():
    """Test route that requires 2FA."""
    auth_header = request.headers.get('Authorization', '')
    
    if not auth_header.startswith('Bearer '):
        return jsonify({"error": "Missing authorization header. Use: Authorization: Bearer <2FA_TOKEN>"}), 401
    
    token = auth_header[7:]  # Remove 'Bearer ' prefix
    
    if not verify_2fa_token(ADMIN_USERNAME, token):
        return jsonify({"error": "Invalid 2FA token"}), 401
    
    return jsonify({
        "message": "🔐 Access granted to protected resource!",
        "user": ADMIN_USERNAME,
        "timestamp": datetime.utcnow().isoformat()
    }), 200

# --- VIEW COUNT TRACKING ---

@app.route('/api/track-view', methods=['POST'])
def track_view():
    """Track when a content item is viewed."""
    try:
        data = request.json
        content_id = data.get('content_id')
        
        if not content_id:
            return jsonify({"success": False, "error": "Content ID required"}), 400
        
        if increment_view_count(content_id):
            current_views = get_view_count(content_id)
            return jsonify({
                "success": True, 
                "views": current_views,
                "content_id": content_id,
                "message": "View count updated"
            }), 200
        else:
            return jsonify({"success": False, "error": "Failed to update view count"}), 500
            
    except Exception as e:
        logger.error(f"View tracking error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# --- EXISTING CONTENT ROUTES (SIMPLIFIED) ---

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Main webhook handler for Telegram updates."""
    try:
        update = request.json
        
        if 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']
            text = message.get('text', '')
            
            if text == '/start':
                send_message(chat_id, "Welcome to StreamHub Bot! Use /add to upload content.")
        
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/content', methods=['GET'])
def get_content():
    """REST API endpoint for the frontend to fetch all content."""
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        content_cursor = content_collection.find().sort("created_at", -1).limit(100)
        
        content_list = []
        for doc in content_cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            if 'views' not in doc:
                doc['views'] = 0
            content_list.append(doc)
            
        return jsonify({
            "success": True,
            "count": len(content_list),
            "data": content_list
        }), 200
    except Exception as e:
        logger.error(f"API Fetch Error: {e}")
        return jsonify({"success": False, "error": "Failed to retrieve content."}), 500

@app.route('/api/content/similar/<tags>', methods=['GET'])
def get_similar_content(tags):
    """API endpoint to fetch content that shares at least one tag."""
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    target_tags = [t.strip().lower() for t in tags.split(',') if t.strip()]

    if not target_tags:
        return jsonify({"success": True, "data": []}), 200

    try:
        query = {"tags": {"$in": target_tags}}
        content_cursor = content_collection.find(query).sort("created_at", -1).limit(10)
        
        content_list = []
        for doc in content_cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            if 'views' not in doc:
                doc['views'] = 0
            content_list.append(doc)
            
        return jsonify({
            "success": True,
            "count": len(content_list),
            "data": content_list
        }), 200
    except Exception as e:
        logger.error(f"API Similar Fetch Error: {e}")
        return jsonify({"success": False, "error": "Failed to retrieve similar content."}), 500

# --- APPLICATION STARTUP ---

def set_webhook():
    """Set the webhook URL for Telegram."""
    if not APP_URL:
        logger.warning("APP_URL not set. Skipping webhook setup.")
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
def before_first_request():
    """Initialize connections before handling requests."""
    if content_collection is None:
        init_mongodb()

if __name__ == '__main__':
    logger.info("Starting StreamHub Application...")
    
    if init_mongodb():
        logger.info("MongoDB initialized successfully")
    else:
        logger.warning("MongoDB initialization failed")
    
    if APP_URL:
        set_webhook()
    else:
        logger.warning("APP_URL not set - webhook not configured")
    
    logger.info(f"Starting Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
