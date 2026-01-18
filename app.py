import os
import json
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS 
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
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
requests_collection = None  # For video requests
post_counter_collection = None  # For post numbering
users_collection = None  # For user subscriptions
plans_collection = None  # For subscription plans

def init_mongodb():
    global client, db, content_collection, counter_collection, broadcast_collection, requests_collection, post_counter_collection, users_collection, plans_collection
    
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
        users_collection = db["users"]  # User subscriptions
        plans_collection = db["plans"]  # Subscription plans
        
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
        users_collection.create_index([("user_id", 1)])
        users_collection.create_index([("expiry_date", 1)])
        users_collection.create_index([("is_active", 1)])
        plans_collection.create_index([("duration_days", 1)])
        
        # Initialize default plans if not exist
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

def get_user_subscription(user_id):
    """Get user subscription details"""
    try:
        if users_collection is None:
            return None
            
        user = users_collection.find_one({
            'user_id': user_id,
            'is_active': True
        })
        
        return user
    except Exception as e:
        logger.error(f"Get subscription error: {e}")
        return None

def create_user_subscription(user_id, plan_id, amount_paid, payment_method="telegram"):
    """Create new user subscription"""
    try:
        if users_collection is None or plans_collection is None:
            return False
            
        # Get plan details
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
            'payment_method': payment_method,
            'purchase_date': datetime.utcnow(),
            'expiry_date': expiry_date,
            'is_active': True,
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        }
        
        # Update or insert user subscription
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': subscription_data},
            upsert=True
        )
        
        # Notify admin about new subscription
        notify_admin_new_subscription(user_id, plan['name'], amount_paid)
        
        return True
    except Exception as e:
        logger.error(f"Create subscription error: {e}")
        return False

def notify_admin_new_subscription(user_id, plan_name, amount):
    """Notify admin about new subscription"""
    try:
        message = f"""
💰 New Subscription Purchase

👤 User ID: {user_id}
📋 Plan: {plan_name}
💵 Amount: ₹{amount}
📅 Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}

User now has access to premium content!
        """
        
        send_message(ADMIN_TELEGRAM_ID, message)
    except Exception as e:
        logger.error(f"Notify admin subscription error: {e}")

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
        [{'text': '💰 Subscriptions'}, {'text': '📋 Plans'}],
        [{'text': 'Cancel'}]
    ],
    'resize_keyboard': True
}

# User keyboard for non-subscribed users
USER_MAIN_KEYBOARD = {
    'keyboard': [
        [{'text': '📥 Request Video'}],
        [{'text': '🆕 My Requests'}, {'text': '💰 Buy Plan'}],
        [{'text': '❌ Cancel'}]
    ],
    'resize_keyboard': True
}

# User keyboard for subscribed users
SUBSCRIBED_USER_KEYBOARD = {
    'keyboard': [
        [{'text': '📥 Request Video'}, {'text': '🎬 Video Files'}],
        [{'text': '🆕 My Requests'}, {'text': '📋 My Plan'}],
        [{'text': '❌ Cancel'}]
    ],
    'resize_keyboard': True
}

# Plans keyboard
PLANS_KEYBOARD = {
    'keyboard': [
        [{'text': '2 Weeks - ₹40'}, {'text': '1 Month - ₹50'}],
        [{'text': '1 Year - ₹200'}, {'text': 'Back to Menu'}]
    ],
    'resize_keyboard': True
}

SUBSCRIPTIONS_KEYBOARD = {
    'keyboard': [
        [{'text': '👥 Active Users'}, {'text': '📋 All Users'}],
        [{'text': '💰 Plan Sales'}, {'text': '📢 Plan Broadcast'}],
        [{'text': 'Back to Menu'}]
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

PLAN_BROADCAST_KEYBOARD = {
    'keyboard': [
        [{'text': '🎯 All Users'}, {'text': '✅ Subscribed Users'}],
        [{'text': '❌ Non-Subscribed Users'}, {'text': 'Back'}]
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
        return False
    
    clean = {k: v for k, v in payload.items() if v is not None}
    
    try:
        response = requests.post(TELEGRAM_API + method, json=clean, timeout=5)
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
    """Send media post to specified channel with post number and request button"""
    if not TELEGRAM_API:
        return False

    # Get bot info for request URL
    bot_url = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "Contact Admin"
    
    link_text = "\n".join([f"🔗 {link.get('episode_title', 'Link')}: {link['url']}" for link in links])
    
    # Add post number to caption if available
    post_number_text = f"#{post_number} " if post_number else ""
    
    # Add request button
    request_button = f"\n\n📥 Request Video: {bot_url}?start=request"
    
    caption = f"{post_number_text}🔥 {title} 🔥\n\n{link_text}{request_button}\n━━━━━━━━━━━━━━━━━\nPowered by {PRODUCT_NAME}"
    
    method = 'sendPhoto' if media_type == 'photo' else 'sendVideo'
    key = 'photo' if media_type == 'photo' else 'video'
    
    return send_telegram(method, {'chat_id': channel_id, key: media_id, 'caption': caption})

def forward_message(chat_id, from_chat_id, message_id):
    """Forward a message"""
    try:
        return send_telegram('forwardMessage', {
            'chat_id': chat_id,
            'from_chat_id': from_chat_id,
            'message_id': message_id
        })
    except Exception as e:
        logger.error(f"Forward message error: {e}")
        return False

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
            'result_media_id': None,
            'result_media_type': None,
            'views': 0,
            'createdAt': datetime.utcnow(),
            'updatedAt': datetime.utcnow()
        }
        
        if requests_collection is None:
            logger.error("Requests collection is None")
            return None
            
        result = requests_collection.insert_one(request_doc)
        request_doc['_id'] = str(result.inserted_id)
        
        # Send the media to admin immediately
        if media_id and media_type and message:
            forward_user_media_to_admin(media_id, media_type, request_doc['request_id'], message)
        
        # Send notification to admin
        notify_admin_new_request(request_doc)
        
        return request_doc
        
    except Exception as e:
        logger.error(f"Create request error: {e}")
        return None

def forward_user_media_to_admin(media_id, media_type, request_id, original_message):
    """Forward user's media to admin"""
    try:
        caption = f"🆕 Request #{request_id}\n👤 From: {original_message['from']['id']}"
        
        if media_type == 'photo':
            send_photo(ADMIN_TELEGRAM_ID, media_id, caption)
        elif media_type == 'video':
            send_video(ADMIN_TELEGRAM_ID, media_id, caption)
            
    except Exception as e:
        logger.error(f"Forward media to admin error: {e}")

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
        
        # Send to admin
        send_message(ADMIN_TELEGRAM_ID, message)
            
    except Exception as e:
        logger.error(f"Notify admin error: {e}")

# --- USER SUBSCRIPTION HANDLERS ---

def handle_plans_command(chat_id, user_id):
    """Show subscription plans to user"""
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

def handle_paid_command(chat_id, user_id, text):
    """Handle /paid command from user after payment"""
    try:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            send_message(chat_id, "Usage: /paid <plan_name> <amount>\nExample: /paid \"1 Month\" 50")
            return
        
        plan_name = parts[1].strip('"\'')
        try:
            amount = int(parts[2])
        except:
            send_message(chat_id, "Invalid amount. Use numbers only.")
            return
        
        # Find plan
        plan = plans_collection.find_one({
            'name': plan_name,
            'price': amount,
            'is_active': True
        })
        
        if not plan:
            send_message(chat_id, f"Plan '{plan_name}' with price ₹{amount} not found.")
            return
        
        # Ask for payment proof
        USER_STATE[chat_id] = {
            'step': 'waiting_payment_proof',
            'user_id': user_id,
            'plan_id': str(plan['_id']),
            'plan_name': plan_name,
            'amount': amount,
            'timestamp': time.time()
        }
        
        send_message(chat_id, f"Please send the payment receipt/screenshot for {plan_name} - ₹{amount}")
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_user_plan_command(chat_id, user_id):
    """Show user's current subscription plan"""
    try:
        user_sub = get_user_subscription(user_id)
        
        if not user_sub:
            send_message(chat_id, "You don't have an active subscription.\n\nUse /plans to view available plans.")
            return
        
        days_left = (user_sub['expiry_date'] - datetime.utcnow()).days
        
        message = "📋 Your Subscription\n\n"
        message += f"📋 Plan: {user_sub.get('plan_name', 'Unknown')}\n"
        message += f"💵 Amount Paid: ₹{user_sub.get('amount_paid', 0)}\n"
        message += f"📅 Purchased: {user_sub.get('purchase_date', datetime.utcnow()).strftime('%Y-%m-%d')}\n"
        message += f"📅 Expires: {user_sub.get('expiry_date', datetime.utcnow()).strftime('%Y-%m-%d')}\n"
        message += f"⏳ Days Left: {max(0, days_left)} days\n"
        message += f"✅ Status: {'Active' if user_sub.get('is_active', False) else 'Inactive'}\n\n"
        
        if days_left <= 7:
            message += "⚠️ Your subscription is expiring soon!\n"
            message += f"Renew now to continue access: /plans\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_video_files_access(chat_id, user_id):
    """Handle video files access for subscribed users"""
    try:
        if not check_user_subscription(user_id):
            send_message(chat_id, "⛔ Access Denied\n\nYou need an active subscription to access video files.\n\nUse /plans to subscribe.")
            return
        
        # For now, just show a message. You can expand this to show actual video files
        message = "🎬 Video Files Access\n\n"
        message += "✅ You have access to premium video files!\n\n"
        message += "Available commands:\n"
        message += "/latest - Get latest video files\n"
        message += "/search <keyword> - Search videos\n"
        message += "/categories - Browse by category\n\n"
        message += "More features coming soon!"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

# --- ADMIN SUBSCRIPTION MANAGEMENT ---

def handle_admin_subscriptions(chat_id):
    """Show admin subscriptions menu"""
    USER_STATE[chat_id] = {'step': 'subscriptions_menu', 'timestamp': time.time()}
    send_message(chat_id, "💰 Subscription Management\n\nSelect an option:", SUBSCRIPTIONS_KEYBOARD)

def handle_active_users_command(chat_id):
    """Show active subscribed users"""
    try:
        if users_collection is None:
            send_message(chat_id, "Database not available")
            return
            
        active_users = list(users_collection.find({
            'is_active': True,
            'expiry_date': {'$gt': datetime.utcnow()}
        }).sort('expiry_date', 1).limit(20))
        
        if not active_users:
            send_message(chat_id, "No active subscribers")
            return
        
        message = "👥 Active Subscribers\n\n"
        total_revenue = 0
        
        for user in active_users:
            days_left = (user['expiry_date'] - datetime.utcnow()).days
            message += f"👤 User ID: {user['user_id']}\n"
            message += f"📋 Plan: {user.get('plan_name', 'Unknown')}\n"
            message += f"⏳ Days Left: {days_left}\n"
            message += f"📅 Expires: {user['expiry_date'].strftime('%Y-%m-%d')}\n\n"
            total_revenue += user.get('amount_paid', 0)
        
        message += f"💰 Total Active Subscribers: {len(active_users)}\n"
        message += f"💵 Total Revenue: ₹{total_revenue}"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_all_users_command(chat_id):
    """Show all users"""
    try:
        if users_collection is None:
            send_message(chat_id, "Database not available")
            return
            
        all_users = list(users_collection.find().sort('purchase_date', -1).limit(20))
        
        if not all_users:
            send_message(chat_id, "No users found")
            return
        
        message = "📋 All Users\n\n"
        active_count = 0
        total_revenue = 0
        
        for user in all_users:
            is_active = user.get('is_active', False) and user.get('expiry_date', datetime.utcnow()) > datetime.utcnow()
            status = "✅ Active" if is_active else "❌ Inactive"
            
            if is_active:
                active_count += 1
            
            message += f"👤 {user['user_id']} - {status}\n"
            message += f"📋 {user.get('plan_name', 'Unknown')} - ₹{user.get('amount_paid', 0)}\n"
            message += f"📅 {user.get('purchase_date', datetime.utcnow()).strftime('%Y-%m-%d')}\n\n"
            total_revenue += user.get('amount_paid', 0)
        
        message += f"📊 Stats:\n"
        message += f"✅ Active Users: {active_count}\n"
        message += f"📈 Total Users: {len(all_users)}\n"
        message += f"💰 Total Revenue: ₹{total_revenue}"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_plan_sales_command(chat_id):
    """Show plan sales statistics"""
    try:
        if users_collection is None or plans_collection is None:
            send_message(chat_id, "Database not available")
            return
        
        # Get all plans
        all_plans = list(plans_collection.find({'is_active': True}))
        
        message = "💰 Plan Sales Statistics\n\n"
        
        total_revenue = 0
        total_sales = 0
        
        for plan in all_plans:
            # Count sales for this plan
            plan_sales = users_collection.count_documents({
                'plan_id': plan['_id']
            })
            
            # Calculate revenue for this plan
            plan_revenue = 0
            plan_users = list(users_collection.find({'plan_id': plan['_id']}))
            for user in plan_users:
                plan_revenue += user.get('amount_paid', 0)
            
            message += f"📋 {plan['name']} - ₹{plan['price']}\n"
            message += f"   📈 Sales: {plan_sales}\n"
            message += f"   💵 Revenue: ₹{plan_revenue}\n\n"
            
            total_sales += plan_sales
            total_revenue += plan_revenue
        
        message += f"📊 Totals:\n"
        message += f"📈 Total Sales: {total_sales}\n"
        message += f"💰 Total Revenue: ₹{total_revenue}"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

def handle_plan_broadcast_menu(chat_id):
    """Show plan broadcast menu"""
    USER_STATE[chat_id] = {'step': 'plan_broadcast_select', 'timestamp': time.time()}
    send_message(chat_id, "📢 Plan Promotion Broadcast\n\nSelect target audience:", PLAN_BROADCAST_KEYBOARD)

def broadcast_plan_promotion(chat_id, target_type, message_text):
    """Broadcast plan promotion to selected users"""
    try:
        if users_collection is None:
            send_message(chat_id, "Database not available")
            return
        
        # Prepare promotion message
        promotion_message = f"💰 {PRODUCT_NAME} Premium Plans\n\n"
        promotion_message += "Get direct video files - no links or ads!\n\n"
        
        # Add available plans
        plans = list(plans_collection.find({'is_active': True}).sort('duration_days', 1))
        for plan in plans:
            promotion_message += f"📋 {plan['name']} - ₹{plan['price']}\n"
            promotion_message += f"⏳ {plan['duration_days']} days access\n\n"
        
        promotion_message += f"💬 Message to purchase: {PAYMENT_BOT_USERNAME}\n"
        promotion_message += "📱 After payment, send receipt to this bot with /paid command\n\n"
        promotion_message += message_text
        
        # Get target users
        if target_type == 'all':
            users = users_collection.distinct('user_id')
        elif target_type == 'subscribed':
            users = users_collection.distinct('user_id', {
                'is_active': True,
                'expiry_date': {'$gt': datetime.utcnow()}
            })
        else:  # non-subscribed
            all_users = set(users_collection.distinct('user_id'))
            subscribed_users = set(users_collection.distinct('user_id', {
                'is_active': True,
                'expiry_date': {'$gt': datetime.utcnow()}
            }))
            users = list(all_users - subscribed_users)
        
        if not users:
            send_message(chat_id, f"No users found for target: {target_type}")
            return
        
        success_count = 0
        failed_count = 0
        
        send_message(chat_id, f"📤 Broadcasting to {len(users)} users...")
        
        for user_id in users[:100]:  # Limit to 100 users to avoid rate limiting
            try:
                send_message(user_id, promotion_message)
                success_count += 1
                time.sleep(0.1)  # Rate limiting
            except:
                failed_count += 1
        
        result_message = f"✅ Broadcast Complete!\n\n"
        result_message += f"✅ Success: {success_count}\n"
        result_message += f"❌ Failed: {failed_count}\n"
        result_message += f"📊 Total: {len(users)}"
        
        send_message(chat_id, result_message, ADMIN_MAIN_KEYBOARD)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")
        USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}

# --- REST OF THE VIDEO REQUEST HANDLERS (same as before) ---

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
                        'result_media_id': media_id,
                        'result_media_type': media_type,
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
                    'video_result': video_url,
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

def handle_user_requests_command(chat_id, user_id):
    """Show user's own requests"""
    try:
        if requests_collection is None:
            send_message(chat_id, "Database not available")
            return
            
        user_requests = list(requests_collection.find({'user_id': user_id}).sort('createdAt', -1).limit(10))
        
        if not user_requests:
            send_message(chat_id, "You haven't made any requests yet.")
            return
        
        message = "📋 Your Requests:\n\n"
        for req in user_requests:
            status_emoji = "✅" if req['status'] == 'completed' else "⏳"
            message += f"{status_emoji} {req['request_id']} - {req['status']}\n"
            message += f"📸 Type: {req.get('media_type', 'Unknown')}\n"
            if req['status'] == 'completed' and req.get('video_result'):
                message += f"🔗 {req['video_result']}\n"
            message += "\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")

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
        
        # Check user subscription status for keyboard
        is_subscribed = check_user_subscription(user_id)
        user_keyboard = SUBSCRIBED_USER_KEYBOARD if is_subscribed else USER_MAIN_KEYBOARD
        
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
                    f"📊 Stats - View system statistics\n"
                    f"💰 Subscriptions - Manage user subscriptions\n"
                    f"📋 Plans - View subscription plans\n\n"
                    f"Choose an option:",
                    ADMIN_MAIN_KEYBOARD
                )
            else:
                welcome_msg = f"👋 Welcome to {PRODUCT_NAME}!\n\n"
                if is_subscribed:
                    welcome_msg += "✅ You have an active subscription!\n\n"
                    welcome_msg += "Here you can:\n"
                    welcome_msg += "📥 Request Video - Request specific videos\n"
                    welcome_msg += "🎬 Video Files - Access premium video files\n"
                    welcome_msg += "🆕 My Requests - Check your request status\n"
                    welcome_msg += "📋 My Plan - View subscription details\n"
                else:
                    welcome_msg += "Here you can:\n"
                    welcome_msg += "📥 Request Video - Request specific videos\n"
                    welcome_msg += "🆕 My Requests - Check your request status\n"
                    welcome_msg += "💰 Buy Plan - Subscribe for premium access\n\n"
                    welcome_msg += "💎 Premium features:\n"
                    welcome_msg += "• Direct video files (no ads/links)\n"
                    welcome_msg += "• Faster response times\n"
                    welcome_msg += "• Priority support\n"
                
                send_message(chat_id, welcome_msg, user_keyboard)
            
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            return
        
        # --- MAIN MENU HANDLING ---
        if text in ['Back to Menu', 'Cancel', '❌ Cancel', 'Back']:
            USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
            if admin:
                send_message(chat_id, "Back to main menu:", ADMIN_MAIN_KEYBOARD)
            else:
                send_message(chat_id, "Back to main menu:", user_keyboard)
            return
        
        # --- ADMIN COMMANDS ---
        if admin:
            # Handle admin commands
            if text.startswith('/reply'):
                handle_reply_command(chat_id, text)
                return
            elif text.startswith('/sendmedia'):
                handle_sendmedia_command(chat_id, text)
                return
            elif text.startswith('/complete'):
                handle_complete_command(chat_id, text)
                return
            elif text == '/requests':
                handle_requests_command(chat_id)
                return
            
            # Handle admin sending matching media
            state = USER_STATE.get(chat_id, {'step': 'main', 'timestamp': time.time()})
            if state['step'] == 'sending_matching_media':
                handle_admin_matching_media(chat_id, message, state)
                return
            
            # Handle payment proof from users
            if state['step'] == 'reviewing_payment':
                handle_admin_payment_review(chat_id, message, state)
                return
            
            # Admin menu options
            if text == '💰 Subscriptions':
                handle_admin_subscriptions(chat_id)
                return
            
            elif text == '📋 Plans':
                handle_plans_command(chat_id, user_id)
                return
            
            # Subscriptions menu
            elif state['step'] == 'subscriptions_menu':
                if text == '👥 Active Users':
                    handle_active_users_command(chat_id)
                    return
                elif text == '📋 All Users':
                    handle_all_users_command(chat_id)
                    return
                elif text == '💰 Plan Sales':
                    handle_plan_sales_command(chat_id)
                    return
                elif text == '📢 Plan Broadcast':
                    handle_plan_broadcast_menu(chat_id)
                    return
            
            # Plan broadcast menu
            elif state['step'] == 'plan_broadcast_select':
                if text == '🎯 All Users':
                    USER_STATE[chat_id] = {
                        'step': 'plan_broadcast_message',
                        'target': 'all',
                        'timestamp': time.time()
                    }
                    send_message(chat_id, "Enter promotion message to send to all users:")
                    return
                elif text == '✅ Subscribed Users':
                    USER_STATE[chat_id] = {
                        'step': 'plan_broadcast_message',
                        'target': 'subscribed',
                        'timestamp': time.time()
                    }
                    send_message(chat_id, "Enter promotion message to send to subscribed users:")
                    return
                elif text == '❌ Non-Subscribed Users':
                    USER_STATE[chat_id] = {
                        'step': 'plan_broadcast_message',
                        'target': 'non_subscribed',
                        'timestamp': time.time()
                    }
                    send_message(chat_id, "Enter promotion message to send to non-subscribed users:")
                    return
            
            # Plan broadcast message
            elif state['step'] == 'plan_broadcast_message':
                broadcast_plan_promotion(chat_id, state['target'], text)
                USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
                return
            
            # Video requests menu
            elif text == '📥 Video Requests':
                USER_STATE[chat_id] = {'step': 'requests_menu', 'timestamp': time.time()}
                send_message(
                    chat_id,
                    "📥 Video Requests Management\n\n"
                    "📋 Pending Requests - View pending requests\n"
                    "📊 All Requests - View all requests\n\n"
                    "Commands:\n"
                    "/reply <id> <url> - Send video URL to user\n"
                    "/sendmedia <id> - Send matching media to user\n"
                    "/complete <id> <url> - Mark as completed",
                    REQUESTS_KEYBOARD
                )
                return
            
            # Requests menu
            elif state['step'] == 'requests_menu':
                if text == '📋 Pending Requests':
                    handle_requests_command(chat_id)
                    USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
                    return
                elif text == '📊 All Requests':
                    handle_all_requests_command(chat_id)
                    USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
                    return
            
            # Stats
            elif text == '📊 Stats':
                handle_stats_command(chat_id)
                return
            
            # Rest of admin flow (posting, broadcasting, etc.)
            # ... [same as previous code for admin posting flow]
            
        # --- USER FLOW ---
        else:
            state = USER_STATE.get(chat_id, {'step': 'main', 'timestamp': time.time()})
            
            # Handle /paid command from user
            if text.startswith('/paid'):
                handle_paid_command(chat_id, user_id, text)
                return
            
            # Handle /plans command
            elif text == '/plans' or text == '💰 Buy Plan':
                handle_plans_command(chat_id, user_id)
                return
            
            # Handle /myplan command
            elif text == '/myplan' or text == '📋 My Plan':
                handle_user_plan_command(chat_id, user_id)
                return
            
            # Handle video files access for subscribed users
            elif text == '🎬 Video Files':
                handle_video_files_access(chat_id, user_id)
                return
            
            # User menu options
            elif text == '📥 Request Video':
                handle_user_request(chat_id, user_id)
                return
            
            elif text == '🆕 My Requests':
                handle_user_requests_command(chat_id, user_id)
                return
            
            # Handle payment proof
            elif state['step'] == 'waiting_payment_proof':
                handle_user_payment_proof(chat_id, user_id, message, state)
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
                # Check if user sent appropriate media
                if state['step'] == 'waiting_photo' and 'photo' not in message:
                    send_message(chat_id, "Please send a photo or select 'Cancel'")
                    return
                
                if state['step'] == 'waiting_video' and 'video' not in message:
                    send_message(chat_id, "Please send a video or select 'Cancel'")
                    return
                
                # Process the media submission
                process_user_media_request(chat_id, user_id, message, state['step'])
                return
            
            # Handle user sending media without going through menu
            elif ('photo' in message or 'video' in message) and state['step'] == 'main':
                # User sent media directly, treat as request
                process_user_media_request(chat_id, user_id, message)
                return
            
            # Unknown command for user
            else:
                send_message(chat_id, "Please select an option from the menu:", user_keyboard)
        
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

def handle_user_payment_proof(chat_id, user_id, message, state):
    """Handle user sending payment proof"""
    try:
        plan_id = state['plan_id']
        plan_name = state['plan_name']
        amount = state['amount']
        
        # Forward payment proof to admin
        if 'photo' in message or 'document' in message:
            # Forward media to admin
            if 'photo' in message:
                photo_id = message['photo'][-1]['file_id']
                send_photo(ADMIN_TELEGRAM_ID, photo_id, 
                          f"💰 Payment Proof from User {user_id}\nPlan: {plan_name}\nAmount: ₹{amount}")
            elif 'document' in message:
                # Forward document
                forward_message(ADMIN_TELEGRAM_ID, chat_id, message['message_id'])
                send_message(ADMIN_TELEGRAM_ID, 
                           f"💰 Payment Proof from User {user_id}\nPlan: {plan_name}\nAmount: ₹{amount}")
            
            # Update user state for admin review
            USER_STATE[ADMIN_TELEGRAM_ID] = {
                'step': 'reviewing_payment',
                'user_id': user_id,
                'plan_id': plan_id,
                'plan_name': plan_name,
                'amount': amount,
                'timestamp': time.time()
            }
            
            # Notify admin
            admin_msg = f"💰 New Payment Proof Received\n\n"
            admin_msg += f"👤 User ID: {user_id}\n"
            admin_msg += f"📋 Plan: {plan_name}\n"
            admin_msg += f"💵 Amount: ₹{amount}\n\n"
            admin_msg += "Send 'approve' to activate subscription or 'reject' to deny."
            send_message(ADMIN_TELEGRAM_ID, admin_msg)
            
            # Notify user
            send_message(chat_id, "✅ Payment proof received! Admin will review and activate your subscription shortly.", USER_MAIN_KEYBOARD)
        
        else:
            send_message(chat_id, "Please send a photo or document of your payment receipt.")
            return
        
        USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")
        USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}

def handle_admin_payment_review(chat_id, message, state):
    """Handle admin reviewing payment proof"""
    try:
        user_id = state['user_id']
        plan_id = state['plan_id']
        plan_name = state['plan_name']
        amount = state['amount']
        text = message.get('text', '').lower()
        
        if text == 'approve':
            # Create subscription
            if create_user_subscription(user_id, plan_id, amount):
                # Notify user
                user_msg = f"🎉 Subscription Activated!\n\n"
                user_msg += f"✅ Your {plan_name} subscription has been activated!\n"
                user_msg += f"💵 Amount: ₹{amount}\n"
                user_msg += f"📅 Activated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}\n\n"
                user_msg += "You now have access to premium video files!"
                send_message(user_id, user_msg)
                
                # Notify admin
                send_message(chat_id, f"✅ Subscription activated for user {user_id}")
            else:
                send_message(chat_id, f"❌ Failed to activate subscription for user {user_id}")
        
        elif text == 'reject':
            # Notify user
            user_msg = f"❌ Payment Rejected\n\n"
            user_msg += f"Your payment for {plan_name} has been rejected.\n"
            user_msg += f"Please contact admin if you believe this is an error."
            send_message(user_id, user_msg)
            
            # Notify admin
            send_message(chat_id, f"❌ Payment rejected for user {user_id}")
        
        else:
            send_message(chat_id, "Send 'approve' to activate or 'reject' to deny.")
            return
        
        USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}
        
    except Exception as e:
        send_message(chat_id, f"Error: {str(e)}")
        USER_STATE[chat_id] = {'step': 'main', 'timestamp': time.time()}

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

def handle_stats_command(chat_id):
    """Handle stats command"""
    try:
        stats = {
            "total_content": 0,
            "total_requests": 0,
            "pending_requests": 0,
            "completed_requests": 0,
            "total_users": 0,
            "active_subscribers": 0,
            "total_revenue": 0,
            "channels": {}
        }
        
        if content_collection is not None:
            stats["total_content"] = content_collection.count_documents({})
            for channel in BROADCAST_CHANNELS:
                count = content_collection.count_documents({"channel": channel})
                stats["channels"][channel] = count
        
        if requests_collection is not None:
            stats["total_requests"] = requests_collection.count_documents({})
            stats["pending_requests"] = requests_collection.count_documents({"status": "pending"})
            stats["completed_requests"] = requests_collection.count_documents({"status": "completed"})
        
        if users_collection is not None:
            stats["total_users"] = users_collection.count_documents({})
            stats["active_subscribers"] = users_collection.count_documents({
                'is_active': True,
                'expiry_date': {'$gt': datetime.utcnow()}
            })
            
            # Calculate total revenue
            all_users = list(users_collection.find({}))
            stats["total_revenue"] = sum(user.get('amount_paid', 0) for user in all_users)
        
        message = "📊 System Statistics\n\n"
        message += f"📁 Total Posts: {stats['total_content']}\n"
        message += f"📥 Total Requests: {stats['total_requests']}\n"
        message += f"⏳ Pending Requests: {stats['pending_requests']}\n"
        message += f"✅ Completed Requests: {stats['completed_requests']}\n"
        message += f"👥 Total Users: {stats['total_users']}\n"
        message += f"💰 Active Subscribers: {stats['active_subscribers']}\n"
        message += f"💵 Total Revenue: ₹{stats['total_revenue']}\n\n"
        
        message += "📈 Channel Posts:\n"
        for channel, count in stats['channels'].items():
            channel_name = BROADCAST_CHANNELS[channel]['name']
            message += f"  • {channel_name}: {count}\n"
        
        send_message(chat_id, message)
        
    except Exception as e:
        send_message(chat_id, f"Error getting stats: {str(e)}")

# --- REST OF THE CODE (Flask routes, background tasks, startup) ---

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
        "bot": f"@{BOT_USERNAME}" if BOT_USERNAME else "Not set",
        "features": ["telegram_bot", "video_requests", "subscriptions", "multi_channel", "media_forwarding"]
    }), 200

@app.route('/health', methods=['GET'])
def health():
    status = {"status": "healthy", "service": PRODUCT_NAME}
    return jsonify(status), 200

def flush_views_once():
    """Flush all pending views to DB"""
    try:
        with view_lock:
            if not view_cache or content_collection is None:
                return
            
            from pymongo import UpdateOne
            
            bulk_ops = []
            for cid, count in view_cache.items():
                if count > 0 and ObjectId.is_valid(cid):
                    bulk_ops.append(UpdateOne(
                        {"_id": ObjectId(cid)},
                        {"$inc": {"views": count}}
                    ))
            
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
    logger.info(f"Payment bot: {PAYMENT_BOT_USERNAME}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
