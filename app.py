import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS 
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import logging
import pyotp  # For Google Authenticator 2FA
import qrcode  # For QR code generation
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
users_collection = None  # For 2FA secrets

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
        users_collection = db["admin_users"]  # Collection for 2FA
        
        # Create index for view counts
        content_collection.create_index([("_id", 1)])
        
        logger.info(f"MongoDB connected. Database: {db_name}")
        return True
    except Exception as e:
        logger.error(f"MongoDB initialization failed: {e}")
        return False

# --- 2. 2FA AUTHENTICATION SETUP ---
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

def generate_2fa_secret(username):
    """Generate a new 2FA secret for a user."""
    secret = pyotp.random_base32()
    return secret

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
            {"$set": {"totp_secret": secret, "updated_at": datetime.utcnow()}},
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
    return totp.verify(token)

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
# CORS is essential since the Vercel frontend is a different domain!
CORS(app) 

# Global state to track multi-step conversation
USER_STATE = {}

# FSM States (CRUD states remain the same)
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

# --- 5. CORE BOT FUNCTIONS (CRUD) ---
# (Helper functions like send_message, save_content, delete_content, update_content remain the same)
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
            "views": 0,  # Initialize view count
            "created_at": datetime.utcnow(),
            "last_viewed": datetime.utcnow()
        }
        result = content_collection.insert_one(document)
        logger.info(f"Content saved with ID: {result.inserted_id}")
        return True
    except Exception as e:
        logger.error(f"MongoDB Save Error: {e}")
        return False

def delete_content(content_id):
    """Deletes a content document by ID."""
    if content_collection is None: return False
    try:
        result = content_collection.delete_one({"_id": ObjectId(content_id)})
        return result.deleted_count > 0
    except Exception as e:
        logger.error(f"MongoDB Delete Error: {e}")
        return False

def update_content(content_id, update_fields):
    """Updates specific fields of a content document."""
    if content_collection is None: return False
    try:
        # Special handling for tags field to format them as a list
        if 'tags' in update_fields and isinstance(update_fields['tags'], str):
             update_fields['tags'] = [t.strip().lower() for t in update_fields['tags'].split(',') if t.strip()]

        clean_update = {k: v for k, v in update_fields.items() if v is not None}
        if not clean_update: return False
        
        result = content_collection.update_one(
            {"_id": ObjectId(content_id)},
            {"$set": clean_update}
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"MongoDB Update Error: {e}")
        return False

# --- 6. CONVERSATION HANDLERS (FSM) ---
# (FSM functions remain the same as before)

def start_new_upload(chat_id):
    """Starts the content upload process."""
    USER_STATE[chat_id] = {'state': STATE_WAITING_FOR_TYPE, 'content_data': {'links': []}}
    keyboard = {
        'inline_keyboard': [
            [{'text': '🎬 Video', 'callback_data': 'type_Video'}],
            [{'text': '📺 Web Series', 'callback_data': 'type_Series'}]
        ]
    }
    send_message(
        chat_id,
        "*Welcome to the Content Upload Bot!*\n\nPlease select the type of content:",
        reply_markup=keyboard
    )

def ask_for_title(chat_id):
    USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_TITLE
    send_message(chat_id, "✅ Content Type set.\n\nWhat is the *Title* of the Video/Series?")

def ask_for_thumbnail(chat_id):
    USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_THUMBNAIL
    send_message(chat_id, "✅ Title set.\n\nNext, please send the *public URL* for the Content Thumbnail Image:")

def ask_for_tags(chat_id):
    USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_TAGS
    send_message(chat_id, "✅ Thumbnail URL set.\n\nPlease enter comma-separated *Tags* (e.g., action, sci-fi, 2024). These are used for 'Similar Content' on the player page.")

def ask_for_link_title(chat_id):
    prompt = "Enter the name for the streaming link (e.g., 'Full Video' or 'S01E01 Pilot')."
    USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_LINK_TITLE
    send_message(chat_id, f"✅ Tags set.\n\n{prompt}")

def finish_upload(chat_id):
    content_data = USER_STATE[chat_id]['content_data']
    
    if not content_data.get('title') or not content_data.get('links'):
        send_message(chat_id, "❌ Error: Missing title or streaming links. Please start over with `/add`.")
        USER_STATE[chat_id]['state'] = STATE_START
        return

    if save_content(content_data):
        send_message(chat_id, f"🎉 *Success!* Content '{content_data['title']}' saved to database.")
        USER_STATE[chat_id]['state'] = STATE_START
        USER_STATE[chat_id]['content_data'] = {'links': []}
    else:
        send_message(chat_id, "❌ Error: Could not save to database. Please try again later.")

def fetch_and_send_content_list(chat_id, show_actions=False):
    """Fetches the latest content and sends a summary list with optional action buttons."""
    if content_collection is None:
        send_message(chat_id, "❌ Error: Database connection is unavailable.")
        return

    try:
        content_cursor = content_collection.find().sort("created_at", -1).limit(10)
        
        content_list = []
        for i, doc in enumerate(content_cursor):
            doc_id = str(doc['_id'])
            title = doc.get('title', 'Untitled')
            content_type = doc.get('type', 'Item')
            views = doc.get('views', 0)
            
            # Format a concise summary with view count
            summary = f"*{i+1}. {title}* (`{content_type}`) 👁️ {views} views"
            content_list.append(summary)

            if show_actions:
                keyboard = {
                    'inline_keyboard': [
                        [{'text': '✍️ Edit', 'callback_data': f'edit_start_{doc_id}'}],
                        [{'text': '🗑️ Delete', 'callback_data': f'delete_confirm_{doc_id}'}]
                    ]
                }
                send_message(chat_id, summary, reply_markup=keyboard)
            
        if not show_actions:
            if content_list:
                header = "📦 *Latest 10 Content Items:*\n\n"
                message = header + "\n\n".join(content_list)
            else:
                message = "📭 No content found. Use `/add` to upload one!"
            send_message(chat_id, message)

    except Exception as e:
        logger.error(f"Error viewing content: {e}")
        send_message(chat_id, "❌ An error occurred while fetching content.")

def handle_text_message(chat_id, text):
    """Handle text messages based on current state."""
    state = USER_STATE.get(chat_id, {}).get('state', STATE_START)
    content_data = USER_STATE.get(chat_id, {}).get('content_data', {})

    if text.startswith('/add'):
        start_new_upload(chat_id)
        return
    
    if text.startswith('/view'):
        fetch_and_send_content_list(chat_id, show_actions=False)
        return

    if text.startswith('/edit'):
        send_message(chat_id, "Select the content you wish to edit from the list below:")
        fetch_and_send_content_list(chat_id, show_actions=True)
        return

    # ... rest of the handle_text_message function remains the same
    # (Only showing the modified parts for brevity)

def handle_callback_query(chat_id, data):
    """Handle inline keyboard button presses."""
    # ... existing callback handling code remains the same

# --- 7. FLASK ROUTES ---

@app.route('/', methods=['GET'])
def index():
    """Simple status check with 2FA setup option."""
    return jsonify({
        "service": "StreamHub API/Bot", 
        "status": "online", 
        "message": "API is running. Frontend expected at Vercel deployment.",
        "api_endpoints": ["/api/content", "/api/content/similar/<tags>", "/api/auth/2fa-setup", "/api/track-view"]
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

# --- NEW 2FA AUTHENTICATION ROUTES ---

@app.route('/api/auth/2fa-setup', methods=['POST'])
def setup_2fa():
    """Setup 2FA for admin user."""
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')
        
        # Basic authentication
        if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
            return jsonify({"success": False, "error": "Invalid credentials"}), 401
        
        # Generate new secret
        secret = generate_2fa_secret(username)
        if not save_2fa_secret(username, secret):
            return jsonify({"success": False, "error": "Failed to save 2FA secret"}), 500
        
        # Generate QR code
        qr_code = generate_qr_code(secret, username)
        
        return jsonify({
            "success": True,
            "secret": secret,
            "qr_code": qr_code,
            "message": "Scan the QR code with Google Authenticator"
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
            return jsonify({"success": True, "message": "2FA verification successful"}), 200
        else:
            return jsonify({"success": False, "error": "Invalid 2FA token"}), 401
            
    except Exception as e:
        logger.error(f"2FA verification error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/auth/protected-route', methods=['GET'])
def protected_route():
    """Example protected route that requires 2FA."""
    auth_header = request.headers.get('Authorization', '')
    
    if not auth_header.startswith('Bearer '):
        return jsonify({"error": "Missing or invalid authorization header"}), 401
    
    token = auth_header[7:]  # Remove 'Bearer ' prefix
    
    if not verify_2fa_token(ADMIN_USERNAME, token):
        return jsonify({"error": "Invalid 2FA token"}), 401
    
    return jsonify({"message": "Access granted to protected resource"}), 200

# --- VIEW COUNT TRACKING ROUTE ---

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
                "message": "View count updated"
            }), 200
        else:
            return jsonify({"success": False, "error": "Failed to update view count"}), 500
            
    except Exception as e:
        logger.error(f"View tracking error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Main webhook handler for Telegram updates."""
    try:
        update = request.json
        logger.info(f"Received update: {json.dumps(update)[:200]}")
        
        if 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']
            text = message.get('text', '')
            handle_text_message(chat_id, text)
            
        elif 'callback_query' in update:
            query = update['callback_query']
            chat_id = query['message']['chat']['id']
            data = query['data']
            
            # Answer callback query to remove loading state
            callback_url = TELEGRAM_API + "answerCallbackQuery"
            requests.post(callback_url, json={'callback_query_id': query['id']}, timeout=5)
            
            handle_callback_query(chat_id, data)
        
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
        # Sort by creation date descending
        content_cursor = content_collection.find().sort("created_at", -1).limit(100)
        
        content_list = []
        for doc in content_cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            # Ensure views field exists
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
    """
    API endpoint to fetch content that shares at least one tag.
    """
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    # Clean and lowercase the input tags
    target_tags = [t.strip().lower() for t in tags.split(',') if t.strip()]

    if not target_tags:
        return jsonify({"success": True, "data": []}), 200

    try:
        # Find documents where the 'tags' array contains at least one of the target_tags
        query = {"tags": {"$in": target_tags}}
        
        # Limit results and sort by date descending
        content_cursor = content_collection.find(query).sort("created_at", -1).limit(10)
        
        content_list = []
        for doc in content_cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            # Ensure views field exists
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

# --- 8. APPLICATION STARTUP ---

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
    logger.info("Starting Telegram Bot Application...")
    
    if init_mongodb():
        logger.info("MongoDB initialized successfully")
    else:
        logger.warning("MongoDB initialization failed - bot will have limited functionality")
    
    if APP_URL:
        set_webhook()
    else:
        logger.warning("APP_URL not set - webhook not configured")
    
    logger.info(f"Starting Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
