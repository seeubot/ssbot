import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS 
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import logging
import threading
from cachetools import TTLCache
import time
import atexit
import signal
from urllib.parse import urlparse

# --- CONSTANTS & CONFIGURATION ---
ADMIN_TELEGRAM_ID = 1352497419
GROUP_TELEGRAM_ID = -1002541647242  # DiskWala Channel
TELUGU_GROUP_ID = -1003551789476  # Telugu Channel
CONTENT_FORWARD_CHANNEL_ID = -1002776780769  # Video Files Channel
PRODUCT_NAME = "Adult-Hub"
PAYMENT_BOT_USERNAME = "@seeutech_bot"  # Payment bot username

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
requests_collection = None
post_counter_collection = None
users_collection = None
plans_collection = None

def init_mongodb():
    global client, db, content_collection, counter_collection, broadcast_collection
    global requests_collection, post_counter_collection, users_collection, plans_collection
    
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
        client.admin.command('ping')
        
        db_name = "streamhub"
        collection_name = "diskwala_posts"
        
        db = client[db_name]
        content_collection = db[collection_name]
        counter_collection = db["counters"]
        broadcast_collection = db["broadcasts"]
        requests_collection = db["video_requests"]
        post_counter_collection = db["post_counters"]
        users_collection = db["users"]
        plans_collection = db["plans"]
        
        # Initialize default plans
        init_default_plans()
        
        logger.info(f"MongoDB connected: {db_name}.{collection_name}")
        return True
        
    except Exception as e:
        logger.error(f"MongoDB init failed: {e}")
        return False

def init_default_plans():
    """Initialize default subscription plans"""
    try:
        default_plans = [
            {
                'name': '2 Weeks',
                'duration_days': 14,
                'price': 40,
                'description': '2 weeks access to all video files (no ads, direct links)',
                'is_active': True,
                'created_at': datetime.utcnow()
            },
            {
                'name': '1 Month',
                'duration_days': 30,
                'price': 50,
                'description': '1 month access to all video files (no ads, direct links)',
                'is_active': True,
                'created_at': datetime.utcnow()
            },
            {
                'name': '1 Year',
                'duration_days': 365,
                'price': 200,
                'description': '1 year access to all video files (no ads, direct links)',
                'is_active': True,
                'created_at': datetime.utcnow()
            }
        ]
        
        for plan in default_plans:
            plans_collection.update_one(
                {'name': plan['name']},
                {'$set': plan},
                upsert=True
            )
        
        logger.info("Default plans initialized")
    except Exception as e:
        logger.error(f"Init default plans error: {e}")

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

# --- USER SUBSCRIPTION FUNCTIONS ---
def check_user_subscription(user_id):
    """Check if user has active subscription"""
    try:
        if users_collection is None:
            return False
            
        user = users_collection.find_one({
            'user_id': user_id,
            'is_active': True,
            'expiry_date': {'$gt': datetime.utcnow()}
        })
        
        return user is not None
    except Exception as e:
        logger.error(f"Check subscription error: {e}")
        return False

def create_user_subscription(user_id, plan_id, amount_paid):
    """Create new user subscription"""
    try:
        if users_collection is None or plans_collection is None:
            return False
            
        plan = plans_collection.find_one({'_id': ObjectId(plan_id)})
        if not plan:
            return False
        
        expiry_date = datetime.utcnow() + timedelta(days=plan['duration_days'])
        
        subscription_data = {
            'user_id': user_id,
            'plan_id': ObjectId(plan_id),
            'plan_name': plan['name'],
            'duration_days': plan['duration_days'],
            'amount_paid': amount_paid,
            'purchase_date': datetime.utcnow(),
            'expiry_date': expiry_date,
            'is_active': True
        }
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': subscription_data},
            upsert=True
        )
        
        return True
    except Exception as e:
        logger.error(f"Create subscription error: {e}")
        return False

# --- AUTHENTICATION ---
def is_admin(user_id):
    """Check if user is admin"""
    return user_id == ADMIN_TELEGRAM_ID

# --- TELEGRAM SETUP ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/" if BOT_TOKEN else None
APP_URL = os.environ.get("APP_URL", "http://localhost:8000")
PORT = int(os.environ.get("PORT", 8000))
BOT_USERNAME = None

app = Flask(__name__)
CORS(app)

USER_STATE = {}

# --- KEYBOARDS ---
ADMIN_MAIN_KEYBOARD = {
    'keyboard': [
        [{'text': 'DiskWala Posts'}, {'text': 'Telugu Posts'}],
        [{'text': 'Video Files'}, {'text': '📢 Broadcast'}],
        [{'text': '📥 Video Requests'}, {'text': '📊 Stats'}],
        [{'text': '💰 Subscriptions'}, {'text': '📋 Plans'}],
        [{'text': 'Cancel'}]
    ],
    'resize_keyboard': True
}

USER_MAIN_KEYBOARD = {
    'keyboard': [
        [{'text': '📥 Request Video'}],
        [{'text': '🆕 My Requests'}, {'text': '💰 Buy Plan'}],
        [{'text': '❌ Cancel'}]
    ],
    'resize_keyboard': True
}

SUBSCRIBED_USER_KEYBOARD = {
    'keyboard': [
        [{'text': '📥 Request Video'}, {'text': '🎬 Video Files'}],
        [{'text': '🆕 My Requests'}, {'text': '📋 My Plan'}],
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

PLANS_KEYBOARD = {
    'keyboard': [
        [{'text': '2 Weeks - ₹40'}, {'text': '1 Month - ₹50'}],
        [{'text': '1 Year - ₹200'}, {'text': 'Back to Menu'}]
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
        [{'text': '❌ Cancel'}]
    ],
    'resize_keyboard': True
}

# --- TELEGRAM FUNCTIONS ---
def send_telegram(method, payload):
    if not TELEGRAM_API:
        logger.error("Telegram API not configured")
        return False
    
    try:
        response = requests.post(TELEGRAM_API + method, json=payload, timeout=5)
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

def send_media_post(title, media_id, media_type, links, channel_id, channel_name, post_number=None):
    """Send media post to specified channel with post number and request button"""
    if not TELEGRAM_API:
        return False

    bot_url = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "Contact Admin"
    
    link_text = "\n".join([f"🔗 {link.get('episode_title', 'Link')}: {link['url']}" for link in links])
    
    post_number_text = f"#{post_number} " if post_number else ""
    request_button = f"\n\n📥 Request Video: {bot_url}?start=request"
    
    caption = f"{post_number_text}🔥 {title} 🔥\n\n{link_text}{request_button}\n━━━━━━━━━━━━━━━━━\nPowered by {PRODUCT_NAME}"
    
    method = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
    key = 'photo' if media_type == 'photo' else 'video'
    
    return send_telegram(method, {'chat_id': channel_id, key: media_id, 'caption': caption})

def send_photo(chat_id, photo_id, caption=None):
    """Send photo by file_id"""
    try:
        payload = {'chat_id': chat_id, 'photo': photo_id}
        if caption:
            payload['caption'] = caption
        return send_telegram('sendPhoto', payload)
    except Exception as e:
        logger.error(f"Send photo error: {e}")
        return False

def send_video(chat_id, video_id, caption=None):
    """Send video by file_id"""
    try:
        payload = {'chat_id': chat_id, 'video': video_id}
        if caption:
            payload['caption'] = caption
        return send_telegram('sendVideo', payload)
    except Exception as e:
        logger.error(f"Send video error: {e}")
        return False

# --- VIDEO REQUEST FUNCTIONS ---
def create_video_request(user_id, media_id=None, media_type=None, message=None):
    """Create a new video request from user"""
    try:
        request_id = get_next_sequence('video_request_id')
        
        request_doc = {
            'request_id': f"REQ{request_id:06d}",
            'user_id': user_id,
            'media_id': media_id,
            'media_type': media_type,
            'telegram_message_id': message.get('message_id') if message else None,
            'status': 'pending',
            'video_result': None,
            'createdAt': datetime.utcnow(),
            'updatedAt': datetime.utcnow()
        }
        
        if requests_collection is None:
            logger.error("Requests collection is None")
            return None
            
        result = requests_collection.insert_one(request_doc)
        request_doc['_id'] = str(result.inserted_id)
        
        # Send the media to admin immediately
        if media_id and media_type:
            caption = f"🆕 Request #{request_doc['request_id']}\n👤 From: {user_id}"
            if media_type == 'photo':
                send_photo(ADMIN_TELEGRAM_ID, media_id, caption)
            elif media_type == 'video':
                send_video(ADMIN_TELEGRAM_ID, media_id, caption)
        
        # Send notification to admin
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
        media_type = request_doc.get('media_type', 'Unknown')
        
        message = f"""
📥 New Video Request #{request_id}

📸 Type: {media_type}
👤 User ID: {user_id}
📅 Time: {request_doc['createdAt'].strftime('%H:%M:%S')}
⏳ Status: Pending

Commands:
/reply {request_id} <video_url> - Send URL to user
/sendmedia {request_id} - Send matching media to user
        """
        
        send_message(ADMIN_TELEGRAM_ID, message)
    except Exception as e:
        logger.error(f"Notify admin error: {e}")

def handle_user_request(chat_id, user_id):
    """Handle user's video request initiation"""
    USER_STATE[chat_id] = {
        'step': 'request_media',
        'user_id': user_id,
        'timestamp': time.time()
    }
    send_message(
        chat_id,
        "📥 Video Request\n\n"
        "Please send a photo or video clip of what you're looking for.\n"
        "We'll forward it to our admin who will find the matching video for you.\n\n"
        "Type 'Cancel' to abort.",
        REQUEST_MEDIA_KEYBOARD
    )

def handle_request_media(chat_id, user_id, text):
    """Handle request media type selection"""
    if text == '📷 Send Photo':
        USER_STATE[chat_id] = {
            'step': 'waiting_photo',
            'user_id': user_id,
            'timestamp': time.time()
        }
        send_message(chat_id, "📷 Please send a photo of the scene/actress you're looking for:")
        
    elif text == '🎥 Send Video':
        USER_STATE[chat_id] = {
            'step': 'waiting_video',
            'user_id': user_id,
            'timestamp': time.time()
        }
        send_message(chat_id, "🎥 Please send a video clip of what you're looking for:")

def process_user_media_request(chat_id, user_id, message, step=None):
    """Process user's media request"""
    media_id = None
    media_type = None
    
    if 'photo' in message:
        media_id = message['photo'][-1]['file_id']
        media_type = 'photo'
    elif 'video' in message:
        media_id = message['video']['file_id']
        media_type = 'video'
    else:
        send_message(chat_id, "❌ Please send a photo or video")
        return
    
    # Create request
    request_doc = create_video_request(
        user_id=user_id,
        media_id=media_id,
        media_type=media_type,
        message=message
    )
    
    if request_doc:
        send_message(
            chat_id,
            f"✅ Request Submitted!\n\n"
            f"🆔 Request ID: {request_doc['request_id']}\n"
            f"📸 Media Type: {media_type}\n\n"
            f"✅ Your media has been forwarded to admin.\n"
            f"We'll notify you when we find the matching video!",
            USER_MAIN_KEYBOARD
        )
    else:
        send_message(chat_id, "❌ Failed to submit request. Please try again.", USER_MAIN_KEYBOARD)
    
    USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}

# --- ADMIN COMMAND HANDLERS ---
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
        
        # Send video URL to user
        user_message = f"""
✅ Request Completed!

Your request #{request_id} has been fulfilled!
🔗 Video Link: {video_url}

Thank you for using our service!
        """
        
        send_message(user_id, user_message)
        
        # Update request status
        result = requests_collection.update_one(
            {'request_id': request_id},
            {
                '$set': {
                    'status': 'completed',
                    'video_result': video_url,
                    'updatedAt': datetime.utcnow()
                }
            }
        )
        
        if result.modified_count > 0:
            send_message(chat_id, f"✅ Video URL sent to user for request {request_id}")
        else:
            send_message(chat_id, f"⚠️ Request {request_id} update failed")
            
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_sendmedia_command(chat_id, text):
    """Handle /sendmedia command - admin sends matching media to user"""
    try:
        parts = text.split()
        if len(parts) < 2:
            send_message(chat_id, "Usage: /sendmedia <request_id>")
            send_message(chat_id, "Then send the matching photo/video you found")
            USER_STATE[chat_id] = {
                'step': 'waiting_matching_media',
                'timestamp': time.time()
            }
            return
        
        request_id = parts[1]
        
        # Find request
        request_doc = requests_collection.find_one({'request_id': request_id})
        if not request_doc:
            send_message(chat_id, f"❌ Request {request_id} not found")
            return
        
        USER_STATE[chat_id] = {
            'step': 'sending_matching_media',
            'request_id': request_id,
            'user_id': request_doc['user_id'],
            'timestamp': time.time()
        }
        
        send_message(chat_id, f"🔄 Now send the matching photo/video for request {request_id}")
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_admin_matching_media(chat_id, message, state):
    """Handle admin sending matching media for a request"""
    try:
        request_id = state['request_id']
        user_id = state['user_id']
        
        media_id = None
        media_type = None
        
        if 'photo' in message:
            media_id = message['photo'][-1]['file_id']
            media_type = 'photo'
            caption = message.get('caption', '')
        elif 'video' in message:
            media_id = message['video']['file_id']
            media_type = 'video'
            caption = message.get('caption', '')
        elif 'text' in message:
            # Admin sent text with URL
            user_message = f"✅ Your request #{request_id} has been fulfilled!\n\n🔗 {message['text']}"
            send_message(user_id, user_message)
            
            # Update request
            requests_collection.update_one(
                {'request_id': request_id},
                {
                    '$set': {
                        'status': 'completed',
                        'video_result': message['text'],
                        'updatedAt': datetime.utcnow()
                    }
                }
            )
            
            send_message(chat_id, f"✅ Text/URL sent to user for request {request_id}")
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            return
        else:
            send_message(chat_id, "❌ Please send photo, video, or text with URL")
            return
        
        # Send matching media to user
        user_caption = f"✅ Your request #{request_id} has been fulfilled!"
        if caption:
            user_caption += f"\n\n{caption}"
        
        success = False
        if media_type == 'photo':
            success = send_photo(user_id, media_id, user_caption)
        elif media_type == 'video':
            success = send_video(user_id, media_id, user_caption)
        
        if success:
            # Update request
            requests_collection.update_one(
                {'request_id': request_id},
                {
                    '$set': {
                        'status': 'completed',
                        'video_result': 'Media forwarded',
                        'updatedAt': datetime.utcnow()
                    }
                }
            )
            
            send_message(chat_id, f"✅ Matching media sent to user for request {request_id}")
        else:
            send_message(chat_id, f"❌ Failed to send media for request {request_id}")
        
        USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")
        USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}

# --- ADMIN POSTING FLOW ---
def handle_admin_posting_flow(chat_id, text, message, state):
    """Handle admin posting flow"""
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
        return True
        
    elif text == 'Telugu Posts':
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
        return True
        
    elif text == 'Video Files':
        USER_STATE[chat_id] = {'step': 'video_files', 'timestamp': time.time()}
        send_message(
            chat_id,
            "🎬 Video Files Mode\n\n"
            "Send files one by one.\n"
            "Type 'Cancel' when done."
        )
        return True
        
    elif text == '📢 Broadcast':
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
        return True
        
    elif text == '📥 Video Requests':
        USER_STATE[chat_id] = {'step': 'requests_menu', 'timestamp': time.time()}
        send_message(
            chat_id,
            "📥 Video Requests Management\n\n"
            "📋 Pending Requests - View pending requests\n"
            "📊 All Requests - View all requests\n\n"
            "Commands:\n"
            "/reply <id> <url> - Send video URL to user\n"
            "/sendmedia <id> - Send matching media to user",
            REQUESTS_KEYBOARD
        )
        return True
        
    elif text == '📊 Stats':
        handle_stats_command(chat_id)
        return True
        
    elif text == '💰 Subscriptions':
        handle_subscriptions_menu(chat_id)
        return True
        
    elif text == '📋 Plans':
        handle_plans_command(chat_id, ADMIN_TELEGRAM_ID)
        return True
    
    # Handle channel mode selection
    elif state['step'] == 'channel_mode':
        if text == 'Single Post':
            USER_STATE[chat_id]['step'] = 'channel_media'
            USER_STATE[chat_id]['data'] = {}
            send_message(chat_id, f"📤 {state['channel_name']} - Step 1/3: Send thumbnail (photo/video)")
            return True
        elif text == 'Forward Multiple':
            USER_STATE[chat_id]['step'] = 'channel_forward'
            send_message(chat_id, f"📨 Forward messages to {state['channel_name']} channel. Type 'Cancel' when done.")
            return True
    
    # Handle channel media
    elif state['step'] == 'channel_media':
        if 'video' in message:
            media_id = message['video']['file_id']
            media_type = 'video'
        elif 'photo' in message:
            media_id = message['photo'][-1]['file_id']
            media_type = 'photo'
        else:
            send_message(chat_id, "❌ Send photo or video")
            return True
        
        USER_STATE[chat_id]['data'] = {
            'telegram_media_id': media_id, 
            'media_type': media_type
        }
        USER_STATE[chat_id]['step'] = 'channel_title'
        send_message(chat_id, f"✅ {media_type.title()} saved!\n\nStep 2/3: Send title")
        return True
    
    # Handle channel title
    elif state['step'] == 'channel_title':
        USER_STATE[chat_id]['data']['title'] = text.strip()
        USER_STATE[chat_id]['step'] = 'channel_urls'
        send_message(chat_id, "✅ Title saved!\n\nStep 3/3: Send URLs (one per line)")
        return True
    
    # Handle channel URLs
    elif state['step'] == 'channel_urls':
        urls = [url.strip() for url in text.strip().split('\n') if url.strip()]
        
        if not urls:
            send_message(chat_id, "❌ Send valid URLs")
            return True
        
        title = state['data']['title']
        media_id = state['data']['telegram_media_id']
        media_type = state['data']['media_type']
        channel_id = state.get('channel_id', GROUP_TELEGRAM_ID)
        channel_name = state.get('channel_name', 'Channel')
        channel_type = state.get('channel_type', 'diskwala')
        
        links = [{"url": url, "episode_title": "Watch Now" if i == 0 else f"Link {i+1}"} 
                 for i, url in enumerate(urls)]
        
        post_number = get_next_post_number(channel_type)
        send_message(chat_id, f"⏳ Posting to {channel_name}...")
        
        result = send_media_post(title, media_id, media_type, links, channel_id, channel_name, post_number)
        
        if result:
            try:
                content_data = {
                    "title": title,
                    "type": "video",
                    "channel": channel_type,
                    "post_number": post_number,
                    "thumbnail_url": f"telegram_file_id:{media_id}",
                    "post_type": f"{channel_type}_{media_type}",
                    "telegram_media_id": media_id,
                    "telegram_chat_id": channel_id,
                    "diskwala_url": urls[0],
                    "tags": title.lower().split(),
                    "links": links,
                    "views": 0,
                    "created_at": datetime.utcnow()
                }
                content_collection.insert_one(content_data)
                
                msg = f"🎉 Success!\n✅ Posted to {channel_name} channel\n📊 Post #: {post_number}"
                send_message(chat_id, msg, ADMIN_MAIN_KEYBOARD)
            except Exception as e:
                logger.error(f"Save content error: {e}")
                send_message(chat_id, f"✅ Posted to {channel_name} but failed to save to database")
        else:
            send_message(chat_id, f"❌ Failed to post to {channel_name}", ADMIN_MAIN_KEYBOARD)
        
        USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
        return True
    
    # Handle requests menu
    elif state['step'] == 'requests_menu':
        if text == '📋 Pending Requests':
            handle_requests_command(chat_id)
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            return True
        elif text == '📊 All Requests':
            handle_all_requests_command(chat_id)
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            return True
    
    # Handle broadcast select
    elif state['step'] == 'broadcast_select':
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
            send_message(chat_id, f"✅ Selected channels\n\nNow send your broadcast message (text or media with caption)")
            return True
    
    # Handle broadcast content
    elif state['step'] == 'broadcast_content':
        channels = state['channels']
        broadcast_text = text
        media_id = None
        media_type = None
        
        if 'photo' in message:
            media_id = message['photo'][-1]['file_id']
            media_type = 'photo'
            broadcast_text = message.get('caption', '')
        elif 'video' in message:
            media_id = message['video']['file_id']
            media_type = 'video'
            broadcast_text = message.get('caption', '')
        
        # Simple broadcast implementation
        channel_ids = {
            'diskwala': GROUP_TELEGRAM_ID,
            'telugu': TELUGU_GROUP_ID,
            'video_files': CONTENT_FORWARD_CHANNEL_ID
        }
        
        for channel_key in channels:
            channel_id = channel_ids.get(channel_key)
            if channel_id:
                if media_id and media_type:
                    if media_type == 'photo':
                        send_photo(channel_id, media_id, broadcast_text)
                    else:
                        send_video(channel_id, media_id, broadcast_text)
                else:
                    send_message(channel_id, broadcast_text)
        
        send_message(chat_id, f"✅ Broadcast sent to {len(channels)} channel(s)", ADMIN_MAIN_KEYBOARD)
        USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
        return True
    
    return False

# --- SUBSCRIPTION HANDLERS ---
def handle_subscriptions_menu(chat_id):
    """Handle subscriptions menu"""
    send_message(chat_id, "💰 Subscription Management\n\nComing soon...", ADMIN_MAIN_KEYBOARD)

def handle_plans_command(chat_id, user_id):
    """Show subscription plans"""
    try:
        if plans_collection is None:
            send_message(chat_id, "Plans not available at the moment.")
            return
        
        plans = list(plans_collection.find({'is_active': True}).sort('duration_days', 1))
        
        if not plans:
            send_message(chat_id, "No plans available at the moment.")
            return
        
        message = "💰 Subscription Plans\n\n"
        message += "Get direct video files - no links or ads!\n\n"
        
        for plan in plans:
            message += f"📋 {plan['name']}\n"
            message += f"💵 Price: ₹{plan['price']}\n"
            message += f"⏳ Duration: {plan['duration_days']} days\n"
            message += f"📝 {plan.get('description', '')}\n\n"
        
        message += f"To purchase, message: {PAYMENT_BOT_USERNAME}\n"
        message += "After payment, send receipt to this bot with command:\n"
        message += "/paid <plan_name> <amount>\n\n"
        message += "Example: /paid \"1 Month\" 50"
        
        send_message(chat_id, message, PLANS_KEYBOARD)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_requests_command(chat_id):
    """Handle /requests command to show pending requests"""
    try:
        if requests_collection is None:
            send_message(chat_id, "Database not available")
            return
            
        pending = list(requests_collection.find({'status': 'pending'}).sort('createdAt', -1).limit(10))
        
        if not pending:
            send_message(chat_id, "No pending requests")
            return
        
        message = "📋 Pending Requests:\n\n"
        for req in pending:
            message += f"🆔 {req['request_id']}\n"
            message += f"👤 User: {req['user_id']}\n"
            message += f"📸 Type: {req.get('media_type', 'Unknown')}\n"
            message += f"📅 {req['createdAt'].strftime('%Y-%m-%d %H:%M')}\n\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_all_requests_command(chat_id):
    """Show all requests"""
    try:
        if requests_collection is None:
            send_message(chat_id, "Database not available")
            return
            
        all_requests = list(requests_collection.find().sort('createdAt', -1).limit(20))
        
        if not all_requests:
            send_message(chat_id, "No requests found")
            return
        
        message = "📊 All Requests:\n\n"
        for req in all_requests:
            status_emoji = "✅" if req['status'] == 'completed' else "⏳"
            message += f"{status_emoji} {req['request_id']} - {req['status']}\n"
            message += f"👤 User: {req['user_id']}\n"
            message += f"📅 {req['createdAt'].strftime('%m-%d %H:%M')}\n\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_stats_command(chat_id):
    """Handle stats command"""
    try:
        stats = {
            "total_content": 0,
            "total_requests": 0,
            "pending_requests": 0,
            "completed_requests": 0,
            "channels": {
                'diskwala': 0,
                'telugu': 0,
                'video_files': 0
            }
        }
        
        if content_collection is not None:
            stats["total_content"] = content_collection.count_documents({})
            stats["channels"]['diskwala'] = content_collection.count_documents({"channel": 'diskwala'})
            stats["channels"]['telugu'] = content_collection.count_documents({"channel": 'telugu'})
            stats["channels"]['video_files'] = content_collection.count_documents({"channel": 'video_files'})
        
        if requests_collection is not None:
            stats["total_requests"] = requests_collection.count_documents({})
            stats["pending_requests"] = requests_collection.count_documents({"status": "pending"})
            stats["completed_requests"] = requests_collection.count_documents({"status": "completed"})
        
        message = "📊 System Statistics\n\n"
        message += f"📁 Total Posts: {stats['total_content']}\n"
        message += f"📥 Total Requests: {stats['total_requests']}\n"
        message += f"⏳ Pending Requests: {stats['pending_requests']}\n"
        message += f"✅ Completed Requests: {stats['completed_requests']}\n\n"
        
        message += "📈 Channel Posts:\n"
        message += f"  • DiskWala: {stats['channels']['diskwala']}\n"
        message += f"  • Telugu: {stats['channels']['telugu']}\n"
        message += f"  • Video Files: {stats['channels']['video_files']}\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error getting stats: {str(e)}")

# --- MAIN UPDATE HANDLER ---
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
        
        # Check user subscription status for keyboard
        is_subscribed = check_user_subscription(user_id)
        user_keyboard = SUBSCRIBED_USER_KEYBOARD if is_subscribed else USER_MAIN_KEYBOARD
        
        logger.info(f"Processing update - Chat: {chat_id}, User: {user_id}, Text: '{text}', Admin: {admin}")
        
        # Handle start command
        if text.startswith('/start'):
            params = text.split()
            if len(params) > 1 and params[1] == 'request':
                handle_user_request(chat_id, user_id)
                return
            
            if admin:
                send_message(
                    chat_id,
                    f"🚀 {PRODUCT_NAME} Admin Bot\n\n"
                    f"Choose an option:",
                    ADMIN_MAIN_KEYBOARD
                )
            else:
                welcome_msg = f"👋 Welcome to {PRODUCT_NAME}!\n\n"
                if is_subscribed:
                    welcome_msg += "✅ You have an active subscription!\n\n"
                    welcome_msg += "Choose an option:"
                else:
                    welcome_msg += "Choose an option:"
                
                send_message(chat_id, welcome_msg, user_keyboard)
            
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            return
        
        # Handle menu commands
        if text in ['Back to Menu', 'Cancel', '❌ Cancel', 'Back']:
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            if admin:
                send_message(chat_id, "Back to main menu:", ADMIN_MAIN_KEYBOARD)
            else:
                send_message(chat_id, "Back to main menu:", user_keyboard)
            return
        
        # --- ADMIN FLOW ---
        if admin:
            state = USER_STATE.get(chat_id, {'step': 'main', 'timestamp': time.time()})
            
            # Handle admin commands
            if text.startswith('/reply'):
                handle_reply_command(chat_id, text)
                return
            elif text.startswith('/sendmedia'):
                handle_sendmedia_command(chat_id, text)
                return
            
            # Handle admin sending matching media
            if state['step'] == 'sending_matching_media':
                handle_admin_matching_media(chat_id, message, state)
                return
            
            # Try admin posting flow
            if handle_admin_posting_flow(chat_id, text, message, state):
                return
            
            # If no handler matched, show menu
            send_message(chat_id, "Please select an option:", ADMIN_MAIN_KEYBOARD)
        
        # --- USER FLOW ---
        else:
            state = USER_STATE.get(chat_id, {'step': 'main', 'timestamp': time.time()})
            
            # Handle /plans command
            if text == '/plans' or text == '💰 Buy Plan':
                handle_plans_command(chat_id, user_id)
                return
            
            # User menu options
            if text == '📥 Request Video':
                handle_user_request(chat_id, user_id)
                return
            
            # User request media selection
            elif state['step'] == 'request_media':
                if text in ['📷 Send Photo', '🎥 Send Video']:
                    handle_request_media(chat_id, user_id, text)
                else:
                    send_message(chat_id, "Please select an option:", REQUEST_MEDIA_KEYBOARD)
                return
            
            # Handle user sending media
            elif state['step'] in ['waiting_photo', 'waiting_video']:
                if state['step'] == 'waiting_photo' and 'photo' not in message:
                    send_message(chat_id, "Please send a photo or select 'Cancel'")
                    return
                
                if state['step'] == 'waiting_video' and 'video' not in message:
                    send_message(chat_id, "Please send a video or select 'Cancel'")
                    return
                
                process_user_media_request(chat_id, user_id, message, state['step'])
                return
            
            # Handle user sending media directly
            elif ('photo' in message or 'video' in message) and state['step'] == 'main':
                process_user_media_request(chat_id, user_id, message)
                return
            
            # If no handler matched, show menu
            send_message(chat_id, "Please select an option:", user_keyboard)
        
    except Exception as e:
        logger.error(f"Process error: {e}", exc_info=True)
        try:
            if chat_id:
                if is_admin(chat_id):
                    send_message(chat_id, "🚨 Error occurred. Please try again.", ADMIN_MAIN_KEYBOARD)
                else:
                    send_message(chat_id, "🚨 Error occurred. Please try again.", USER_MAIN_KEYBOARD)
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

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": PRODUCT_NAME, 
        "status": "online",
        "bot": f"@{BOT_USERNAME}" if BOT_USERNAME else "Not set"
    }), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy"}), 200

# --- BACKGROUND TASKS ---
def cleanup_old_states():
    """Clean up expired user states"""
    while True:
        threading.Event().wait(300)
        current_time = time.time()
        try:
            expired = [
                chat_id for chat_id, state in USER_STATE.items()
                if current_time - state.get('timestamp', 0) > 1800
            ]
            for chat_id in expired:
                USER_STATE.pop(chat_id, None)
        except Exception as e:
            logger.error(f"State cleanup error: {e}")

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
    result = send_telegram('setWebhook', {
        'url': webhook_url,
        'max_connections': 5,
        'drop_pending_updates': True
    })
    
    if result:
        logger.info(f"Webhook set successfully to {webhook_url}")
    else:
        logger.error("Webhook setup failed")
    
    return result

# --- STARTUP ---
if __name__ == '__main__':
    logger.info(f"Starting {PRODUCT_NAME}...")
    
    # Initialize MongoDB
    if not init_mongodb():
        logger.error("Failed to initialize MongoDB")
    
    # Get bot info
    get_bot_info()
    
    # Start background tasks
    threading.Thread(target=cleanup_old_states, daemon=True).start()
    
    # Set webhook
    if BOT_TOKEN:
        set_webhook()
    
    logger.info(f"Starting server on port {PORT}")
    logger.info(f"Bot username: @{BOT_USERNAME if BOT_USERNAME else 'Not set'}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
