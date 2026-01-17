import os
import json
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS 
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime
import logging
import threading
from cachetools import TTLCache
from functools import wraps
import time
import atexit
import signal
from werkzeug.utils import secure_filename

# --- CONSTANTS & CONFIGURATION ---
ADMIN_TELEGRAM_ID = 1352497419
GROUP_TELEGRAM_ID = -1002541647242  # DiskWala Channel
TELUGU_GROUP_ID = -1003551789476  # Telugu Channel
CONTENT_FORWARD_CHANNEL_ID = -1002776780769  # Video Files Channel
PRODUCT_NAME = "Adult-Hub"

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- MONGODB SETUP ---
MONGODB_URI = "mongodb+srv://naya:naya@naya.fk9em5f.mongodb.net/?appName=naya"
client = None
db = None
content_collection = None
counter_collection = None
broadcast_collection = None
requests_collection = None  # For video requests
post_counter_collection = None  # For post numbering

def init_mongodb():
    global client, db, content_collection, counter_collection, broadcast_collection, requests_collection, post_counter_collection
    
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
        client.admin.command('ping')
        
        db_name = os.environ.get("DB_NAME", "streamhub")
        collection_name = os.environ.get("COLLECTION_NAME", "diskwala_posts")
        
        db = client[db_name]
        content_collection = db[collection_name]
        counter_collection = db["counters"]
        broadcast_collection = db["broadcasts"]
        requests_collection = db["video_requests"]
        post_counter_collection = db["post_counters"]  # For channel post numbering
        
        # Create indexes
        content_collection.create_index([("created_at", -1)])
        content_collection.create_index([("tags", 1)])
        content_collection.create_index([("channel", 1)])
        content_collection.create_index([("post_number", -1)])
        broadcast_collection.create_index([("created_at", -1)])
        requests_collection.create_index([("status", 1)])
        requests_collection.create_index([("views", -1)])
        requests_collection.create_index([("createdAt", -1)])
        requests_collection.create_index([("user_id", 1)])
        
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

def get_next_post_number(channel_name):
    """Get next post number for a specific channel"""
    try:
        result = post_counter_collection.find_one_and_update(
            {'_id': channel_name},
            {'$inc': {'post_number': 1}},
            upsert=True,
            return_document=True
        )
        return result['post_number']
    except:
        return 1

# --- AUTHENTICATION ---
def is_admin(user_id):
    """Check if user is admin"""
    return user_id == ADMIN_TELEGRAM_ID

# --- CACHING ---
content_cache = TTLCache(maxsize=50, ttl=30)  # Reduced cache size

# --- VIEW TRACKING ---
view_cache = {}
view_lock = threading.RLock()

def track_view(content_id):
    with view_lock:
        view_cache[content_id] = view_cache.get(content_id, 0) + 1

# --- TELEGRAM SETUP ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
APP_URL = os.environ.get("APP_URL")
PORT = int(os.environ.get("PORT", 8000))
BOT_USERNAME = None  # Will be set after bot info fetch

# --- FILE UPLOAD CONFIGURATION ---
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp4', 'avi', 'mov', 'mkv'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/" if BOT_TOKEN else None

app = Flask(__name__)
CORS(app)

USER_STATE = {}

# Available channels for broadcast
BROADCAST_CHANNELS = {
    'diskwala': {'id': GROUP_TELEGRAM_ID, 'name': 'DiskWala Channel'},
    'telugu': {'id': TELUGU_GROUP_ID, 'name': 'Telugu Channel'},
    'video_files': {'id': CONTENT_FORWARD_CHANNEL_ID, 'name': 'Video Files Channel'}
}

# --- KEYBOARDS ---
# Main keyboard for admin
ADMIN_MAIN_KEYBOARD = {
    'keyboard': [
        [{'text': 'DiskWala Posts'}, {'text': 'Telugu Posts'}],
        [{'text': 'Video Files'}, {'text': '📢 Broadcast'}],
        [{'text': '📥 Video Requests'}, {'text': '📊 Stats'}],
        [{'text': 'Cancel'}]
    ],
    'resize_keyboard': True
}

# User keyboard (for non-admin users)
USER_MAIN_KEYBOARD = {
    'keyboard': [
        [{'text': '📥 Request Video'}],
        [{'text': '📊 Popular Requests'}, {'text': '🆕 My Requests'}],
        [{'text': '❌ Cancel'}]
    ],
    'resize_keyboard': True
}

CHANNEL_MODE_KEYBOARD = {
    'keyboard': [
        [{'text': 'Single Post'}, {'text': 'Forward Multiple'}],
        [{'text': 'Back to Menu'}]
    ],
    'resize_keyboard': True
}

BROADCAST_KEYBOARD = {
    'keyboard': [
        [{'text': '📺 DiskWala'}, {'text': '🇮🇳 Telugu'}],
        [{'text': '🎬 Video Files'}, {'text': '📡 All Channels'}],
        [{'text': 'Back to Menu'}]
    ],
    'resize_keyboard': True
}

REQUESTS_KEYBOARD = {
    'keyboard': [
        [{'text': '📋 Pending Requests'}, {'text': '📊 All Requests'}],
        [{'text': 'Back to Menu'}]
    ],
    'resize_keyboard': True
}

REQUEST_MEDIA_KEYBOARD = {
    'keyboard': [
        [{'text': '📷 Send Photo'}, {'text': '🎥 Send Video'}],
        [{'text': '✏️ Text Only'}, {'text': '❌ Cancel'}]
    ],
    'resize_keyboard': True
}

# --- TELEGRAM FUNCTIONS ---

def send_telegram(method, payload):
    if not TELEGRAM_API:
        return False
    
    clean = {k: v for k, v in payload.items() if v is not None}
    
    try:
        response = requests.post(TELEGRAM_API + method, json=clean, timeout=5)  # Reduced timeout
        response.raise_for_status()
        result = response.json()
        return result.get('result') if result.get('ok') else False
            
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

def send_message(chat_id, text, keyboard=None, parse_mode=None, disable_web_page_preview=True):
    payload = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': disable_web_page_preview}
    if keyboard:
        payload['reply_markup'] = json.dumps(keyboard)
    if parse_mode:
        payload['parse_mode'] = parse_mode
    return send_telegram('sendMessage', payload)

def send_media_post(title, media_id, media_type, links, channel_id, channel_name, post_number=None):
    """Send media post to specified channel with post number"""
    if not TELEGRAM_API:
        return False

    # Get bot info for request URL
    bot_url = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "Contact Admin"
    
    link_text = "\n".join([f"🔗 {link.get('episode_title', 'Link')}: {link['url']}" for link in links])
    
    # Add post number to caption if available
    post_number_text = f"#{post_number} " if post_number else ""
    request_button = f"\n\n📥 Request Video: {bot_url}?start=request"
    
    caption = f"{post_number_text}🔥 {title} 🔥\n\n{link_text}{request_button}\n━━━━━━━━━━━━━━━━━\nPowered by {PRODUCT_NAME}"
    
    method = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
    key = 'photo' if media_type == 'photo' else 'video'
    
    return send_telegram(method, {'chat_id': channel_id, key: media_id, 'caption': caption})

def forward_message_to_admin(message, caption=None):
    """Forward user message to admin for review"""
    try:
        payload = {
            'chat_id': ADMIN_TELEGRAM_ID,
            'from_chat_id': message['chat']['id'],
            'message_id': message['message_id']
        }
        if caption:
            payload['caption'] = caption
            
        return send_telegram('forwardMessage', payload)
    except Exception as e:
        logger.error(f"Forward to admin error: {e}")
        return False

def send_direct_message(chat_id, text, reply_markup=None):
    """Send direct message to user"""
    payload = {'chat_id': chat_id, 'text': text}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    return send_telegram('sendMessage', payload)

# --- VIDEO REQUEST FUNCTIONS ---

def create_video_request(user_id, description, media_url=None, media_type=None, message_id=None):
    """Create a new video request from user"""
    try:
        request_id = get_next_sequence('video_request_id')
        
        request_doc = {
            'request_id': f"REQ{request_id:06d}",
            'user_id': user_id,
            'description': description,
            'mediaUrl': media_url,
            'mediaType': media_type,
            'telegram_message_id': message_id,
            'status': 'pending',
            'videoResult': None,
            'views': 0,
            'createdAt': datetime.utcnow(),
            'updatedAt': datetime.utcnow()
        }
        
        if not requests_collection:
            return None
            
        result = requests_collection.insert_one(request_doc)
        request_doc['_id'] = str(result.inserted_id)
        
        # Notify admin
        notify_admin_new_request(request_doc)
        
        return request_doc
        
    except Exception as e:
        logger.error(f"Create request error: {e}")
        return None

def notify_admin_new_request(request_doc):
    """Notify admin about new video request"""
    try:
        user_id = request_doc['user_id']
        request_id = request_doc['request_id']
        description = request_doc.get('description', 'No description')
        
        message = f"""
🆕 New Video Request

📝 Description: {description}
🆔 Request ID: {request_id}
👤 User ID: {user_id}
📅 Date: {request_doc['createdAt'].strftime('%Y-%m-%d %H:%M')}

Use /reply {request_id} <video_url> to send to user
Or /complete {request_id} <video_url> to complete
        """
        
        # Send to admin
        send_message(ADMIN_TELEGRAM_ID, message)
        
        # If there's a media message, forward it
        if request_doc.get('telegram_message_id'):
            forward_message_to_admin({
                'chat_id': request_doc.get('user_id'),
                'message_id': request_doc['telegram_message_id']
            }, caption=f"Request #{request_id}")
            
    except Exception as e:
        logger.error(f"Notify admin error: {e}")

def handle_user_request(chat_id, user_id):
    """Handle user's video request initiation"""
    USER_STATE[chat_id] = {
        'step': 'request_description',
        'user_id': user_id,
        'timestamp': time.time()
    }
    send_message(
        chat_id,
        "📥 Video Request\n\n"
        "Please describe the video you're looking for:\n"
        "(You can include details like actress name, scene description, etc.)\n\n"
        "Type 'Cancel' to abort.",
        REQUEST_MEDIA_KEYBOARD
    )

def handle_request_media(chat_id, user_id, text):
    """Handle request media type selection"""
    if text == '📷 Send Photo':
        USER_STATE[chat_id] = {
            'step': 'request_photo',
            'user_id': user_id,
            'timestamp': time.time()
        }
        send_message(chat_id, "Please send a photo of the scene/actress you're looking for:")
        
    elif text == '🎥 Send Video':
        USER_STATE[chat_id] = {
            'step': 'request_video',
            'user_id': user_id,
            'timestamp': time.time()
        }
        send_message(chat_id, "Please send a video clip of what you're looking for:")
        
    elif text == '✏️ Text Only':
        USER_STATE[chat_id] = {
            'step': 'request_description_text',
            'user_id': user_id,
            'timestamp': time.time()
        }
        send_message(chat_id, "Please describe what you're looking for:")

def handle_reply_command(chat_id, text):
    """Handle /reply command to send video directly to user"""
    try:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            send_message(chat_id, "Usage: /reply <request_id> <video_url>")
            return
        
        request_id = parts[1]
        video_url = parts[2]
        
        # Find request
        request_doc = requests_collection.find_one({'request_id': request_id})
        if not request_doc:
            send_message(chat_id, f"❌ Request {request_id} not found")
            return
        
        user_id = request_doc['user_id']
        
        # Send video to user
        user_message = f"""
✅ Your Request Completed!

Your request #{request_id} has been fulfilled!
🔗 Video Link: {video_url}

Thank you for using our service!
        """
        
        send_direct_message(user_id, user_message)
        
        # Update request status
        requests_collection.update_one(
            {'request_id': request_id},
            {
                '$set': {
                    'status': 'completed',
                    'videoResult': video_url,
                    'updatedAt': datetime.utcnow()
                }
            }
        )
        
        send_message(chat_id, f"✅ Video sent to user for request {request_id}")
            
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_complete_command(chat_id, text):
    """Handle /complete command from admin for video requests"""
    try:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            send_message(chat_id, "Usage: /complete <request_id> <video_url>")
            return
        
        request_id = parts[1]
        video_url = parts[2]
        
        # Update request
        result = requests_collection.update_one(
            {'request_id': request_id},
            {
                '$set': {
                    'status': 'completed',
                    'videoResult': video_url,
                    'updatedAt': datetime.utcnow()
                }
            }
        )
        
        if result.modified_count > 0:
            send_message(chat_id, f"✅ Request {request_id} marked as completed!")
        else:
            send_message(chat_id, f"❌ Request {request_id} not found")
            
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_requests_command(chat_id):
    """Handle /requests command to show pending requests"""
    try:
        pending = list(requests_collection.find({'status': 'pending'}).sort('createdAt', -1).limit(10))
        
        if not pending:
            send_message(chat_id, "No pending requests")
            return
        
        message = "📋 Pending Requests:\n\n"
        for req in pending:
            message += f"🆔 {req['request_id']}\n"
            message += f"👤 User: {req['user_id']}\n"
            message += f"📝 {req.get('description', 'No description')[:50]}\n"
            message += f"📅 {req['createdAt'].strftime('%Y-%m-%d %H:%M')}\n\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_user_requests_command(chat_id, user_id):
    """Show user's own requests"""
    try:
        user_requests = list(requests_collection.find({'user_id': user_id}).sort('createdAt', -1).limit(10))
        
        if not user_requests:
            send_message(chat_id, "You haven't made any requests yet.")
            return
        
        message = "📋 Your Requests:\n\n"
        for req in user_requests:
            status_emoji = "✅" if req['status'] == 'completed' else "⏳"
            message += f"{status_emoji} {req['request_id']} - {req['status']}\n"
            message += f"📝 {req.get('description', 'No description')[:40]}...\n"
            if req['status'] == 'completed' and req.get('videoResult'):
                message += f"🔗 {req['videoResult']}\n"
            message += "\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_popular_requests_command(chat_id):
    """Show popular completed requests"""
    try:
        popular = list(requests_collection.find(
            {'status': 'completed', 'videoResult': {'$ne': None}}
        ).sort('views', -1).limit(10))
        
        if not popular:
            send_message(chat_id, "No completed requests yet.")
            return
        
        message = "🔥 Popular Requests:\n\n"
        for req in popular:
            message += f"🆔 {req['request_id']}\n"
            message += f"📝 {req.get('description', 'No description')[:50]}\n"
            message += f"👁️ {req.get('views', 0)} views\n"
            message += f"🔗 {req.get('videoResult', 'No link')}\n\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

# --- URL VALIDATION ---
def validate_url(url):
    """Validate URL format"""
    from urllib.parse import urlparse
    try:
        result = urlparse(url)
        return all([result.scheme in ['http', 'https'], result.netloc])
    except:
        return False

# --- CONTENT MANAGEMENT ---

def save_content(data):
    if not content_collection:
        return False
    
    try:
        tags = [t.strip().lower() for t in data.get('tags', '').split(',') if t.strip()]
        
        # Get post number for channel
        channel = data.get('channel', 'diskwala')
        post_number = get_next_post_number(channel)
        
        doc = {
            "title": data.get('title'),
            "type": data.get('type'),
            "channel": channel,
            "post_number": post_number,
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

# --- STATE CLEANUP ---
USER_STATE_TIMEOUT = 1800  # 30 minutes

def cleanup_old_states():
    """Clean up expired user states"""
    while True:
        threading.Event().wait(300)
        current_time = time.time()
        try:
            expired = [
                chat_id for chat_id, state in USER_STATE.items()
                if current_time - state.get('timestamp', 0) > USER_STATE_TIMEOUT
            ]
            for chat_id in expired:
                USER_STATE.pop(chat_id, None)
        except Exception as e:
            logger.error(f"State cleanup error: {e}")

# --- TELEGRAM UPDATE HANDLER ---

def process_update(update):
    try:
        message = update.get('message')
        if not message:
            return
            
        chat_id = message['chat']['id']
        text = message.get('text', '').strip()
        user_id = message['from']['id']
        
        # Check if user is admin
        admin = is_admin(user_id)
        
        # Handle start command with parameters
        if text.startswith('/start'):
            params = text.split()
            if len(params) > 1 and params[1] == 'request':
                # User clicked request button from channel
                handle_user_request(chat_id, user_id)
                return
            
            # Regular start command
            if admin:
                send_message(
                    chat_id,
                    f"🚀 {PRODUCT_NAME} Admin Bot\n\n"
                    f"📁 DiskWala Posts - Post to DiskWala channel\n"
                    f"🇮🇳 Telugu Posts - Post to Telugu channel\n"
                    f"🎬 Video Files - Forward files to video channel\n"
                    f"📢 Broadcast - Send message to channels\n"
                    f"📥 Video Requests - Manage video requests\n"
                    f"📊 Stats - View system statistics\n\n"
                    f"Choose an option:",
                    ADMIN_MAIN_KEYBOARD
                )
            else:
                send_message(
                    chat_id,
                    f"👋 Welcome to {PRODUCT_NAME}!\n\n"
                    f"Here you can:\n"
                    f"📥 Request Video - Request specific videos\n"
                    f"📊 Popular Requests - View popular fulfilled requests\n"
                    f"🆕 My Requests - Check your request status\n\n"
                    f"Choose an option:",
                    USER_MAIN_KEYBOARD
                )
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            return
        
        # --- MAIN MENU HANDLING ---
        if text in ['Back to Menu', 'Cancel', '❌ Cancel']:
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            if admin:
                send_message(chat_id, "Back to main menu:", ADMIN_MAIN_KEYBOARD)
            else:
                send_message(chat_id, "Back to main menu:", USER_MAIN_KEYBOARD)
            return
        
        # --- ADMIN COMMANDS ---
        if admin:
            # Handle admin commands
            if text.startswith('/reply'):
                handle_reply_command(chat_id, text)
                return
            elif text.startswith('/complete'):
                handle_complete_command(chat_id, text)
                return
            elif text.startswith('/requests'):
                handle_requests_command(chat_id)
                return
            
            # Admin menu options
            state = USER_STATE.get(chat_id, {'step': 'main', 'timestamp': time.time()})
            
            # Video requests menu
            if text == '📥 Video Requests':
                USER_STATE[chat_id] = {'step': 'requests_menu', 'timestamp': time.time()}
                send_message(
                    chat_id,
                    "📥 Video Requests Management\n\n"
                    "📋 Pending Requests - View pending requests\n"
                    "📊 All Requests - View all requests\n\n"
                    "Commands:\n"
                    "/reply <id> <url> - Send video to user\n"
                    "/complete <id> <url> - Mark as completed",
                    REQUESTS_KEYBOARD
                )
                return
            
            # Stats
            elif text == '📊 Stats':
                handle_stats_command(chat_id)
                return
            
            # Rest of admin flow remains same...
            # [Previous admin flow code here - shortened for brevity]
            
        # --- USER FLOW ---
        else:
            state = USER_STATE.get(chat_id, {'step': 'main', 'timestamp': time.time()})
            
            # User menu options
            if text == '📥 Request Video':
                handle_user_request(chat_id, user_id)
                return
            
            elif text == '📊 Popular Requests':
                handle_popular_requests_command(chat_id)
                return
            
            elif text == '🆕 My Requests':
                handle_user_requests_command(chat_id, user_id)
                return
            
            # User request flow
            elif state['step'] == 'request_description':
                if text in ['📷 Send Photo', '🎥 Send Video', '✏️ Text Only']:
                    handle_request_media(chat_id, user_id, text)
                else:
                    send_message(chat_id, "Please select an option:", REQUEST_MEDIA_KEYBOARD)
                return
            
            elif state['step'] in ['request_photo', 'request_video', 'request_description_text']:
                handle_user_request_submission(chat_id, user_id, state, message)
                return
            
            # Handle user sending media directly
            elif 'photo' in message or 'video' in message:
                # User sent media without going through menu
                if chat_id not in USER_STATE:
                    send_message(chat_id, "Please use 'Request Video' button first.")
                    return
            
            # Unknown command for user
            else:
                send_message(chat_id, "Please select an option from the menu:", USER_MAIN_KEYBOARD)
        
    except Exception as e:
        logger.error(f"Process error: {e}", exc_info=True)
        try:
            if chat_id:
                send_message(chat_id, "🚨 Error occurred. Please try again.", USER_MAIN_KEYBOARD)
        except:
            pass

def handle_user_request_submission(chat_id, user_id, state, message):
    """Handle user's request submission"""
    step = state['step']
    
    if step == 'request_photo' and 'photo' not in message:
        send_message(chat_id, "Please send a photo or type 'Cancel'")
        return
    
    if step == 'request_video' and 'video' not in message:
        send_message(chat_id, "Please send a video or type 'Cancel'")
        return
    
    if step == 'request_description_text' and not message.get('text'):
        send_message(chat_id, "Please describe what you're looking for or type 'Cancel'")
        return
    
    # Get description from text or caption
    description = ""
    if step == 'request_description_text':
        description = message.get('text', '')
    else:
        description = message.get('caption', '')
        if not description:
            USER_STATE[chat_id] = {
                'step': 'request_description_followup',
                'user_id': user_id,
                'media_message': message,
                'timestamp': time.time()
            }
            send_message(chat_id, "Please describe what you're looking for in this media:")
            return
    
    # Create request
    media_url = None
    media_type = None
    
    if 'photo' in message:
        media_id = message['photo'][-1]['file_id']
        media_type = 'photo'
        media_url = f"telegram_file:{media_id}"
    elif 'video' in message:
        media_id = message['video']['file_id']
        media_type = 'video'
        media_url = f"telegram_file:{media_id}"
    
    request_doc = create_video_request(
        user_id=user_id,
        description=description,
        media_url=media_url,
        media_type=media_type,
        message_id=message.get('message_id')
    )
    
    if request_doc:
        send_message(
            chat_id,
            f"✅ Request Submitted!\n\n"
            f"🆔 Request ID: {request_doc['request_id']}\n"
            f"📝 Description: {description[:100]}...\n\n"
            f"We'll notify you when it's fulfilled!",
            USER_MAIN_KEYBOARD
        )
    else:
        send_message(chat_id, "❌ Failed to submit request. Please try again.", USER_MAIN_KEYBOARD)
    
    USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}

def handle_stats_command(chat_id):
    """Handle stats command"""
    try:
        stats = {
            "total_content": 0,
            "total_requests": 0,
            "pending_requests": 0,
            "completed_requests": 0,
            "channels": {}
        }
        
        if content_collection:
            stats["total_content"] = content_collection.count_documents({})
            for channel in BROADCAST_CHANNELS:
                count = content_collection.count_documents({"channel": channel})
                stats["channels"][channel] = count
        
        if requests_collection:
            stats["total_requests"] = requests_collection.count_documents({})
            stats["pending_requests"] = requests_collection.count_documents({"status": "pending"})
            stats["completed_requests"] = requests_collection.count_documents({"status": "completed"})
        
        message = "📊 System Statistics\n\n"
        message += f"📁 Total Posts: {stats['total_content']}\n"
        message += f"📥 Total Requests: {stats['total_requests']}\n"
        message += f"⏳ Pending Requests: {stats['pending_requests']}\n"
        message += f"✅ Completed Requests: {stats['completed_requests']}\n\n"
        
        message += "📈 Channel Posts:\n"
        for channel, count in stats['channels'].items():
            channel_name = BROADCAST_CHANNELS[channel]['name']
            message += f"  • {channel_name}: {count}\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error getting stats: {str(e)}")

# --- FLASK ROUTES (Minimal) ---

@app.route('/webhook', methods=['POST'])
def webhook():
    """Telegram webhook handler"""
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

@app.route('/health', methods=['GET'])
def health():
    status = {"status": "healthy", "service": PRODUCT_NAME}
    return jsonify(status), 200

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

@app.route('/api/request/<request_id>/view', methods=['POST'])
def increment_view(request_id):
    """Increment view count for a request"""
    try:
        if not requests_collection:
            return jsonify({'error': 'Database not configured'}), 503
            
        result = requests_collection.update_one(
            {'request_id': request_id},
            {'$inc': {'views': 1}}
        )
        
        if result.modified_count > 0:
            return jsonify({'success': True}), 200
        else:
            return jsonify({'error': 'Request not found'}), 404
            
    except Exception as e:
        logger.error(f"Increment view error: {e}")
        return jsonify({'error': str(e)}), 500

# --- BACKGROUND TASKS ---

def flush_views_once():
    """Flush all pending views to DB"""
    try:
        with view_lock:
            if not view_cache or not content_collection:
                return
            
            from pymongo import UpdateOne
            
            bulk_ops = [
                UpdateOne(
                    {"_id": ObjectId(cid)},
                    {"$inc": {"views": count}}
                ) for cid, count in view_cache.items()
                if count > 0 and ObjectId.is_valid(cid)
            ]
            
            if bulk_ops:
                content_collection.bulk_write(bulk_ops, ordered=False)
            
            view_cache.clear()
    except Exception as e:
        logger.error(f"Flush error: {e}")

def flush_views_loop():
    """Periodic view flush loop"""
    while True:
        threading.Event().wait(30)
        flush_views_once()

def get_bot_info():
    """Get bot username"""
    global BOT_USERNAME
    try:
        result = send_telegram('getMe', {})
        if result:
            BOT_USERNAME = result.get('username')
            logger.info(f"Bot username: @{BOT_USERNAME}")
    except Exception as e:
        logger.error(f"Get bot info error: {e}")

def set_webhook():
    if not APP_URL or not BOT_TOKEN:
        return False
    
    webhook_url = f"{APP_URL.rstrip('/')}/webhook"
    return send_telegram('setWebhook', {
        'url': webhook_url,
        'max_connections': 5,
        'drop_pending_updates': True
    })

# --- STARTUP ---

if __name__ == '__main__':
    logger.info(f"Starting {PRODUCT_NAME}...")
    
    init_mongodb()
    
    # Get bot info
    get_bot_info()
    
    # Start background tasks
    threading.Thread(target=flush_views_loop, daemon=True).start()
    threading.Thread(target=cleanup_old_states, daemon=True).start()
    
    # Register shutdown handler
    atexit.register(flush_views_once)
    signal.signal(signal.SIGTERM, lambda s, f: flush_views_once())
    
    if APP_URL and BOT_TOKEN:
        if set_webhook():
            logger.info("Webhook set successfully")
        else:
            logger.error("Webhook setup failed")
    
    logger.info(f"Starting server on port {PORT}")
    logger.info(f"Bot username: @{BOT_USERNAME if BOT_USERNAME else 'Not set'}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
