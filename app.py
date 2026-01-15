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
WEBAPP_URL = "https://drs-kappa.vercel.app/"

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

def init_mongodb():
    global client, db, content_collection, counter_collection, broadcast_collection, requests_collection
    
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
        client.admin.command('ping')
        
        db_name = os.environ.get("DB_NAME", "streamhub")
        collection_name = os.environ.get("COLLECTION_NAME", "diskwala_posts")
        
        db = client[db_name]
        content_collection = db[collection_name]
        counter_collection = db["counters"]
        broadcast_collection = db["broadcasts"]
        requests_collection = db["video_requests"]  # Collection for video requests
        
        # Create indexes
        content_collection.create_index([("created_at", -1)])
        content_collection.create_index([("tags", 1)])
        content_collection.create_index([("channel", 1)])
        broadcast_collection.create_index([("created_at", -1)])
        requests_collection.create_index([("status", 1)])
        requests_collection.create_index([("views", -1)])
        requests_collection.create_index([("createdAt", -1)])
        
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
view_lock = threading.RLock()

def track_view(content_id):
    with view_lock:
        view_cache[content_id] = view_cache.get(content_id, 0) + 1

# --- TELEGRAM SETUP ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
APP_URL = os.environ.get("APP_URL")
PORT = int(os.environ.get("PORT", 8000))

# --- FILE UPLOAD CONFIGURATION ---
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp4', 'avi', 'mov', 'mkv'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def is_image(filename):
    return filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'gif'}

def is_video(filename):
    return filename.rsplit('.', 1)[1].lower() in {'mp4', 'avi', 'mov', 'mkv'}

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

# Updated keyboards with WebApp and Broadcast
MAIN_KEYBOARD = {
    'keyboard': [
        [{'text': 'DiskWala Posts'}, {'text': 'Telugu Posts'}],
        [{'text': 'Video Files'}, {'text': '📢 Broadcast'}],
        [{'text': '📥 Video Requests'}, {'text': '🌐 Open WebApp', 'web_app': {'url': WEBAPP_URL}}],
        [{'text': 'Cancel'}]
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

def send_message(chat_id, text, keyboard=None, parse_mode=None):
    payload = {'chat_id': chat_id, 'text': text}
    if keyboard:
        payload['reply_markup'] = json.dumps(keyboard)
    if parse_mode:
        payload['parse_mode'] = parse_mode
    return send_telegram('sendMessage', payload)

def send_media_post(title, media_id, media_type, links, channel_id, channel_name):
    """Send media post to specified channel"""
    if not TELEGRAM_API:
        return False

    link_text = "\n".join([f"🔗 {link.get('episode_title', 'Link')}: {link['url']}" for link in links])
    caption = f"🔥 {title} 🔥\n\n{link_text}\n━━━━━━━━━━━━━━━━━\nPowered by {PRODUCT_NAME}"
    
    method = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
    key = 'photo' if media_type == 'photo' else 'video'
    
    return send_telegram(method, {'chat_id': channel_id, key: media_id, 'caption': caption})

def broadcast_message(text, channels, media_id=None, media_type=None):
    """Broadcast message to selected channels"""
    results = {'success': [], 'failed': []}
    
    for channel_key in channels:
        if channel_key not in BROADCAST_CHANNELS:
            continue
        
        channel_info = BROADCAST_CHANNELS[channel_key]
        channel_id = channel_info['id']
        channel_name = channel_info['name']
        
        try:
            if media_id and media_type:
                method = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
                key = 'photo' if media_type == 'photo' else 'video'
                result = send_telegram(method, {
                    'chat_id': channel_id,
                    key: media_id,
                    'caption': text
                })
            else:
                result = send_message(channel_id, text)
            
            if result:
                results['success'].append(channel_name)
                logger.info(f"Broadcast sent to {channel_name}")
            else:
                results['failed'].append(channel_name)
                logger.error(f"Broadcast failed for {channel_name}")
                
            time.sleep(0.5)  # Rate limiting
            
        except Exception as e:
            results['failed'].append(channel_name)
            logger.error(f"Broadcast error for {channel_name}: {e}")
    
    return results

def save_broadcast_log(admin_id, channels, message, results):
    """Save broadcast history"""
    try:
        if broadcast_collection:
            broadcast_collection.insert_one({
                'admin_id': admin_id,
                'channels': channels,
                'message': message,
                'results': results,
                'created_at': datetime.utcnow()
            })
    except Exception as e:
        logger.error(f"Broadcast log error: {e}")

# --- VIDEO REQUEST FUNCTIONS ---

def notify_admins_new_request(request_doc):
    """Notify Telegram admins about new video request"""
    try:
        admin_chat_ids = get_admin_chat_ids()
        
        message = f"""
🆕 New Video Request

📝 Description: {request_doc.get('description', 'No description')}
🆔 Request ID: {request_doc['_id']}
📅 Date: {request_doc['createdAt'].strftime('%Y-%m-%d %H:%M')}

Use /complete {request_doc['_id']} <video_url> to mark as completed
        """
        
        for chat_id in admin_chat_ids:
            send_message(chat_id, message)
            
            # Send media if available
            if request_doc.get('mediaUrl'):
                if request_doc.get('mediaType') == 'image':
                    send_telegram_photo(chat_id, request_doc['mediaUrl'])
                elif request_doc.get('mediaType') == 'video':
                    send_telegram_video(chat_id, request_doc['mediaUrl'])
    except Exception as e:
        logger.error(f"Error notifying admins: {e}")

def send_telegram_photo(chat_id, photo_url):
    """Send photo via Telegram"""
    url = f"{TELEGRAM_API_URL}/sendPhoto"
    data = {'chat_id': chat_id, 'photo': photo_url}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        logger.error(f"Telegram photo error: {e}")

def send_telegram_video(chat_id, video_url):
    """Send video via Telegram"""
    url = f"{TELEGRAM_API_URL}/sendVideo"
    data = {'chat_id': chat_id, 'video': video_url}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        logger.error(f"Telegram video error: {e}")

def get_admin_chat_ids():
    """Get list of admin chat IDs from database or config"""
    try:
        if db:
            admins_collection = db['admins']
            admins = list(admins_collection.find())
            return [admin['chat_id'] for admin in admins]
    except Exception as e:
        logger.error(f"Get admin IDs error: {e}")
    
    # Fallback to admin ID if no database
    return [ADMIN_TELEGRAM_ID]

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
            {'_id': ObjectId(request_id)},
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
            message += f"🆔 {str(req['_id'])}\n"
            message += f"📝 {req.get('description', 'No description')[:50]}\n"
            message += f"📅 {req['createdAt'].strftime('%Y-%m-%d %H:%M')}\n\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_all_requests_command(chat_id):
    """Show all requests with status"""
    try:
        all_requests = list(requests_collection.find().sort('createdAt', -1).limit(20))
        
        if not all_requests:
            send_message(chat_id, "No requests found")
            return
        
        message = "📊 All Requests:\n\n"
        for req in all_requests:
            status_emoji = "✅" if req['status'] == 'completed' else "⏳" if req['status'] == 'pending' else "❌"
            message += f"{status_emoji} {str(req['_id'])[:8]}... - {req['status']}\n"
            message += f"📝 {req.get('description', 'No description')[:40]}...\n"
            message += f"👁️ {req.get('views', 0)} views\n\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_add_admin_command(chat_id):
    """Add user as admin"""
    try:
        if db:
            admins_collection = db['admins']
            admins_collection.update_one(
                {'chat_id': chat_id},
                {'$set': {'chat_id': chat_id, 'addedAt': datetime.utcnow()}},
                upsert=True
            )
            send_message(chat_id, "✅ You are now an admin!")
        else:
            send_message(chat_id, "❌ Database not available")
    except Exception as e:
        send_message(chat_id, f"❌ Error: {str(e)}")

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
        
        doc = {
            "title": data.get('title'),
            "type": data.get('type'),
            "channel": data.get('channel', 'diskwala'),
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
USER_STATE_TIMEOUT = 3600  # 1 hour

def cleanup_old_states():
    """Clean up expired user states"""
    while True:
        threading.Event().wait(300)  # Every 5 minutes
        current_time = time.time()
        try:
            with view_lock:
                expired = [
                    chat_id for chat_id, state in USER_STATE.items()
                    if current_time - state.get('timestamp', 0) > USER_STATE_TIMEOUT
                ]
                for chat_id in expired:
                    USER_STATE.pop(chat_id, None)
                if expired:
                    logger.info(f"Cleaned {len(expired)} expired states")
        except Exception as e:
            logger.error(f"State cleanup error: {e}")

# --- TELEGRAM UPDATE HANDLER ---

def process_update(update):
    try:
        message = update.get('message')
        if not message:
            logger.warning(f"No message in update: {update.get('update_id')}")
            return
            
        chat_id = message['chat']['id']
        
        # Ignore messages from channels
        if chat_id in [GROUP_TELEGRAM_ID, TELUGU_GROUP_ID, CONTENT_FORWARD_CHANNEL_ID]:
            return
        
        text = message.get('text', '').strip()
        user_id = message['from']['id']
        
        # Admin only
        if user_id != ADMIN_TELEGRAM_ID:
            send_message(chat_id, "❌ Access Denied. Administrator only.")
            return
        
        state = USER_STATE.get(chat_id, {'step': 'main', 'timestamp': time.time()})
        
        # --- MAIN MENU ---
        if text in ['/start', 'Back to Menu', 'Cancel']:
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            send_message(
                chat_id, 
                f"🚀 {PRODUCT_NAME} Admin Bot\n\n"
                f"📁 DiskWala Posts - Post to DiskWala channel\n"
                f"🇮🇳 Telugu Posts - Post to Telugu channel\n"
                f"🎬 Video Files - Forward files to video channel\n"
                f"📢 Broadcast - Send message to channels\n"
                f"📥 Video Requests - Manage video requests\n"
                f"🌐 Open WebApp - Access web interface\n\n"
                f"Choose an option:",
                MAIN_KEYBOARD
            )
            return
        
        # --- VIDEO REQUESTS ---
        if text == '📥 Video Requests':
            USER_STATE[chat_id] = {'step': 'requests_menu', 'timestamp': time.time()}
            send_message(
                chat_id,
                "📥 Video Requests Management\n\n"
                "📋 Pending Requests - View pending requests\n"
                "📊 All Requests - View all requests\n\n"
                "You can also use commands:\n"
                "/complete <request_id> <video_url>\n"
                "/requests - Show pending requests",
                REQUESTS_KEYBOARD
            )
            return
        
        # --- REQUESTS MENU ---
        if state['step'] == 'requests_menu':
            if text == '📋 Pending Requests':
                handle_requests_command(chat_id)
                USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
                return
            elif text == '📊 All Requests':
                handle_all_requests_command(chat_id)
                USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
                return
        
        # --- BROADCAST ---
        if text == '📢 Broadcast':
            USER_STATE[chat_id] = {'step': 'broadcast_select', 'timestamp': time.time()}
            send_message(
                chat_id,
                "📢 Broadcast Message\n\n"
                "Select channel(s) to broadcast:\n\n"
                "📺 DiskWala - DiskWala channel only\n"
                "🇮🇳 Telugu - Telugu channel only\n"
                "🎬 Video Files - Video Files channel only\n"
                "📡 All Channels - Send to all channels\n\n"
                "Choose target:",
                BROADCAST_KEYBOARD
            )
            return
        
        # --- Handle commands ---
        if text.startswith('/complete'):
            handle_complete_command(chat_id, text)
            return
        
        if text.startswith('/requests'):
            handle_requests_command(chat_id)
            return
        
        if text.startswith('/addadmin'):
            handle_add_admin_command(chat_id)
            return
        
        # --- BROADCAST CHANNEL SELECTION ---
        if state['step'] == 'broadcast_select':
            channel_map = {
                '📺 DiskWala': ['diskwala'],
                '🇮🇳 Telugu': ['telugu'],
                '🎬 Video Files': ['video_files'],
                '📡 All Channels': ['diskwala', 'telugu', 'video_files']
            }
            
            if text in channel_map:
                USER_STATE[chat_id] = {
                    'step': 'broadcast_content',
                    'channels': channel_map[text],
                    'timestamp': time.time()
                }
                
                channel_names = [BROADCAST_CHANNELS[ch]['name'] for ch in channel_map[text]]
                send_message(
                    chat_id,
                    f"✅ Selected: {', '.join(channel_names)}\n\n"
                    f"Now send your broadcast message.\n"
                    f"You can send:\n"
                    f"• Text message\n"
                    f"• Photo with caption\n"
                    f"• Video with caption\n\n"
                    f"Type 'Cancel' to abort."
                )
            return
        
        # --- BROADCAST CONTENT ---
        if state['step'] == 'broadcast_content':
            broadcast_text = text
            media_id = None
            media_type = None
            
            # Check for media
            if 'photo' in message:
                media_id = message['photo'][-1]['file_id']
                media_type = 'photo'
                broadcast_text = message.get('caption', '')
            elif 'video' in message:
                media_id = message['video']['file_id']
                media_type = 'video'
                broadcast_text = message.get('caption', '')
            
            if not broadcast_text and not media_id:
                send_message(chat_id, "❌ Please send text or media with caption")
                return
            
            channels = state['channels']
            send_message(chat_id, "⏳ Broadcasting...")
            
            results = broadcast_message(broadcast_text, channels, media_id, media_type)
            
            # Save log
            save_broadcast_log(user_id, channels, broadcast_text, results)
            
            # Build result message
            result_msg = "📢 Broadcast Complete!\n\n"
            if results['success']:
                result_msg += f"✅ Success ({len(results['success'])}):\n"
                result_msg += "\n".join([f"  • {ch}" for ch in results['success']])
            
            if results['failed']:
                result_msg += f"\n\n❌ Failed ({len(results['failed'])}):\n"
                result_msg += "\n".join([f"  • {ch}" for ch in results['failed']])
            
            send_message(chat_id, result_msg, MAIN_KEYBOARD)
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            return
        
        # --- DISKWALA POSTS ---
        if text == 'DiskWala Posts':
            USER_STATE[chat_id] = {
                'step': 'channel_mode',
                'channel_type': 'diskwala',
                'channel_id': GROUP_TELEGRAM_ID,
                'channel_name': 'DiskWala',
                'timestamp': time.time()
            }
            send_message(
                chat_id,
                "📁 DiskWala Posts\n\n"
                "✏️ Single Post - Create one post\n"
                "📨 Forward Multiple - Forward directly\n\n"
                "Choose method:",
                CHANNEL_MODE_KEYBOARD
            )
            return
        
        # --- TELUGU POSTS ---
        if text == 'Telugu Posts':
            USER_STATE[chat_id] = {
                'step': 'channel_mode',
                'channel_type': 'telugu',
                'channel_id': TELUGU_GROUP_ID,
                'channel_name': 'Telugu',
                'timestamp': time.time()
            }
            send_message(
                chat_id,
                "🇮🇳 Telugu Posts\n\n"
                "✏️ Single Post - Create one post\n"
                "📨 Forward Multiple - Forward directly\n\n"
                "Choose method:",
                CHANNEL_MODE_KEYBOARD
            )
            return
        
        # --- VIDEO FILES ---
        if text == 'Video Files':
            USER_STATE[chat_id] = {'step': 'video_files', 'timestamp': time.time()}
            send_message(
                chat_id,
                "🎬 Video Files Mode\n\n"
                "Send files one by one.\n"
                "Type 'Cancel' when done."
            )
            return
        
        # --- CHANNEL MODE SELECTION ---
        if state['step'] == 'channel_mode':
            channel_name = state.get('channel_name', 'Channel')
            
            if text == 'Single Post':
                USER_STATE[chat_id]['step'] = 'channel_media'
                USER_STATE[chat_id]['data'] = {}
                USER_STATE[chat_id]['timestamp'] = time.time()
                send_message(chat_id, f"📤 {channel_name} - Step 1/3: Send thumbnail (photo/video)")
                return
            
            elif text == 'Forward Multiple':
                USER_STATE[chat_id]['step'] = 'channel_forward'
                USER_STATE[chat_id]['timestamp'] = time.time()
                send_message(
                    chat_id,
                    f"📨 Forward messages to {channel_name} channel.\n\n"
                    "Forward messages to me and I'll send them to the channel.\n\n"
                    "Type 'Cancel' when done."
                )
                return
        
        # --- SINGLE POST FLOW ---
        if state['step'] == 'channel_media':
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
                USER_STATE[chat_id]['step'] = 'channel_title'
                USER_STATE[chat_id]['timestamp'] = time.time()
                send_message(chat_id, f"✅ {media_type.title()} saved!\n\nStep 2/3: Send title")
            else:
                send_message(chat_id, "❌ Send photo or video")
            return
        
        elif state['step'] == 'channel_title':
            USER_STATE[chat_id]['data']['title'] = text.strip()
            USER_STATE[chat_id]['step'] = 'channel_urls'
            USER_STATE[chat_id]['timestamp'] = time.time()
            send_message(chat_id, "✅ Title saved!\n\nStep 3/3: Send URLs (one per line)")
            return
        
        elif state['step'] == 'channel_urls':
            urls = [url.strip() for url in text.strip().split('\n') 
                   if url.strip() and validate_url(url.strip())]
            
            if not urls:
                send_message(chat_id, "❌ Send valid URLs (http:// or https://)")
                return
            
            title = state['data']['title']
            media_id = state['data']['telegram_media_id']
            media_type = state['data']['media_type']
            channel_id = state.get('channel_id', GROUP_TELEGRAM_ID)
            channel_name = state.get('channel_name', 'Channel')
            channel_type = state.get('channel_type', 'diskwala')
            
            links = [{"url": url, "episode_title": "Watch Now" if i == 0 else f"Link {i+1}"} 
                     for i, url in enumerate(urls)]
            
            send_message(chat_id, f"⏳ Posting to {channel_name}...")
            
            result = send_media_post(title, media_id, media_type, links, channel_id, channel_name)
            
            if result:
                content_id = save_content({
                    "title": title,
                    "type": "video",
                    "channel": channel_type,
                    "thumbnail_url": f"telegram_file_id:{media_id}",
                    "post_type": f"{channel_type}_{media_type}",
                    "telegram_media_id": media_id,
                    "telegram_message_id": result.get('message_id') if isinstance(result, dict) else None,
                    "telegram_chat_id": channel_id,
                    "diskwala_url": urls[0],
                    "tags": title.lower().split(),
                    "links": links,
                })
                
                msg = f"🎉 Success!\n✅ Posted to {channel_name} channel"
                if content_id:
                    msg += f"\n📊 Content ID: {content_id}"
                send_message(chat_id, msg, MAIN_KEYBOARD)
            else:
                send_message(chat_id, f"❌ Failed to post to {channel_name}", MAIN_KEYBOARD)
            
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            return
        
        # --- FORWARD MULTIPLE ---
        elif state['step'] == 'channel_forward':
            if 'forward_from' in message or 'forward_from_chat' in message:
                channel_id = state.get('channel_id', GROUP_TELEGRAM_ID)
                channel_name = state.get('channel_name', 'Channel')
                
                result = send_telegram('copyMessage', {
                    'chat_id': channel_id,
                    'from_chat_id': chat_id,
                    'message_id': message['message_id'],
                })
                
                send_message(chat_id, f"✅ Forwarded to {channel_name}" if result else "❌ Failed")
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
        logger.error(f"Process error: {e}", exc_info=True)
        try:
            send_message(chat_id, "🚨 Error occurred. Please try again.", MAIN_KEYBOARD)
        except:
            pass

# --- FLASK ROUTES ---

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

@app.route('/webhook/telegram', methods=['POST'])
def telegram_webhook():
    """Handle Telegram webhook updates for video requests"""
    try:
        update = request.get_json()
        
        if 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']
            text = message.get('text', '')
            
            # Handle /complete command
            if text.startswith('/complete'):
                handle_complete_command(chat_id, text)
            
            # Handle /requests command
            elif text.startswith('/requests'):
                handle_requests_command(chat_id)
            
            # Handle /addadmin command
            elif text.startswith('/addadmin'):
                handle_add_admin_command(chat_id)
        
        return jsonify({'ok': True}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'error': str(e)}), 500

# --- VIDEO REQUEST ROUTES ---

@app.route('/api/request', methods=['POST'])
def create_request():
    """Create a new video request"""
    try:
        description = request.form.get('description', '')
        media_url = None
        media_type = None
        
        # Handle file upload
        if 'file' in request.files:
            file = request.files['file']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
                filename = f"{timestamp}_{filename}"
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                file.save(filepath)
                
                # Generate public URL (adjust based on your deployment)
                base_url = request.host_url.rstrip('/')
                media_url = f"{base_url}/uploads/{filename}"
                media_type = 'image' if is_image(filename) else 'video'
        
        # Handle URL submission
        elif 'url' in request.form:
            media_url = request.form.get('url')
            # Determine type from URL extension
            if any(ext in media_url.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif']):
                media_type = 'image'
            elif any(ext in media_url.lower() for ext in ['.mp4', '.avi', '.mov']):
                media_type = 'video'
            else:
                media_type = 'unknown'
        
        # Create request document
        request_doc = {
            'description': description,
            'mediaUrl': media_url,
            'mediaType': media_type,
            'status': 'pending',
            'videoResult': None,
            'views': 0,
            'createdAt': datetime.utcnow(),
            'updatedAt': datetime.utcnow()
        }
        
        if not requests_collection:
            return jsonify({'error': 'Database not configured'}), 503
            
        result = requests_collection.insert_one(request_doc)
        request_doc['_id'] = str(result.inserted_id)
        
        # Notify admins via Telegram
        notify_admins_new_request(request_doc)
        
        return jsonify({
            'success': True,
            'requestId': str(result.inserted_id),
            'message': 'Request submitted successfully'
        }), 201
        
    except Exception as e:
        logger.error(f"Create request error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/requests/popular', methods=['GET'])
def get_popular_requests():
    """Get popular and most viewed completed requests"""
    try:
        if not requests_collection:
            return jsonify({'error': 'Database not configured'}), 503
            
        # Get completed requests sorted by views
        popular = list(requests_collection.find(
            {'status': 'completed', 'videoResult': {'$ne': None}}
        ).sort('views', -1).limit(12))
        
        # Convert ObjectId to string
        for req in popular:
            req['_id'] = str(req['_id'])
            if 'createdAt' in req:
                req['createdAt'] = req['createdAt'].isoformat()
            if 'updatedAt' in req:
                req['updatedAt'] = req['updatedAt'].isoformat()
        
        return jsonify({
            'success': True,
            'requests': popular
        }), 200
        
    except Exception as e:
        logger.error(f"Popular requests error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/request/<request_id>/view', methods=['POST'])
def increment_view(request_id):
    """Increment view count for a request"""
    try:
        if not requests_collection:
            return jsonify({'error': 'Database not configured'}), 503
            
        result = requests_collection.update_one(
            {'_id': ObjectId(request_id)},
            {'$inc': {'views': 1}}
        )
        
        if result.modified_count > 0:
            return jsonify({'success': True}), 200
        else:
            return jsonify({'error': 'Request not found'}), 404
            
    except Exception as e:
        logger.error(f"Increment view error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/requests', methods=['GET'])
def get_requests():
    """Get all video requests"""
    try:
        if not requests_collection:
            return jsonify({'error': 'Database not configured'}), 503
            
        status = request.args.get('status')
        query = {}
        if status:
            query['status'] = status
        
        requests_list = list(requests_collection.find(query).sort('createdAt', -1))
        
        # Convert ObjectId to string and format dates
        for req in requests_list:
            req['_id'] = str(req['_id'])
            if 'createdAt' in req:
                req['createdAt'] = req['createdAt'].isoformat()
            if 'updatedAt' in req:
                req['updatedAt'] = req['updatedAt'].isoformat()
        
        return jsonify({
            'success': True,
            'requests': requests_list
        }), 200
        
    except Exception as e:
        logger.error(f"Get requests error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/request/<request_id>', methods=['GET'])
def get_request(request_id):
    """Get a specific request"""
    try:
        if not requests_collection:
            return jsonify({'error': 'Database not configured'}), 503
            
        request_doc = requests_collection.find_one({'_id': ObjectId(request_id)})
        
        if not request_doc:
            return jsonify({'error': 'Request not found'}), 404
        
        request_doc['_id'] = str(request_doc['_id'])
        if 'createdAt' in request_doc:
            request_doc['createdAt'] = request_doc['createdAt'].isoformat()
        if 'updatedAt' in request_doc:
            request_doc['updatedAt'] = request_doc['updatedAt'].isoformat()
        
        return jsonify({
            'success': True,
            'request': request_doc
        }), 200
        
    except Exception as e:
        logger.error(f"Get request error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/request/<request_id>/complete', methods=['POST'])
@require_auth
def complete_request(request_id):
    """Admin endpoint to mark request as completed with video"""
    try:
        data = request.get_json()
        video_url = data.get('videoUrl')
        
        if not video_url:
            return jsonify({'error': 'Video URL is required'}), 400
        
        if not requests_collection:
            return jsonify({'error': 'Database not configured'}), 503
            
        result = requests_collection.update_one(
            {'_id': ObjectId(request_id)},
            {
                '$set': {
                    'status': 'completed',
                    'videoResult': video_url,
                    'updatedAt': datetime.utcnow()
                }
            }
        )
        
        if result.modified_count == 0:
            return jsonify({'error': 'Request not found or already completed'}), 404
        
        return jsonify({
            'success': True,
            'message': 'Request marked as completed'
        }), 200
        
    except Exception as e:
        logger.error(f"Complete request error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """Serve uploaded files"""
    try:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        return send_file(filepath)
    except Exception as e:
        logger.error(f"Serve file error: {e}")
        return jsonify({'error': 'File not found'}), 404

# --- EXISTING ROUTES ---

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": PRODUCT_NAME, 
        "status": "online",
        "webapp": WEBAPP_URL,
        "features": ["content", "broadcast", "video_requests", "telegram_bot"]
    }), 200

@app.route('/health', methods=['GET'])
def health():
    status = {"status": "healthy", "components": {}}
    
    # Check MongoDB
    try:
        if client:
            client.admin.command('ping')
            status["components"]["mongodb"] = "ok"
        else:
            status["components"]["mongodb"] = "not_configured"
            status["status"] = "degraded"
    except Exception as e:
        status["components"]["mongodb"] = f"error: {str(e)}"
        status["status"] = "unhealthy"
    
    # Check Telegram
    if TELEGRAM_API:
        try:
            result = send_telegram('getMe', {})
            status["components"]["telegram"] = "ok" if result else "error"
            if not result:
                status["status"] = "unhealthy"
        except Exception as e:
            status["components"]["telegram"] = f"error: {str(e)}"
            status["status"] = "unhealthy"
    else:
        status["components"]["telegram"] = "not_configured"
        status["status"] = "degraded"
    
    return jsonify(status), 200 if status["status"] == "healthy" else 503

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
        
        if channel := request.args.get('channel'):
            query['channel'] = channel.lower()
            
        if tag := request.args.get('tag'):
            query['tags'] = tag.lower()
            
        if search := request.args.get('q'):
            regex = {"$regex": search, "$options": "i"}
            query['$or'] = [{"title": regex}, {"tags": regex}]

        projection = {'title': 1, 'type': 1, 'channel': 1, 'thumbnail_url': 1, 'tags': 1, 'views': 1, 'created_at': 1, 'links': 1}
        
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

@app.route('/api/channels', methods=['GET'])
def get_channels():
    """Get list of available channels"""
    return jsonify({
        "success": True,
        "channels": [
            {"id": "diskwala", "name": "DiskWala", "telegram_id": GROUP_TELEGRAM_ID},
            {"id": "telugu", "name": "Telugu", "telegram_id": TELUGU_GROUP_ID},
            {"id": "video_files", "name": "Video Files", "telegram_id": CONTENT_FORWARD_CHANNEL_ID}
        ]
    }), 200

@app.route('/api/broadcasts', methods=['GET'])
@require_auth
def get_broadcasts():
    """Get broadcast history"""
    if not broadcast_collection:
        return jsonify({"error": "DB not configured"}), 503
    
    try:
        page = max(1, int(request.args.get('page', 1)))
        limit = min(int(request.args.get('limit', 20)), 50)
        skip = (page - 1) * limit
        
        total = broadcast_collection.count_documents({})
        cursor = broadcast_collection.find({}).sort("created_at", -1).skip(skip).limit(limit)
        
        items = []
        for doc in cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            items.append(doc)
        
        return jsonify({
            "success": True,
            "data": items,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": (total + limit - 1) // limit
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Broadcast history error: {e}")
        return jsonify({"error": "Failed"}), 500

@app.route('/api/broadcast', methods=['POST'])
@require_auth
def api_broadcast():
    """API endpoint for broadcasting"""
    try:
        data = request.get_json() or {}
        
        message = data.get('message', '').strip()
        channels = data.get('channels', [])
        
        if not message:
            return jsonify({"error": "Message required"}), 400
        
        if not channels:
            return jsonify({"error": "At least one channel required"}), 400
        
        # Validate channels
        valid_channels = [ch for ch in channels if ch in BROADCAST_CHANNELS]
        if not valid_channels:
            return jsonify({"error": "Invalid channels"}), 400
        
        # Perform broadcast
        results = broadcast_message(message, valid_channels)
        
        # Save log
        save_broadcast_log(ADMIN_TELEGRAM_ID, valid_channels, message, results)
        
        return jsonify({
            "success": True,
            "results": results
        }), 200
        
    except Exception as e:
        logger.error(f"API broadcast error: {e}")
        return jsonify({"error": "Failed"}), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get system statistics"""
    try:
        stats = {
            "total_content": 0,
            "total_views": 0,
            "total_broadcasts": 0,
            "total_requests": 0,
            "pending_requests": 0,
            "channels": {}
        }
        
        if content_collection:
            stats["total_content"] = content_collection.count_documents({})
            
            # Get total views
            pipeline = [{"$group": {"_id": None, "total": {"$sum": "$views"}}}]
            result = list(content_collection.aggregate(pipeline))
            if result:
                stats["total_views"] = result[0].get("total", 0)
            
            # Get content by channel
            for channel_key in BROADCAST_CHANNELS.keys():
                count = content_collection.count_documents({"channel": channel_key})
                stats["channels"][channel_key] = count
        
        if broadcast_collection:
            stats["total_broadcasts"] = broadcast_collection.count_documents({})
            
        if requests_collection:
            stats["total_requests"] = requests_collection.count_documents({})
            stats["pending_requests"] = requests_collection.count_documents({"status": "pending"})
        
        return jsonify({
            "success": True,
            "stats": stats
        }), 200
        
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return jsonify({"error": "Failed"}), 500

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
                logger.info(f"Flushed {len(bulk_ops)} view updates")
            
            view_cache.clear()
    except Exception as e:
        logger.error(f"Flush error: {e}")

def flush_views_loop():
    """Periodic view flush loop"""
    while True:
        threading.Event().wait(30)
        flush_views_once()

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
    logger.info(f"Starting {PRODUCT_NAME}...")
    
    init_mongodb()
    
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
    logger.info(f"WebApp URL: {WEBAPP_URL}")
    logger.info(f"Video requests enabled at /api/request")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
