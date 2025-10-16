import os
import json
import requests
from flask import Flask, request, jsonify
from pymongo import MongoClient
from datetime import datetime
import logging

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

def init_mongodb():
    """Initialize MongoDB connection with error handling."""
    global client, db, content_collection
    
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
        
        db_name = os.environ.get("DB_NAME", "movie")
        collection_name = os.environ.get("COLLECTION_NAME", "content_items")
        
        db = client[db_name]
        content_collection = db[collection_name]
        
        logger.info(f"MongoDB connected. Database: {db_name}, Collections: {db.list_collection_names()}")
        return True
    except Exception as e:
        logger.error(f"MongoDB initialization failed: {e}")
        return False

# --- 2. TELEGRAM AND FLASK SETUP ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
APP_URL = os.environ.get("APP_URL")
PORT = int(os.environ.get("PORT", 8000))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

app = Flask(__name__)

# Global state to track multi-step conversation
USER_STATE = {}

# FSM States
STATE_START = 'START'
STATE_WAITING_FOR_TYPE = 'WAITING_FOR_TYPE'
STATE_WAITING_FOR_THUMBNAIL = 'WAITING_FOR_THUMBNAIL'
STATE_WAITING_FOR_TITLE = 'WAITING_FOR_TITLE'
STATE_WAITING_FOR_LINK_TITLE = 'WAITING_FOR_LINK_TITLE'
STATE_WAITING_FOR_LINK_URL = 'WAITING_FOR_LINK_URL'
STATE_CONFIRM_LINK = 'CONFIRM_LINK'

# --- 3. CORE BOT FUNCTIONS ---

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
    if content_collection is None:
        logger.error("MongoDB collection not initialized")
        return False

    try:
        document = {
            "title": content_data.get('title'),
            "type": content_data.get('type'),
            "thumbnail_url": content_data.get('thumbnail_url'),
            "links": content_data.get('links', []),
            "created_at": datetime.utcnow()
        }
        
        result = content_collection.insert_one(document)
        logger.info(f"Content saved with ID: {result.inserted_id}")
        return True
    except Exception as e:
        logger.error(f"MongoDB Save Error: {e}")
        return False

# --- 4. CONVERSATION HANDLERS ---

def start_new_upload(chat_id):
    """Starts the content upload process."""
    USER_STATE[chat_id] = {'state': STATE_WAITING_FOR_TYPE, 'content_data': {'links': []}}
    keyboard = {
        'inline_keyboard': [
            [{'text': '🎬 Movie', 'callback_data': 'type_Movie'}],
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
    send_message(chat_id, "✅ Content Type set.\n\nNow, what is the *Title* of the Movie/Series?")

def ask_for_thumbnail(chat_id):
    USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_THUMBNAIL
    send_message(chat_id, "✅ Title set.\n\nNext, please send the *public URL* for the Content Thumbnail Image:")

def ask_for_link_title(chat_id):
    prompt = "Enter the name for the streaming link (e.g., 'Full Movie' or 'S01E01 Pilot')."
    USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_LINK_TITLE
    send_message(chat_id, f"✅ Thumbnail URL set.\n\n{prompt}")

def finish_upload(chat_id):
    content_data = USER_STATE[chat_id]['content_data']
    
    if not content_data.get('title') or not content_data.get('links'):
        send_message(chat_id, "❌ Error: Missing title or streaming links. Please start over with /start.")
        USER_STATE[chat_id]['state'] = STATE_START
        return

    if save_content(content_data):
        send_message(chat_id, f"🎉 *Success!* Content '{content_data['title']}' saved to database.")
        USER_STATE[chat_id]['state'] = STATE_START
        USER_STATE[chat_id]['content_data'] = {'links': []}
    else:
        send_message(chat_id, "❌ Error: Could not save to database. Please try again later.")

def handle_text_message(chat_id, text):
    """Handle text messages based on current state."""
    state = USER_STATE.get(chat_id, {}).get('state', STATE_START)
    content_data = USER_STATE.get(chat_id, {}).get('content_data', {})

    if text.startswith('/start'):
        start_new_upload(chat_id)
        return

    if state == STATE_WAITING_FOR_TITLE:
        content_data['title'] = text.strip()
        ask_for_thumbnail(chat_id)

    elif state == STATE_WAITING_FOR_THUMBNAIL:
        if text.startswith('http'):
            content_data['thumbnail_url'] = text.strip()
            ask_for_link_title(chat_id)
        else:
            send_message(chat_id, "Please send a *public URL* starting with `http` or `https`.")

    elif state == STATE_WAITING_FOR_LINK_TITLE:
        content_data['current_link_title'] = text.strip()
        USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_LINK_URL
        send_message(chat_id, f"Link name set: *{text.strip()}*\n\nNow, send the *Streaming URL*:")

    elif state == STATE_WAITING_FOR_LINK_URL:
        if text.startswith('http'):
            link_title = content_data.pop('current_link_title', 'Link')
            content_data['links'].append({'episode_title': link_title, 'url': text.strip()})
            
            keyboard = {
                'inline_keyboard': [
                    [{'text': '➕ Add Another Link', 'callback_data': 'add_Yes'}],
                    [{'text': '✅ Done Uploading', 'callback_data': 'add_No'}]
                ]
            }
            send_message(
                chat_id,
                f"✅ Streaming URL added! Total links: {len(content_data['links'])}.\n\nWhat next?",
                reply_markup=keyboard
            )
            USER_STATE[chat_id]['state'] = STATE_CONFIRM_LINK
        else:
            send_message(chat_id, "Please send a URL starting with `http` or `https`.")

    elif state == STATE_START:
        send_message(chat_id, "Please use the /start command to begin a new content upload.")

def handle_callback_query(chat_id, data):
    """Handle inline keyboard button presses."""
    state = USER_STATE.get(chat_id, {}).get('state')

    if state == STATE_WAITING_FOR_TYPE and data.startswith('type_'):
        content_type = data.split('_')[1]
        USER_STATE[chat_id]['content_data']['type'] = content_type
        ask_for_title(chat_id)
        
    elif state == STATE_CONFIRM_LINK and data.startswith('add_'):
        action = data.split('_')[1]
        if action == 'Yes':
            ask_for_link_title(chat_id)
        elif action == 'No':
            finish_upload(chat_id)

# --- 5. WEBHOOK SETUP ---

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

# --- 6. FLASK ROUTES ---

@app.route('/')
def index():
    """Serve the frontend page."""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>StreamHub - Movies & Series</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; }
        .container { max-width: 1400px; margin: 0 auto; }
        header { text-align: center; color: white; margin-bottom: 40px; animation: fadeIn 0.8s ease-in; }
        h1 { font-size: 3rem; margin-bottom: 10px; text-shadow: 2px 2px 4px rgba(0,0,0,0.3); }
        .subtitle { font-size: 1.2rem; opacity: 0.9; }
        .filters { display: flex; justify-content: center; gap: 15px; margin-bottom: 30px; flex-wrap: wrap; }
        .filter-btn { padding: 12px 30px; border: none; border-radius: 25px; background: white; color: #667eea; font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.3s ease; box-shadow: 0 4px 15px rgba(0,0,0,0.2); }
        .filter-btn:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,0,0,0.3); }
        .filter-btn.active { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .loading { text-align: center; color: white; font-size: 1.5rem; margin: 50px 0; }
        .spinner { border: 4px solid rgba(255,255,255,0.3); border-top: 4px solid white; border-radius: 50%; width: 50px; height: 50px; animation: spin 1s linear infinite; margin: 20px auto; }
        .content-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 30px; margin-bottom: 40px; }
        .content-card { background: white; border-radius: 15px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.3); transition: all 0.3s ease; cursor: pointer; animation: fadeInUp 0.6s ease-out; }
        .content-card:hover { transform: translateY(-10px); box-shadow: 0 15px 40px rgba(0,0,0,0.4); }
        .card-image { width: 100%; height: 400px; object-fit: cover; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
        .card-content { padding: 20px; }
        .card-type { display: inline-block; padding: 5px 15px; border-radius: 20px; font-size: 0.85rem; font-weight: 600; margin-bottom: 10px; }
        .type-movie { background: #ff6b6b; color: white; }
        .type-series { background: #4ecdc4; color: white; }
        .card-title { font-size: 1.4rem; font-weight: 700; color: #333; margin-bottom: 15px; }
        .card-links { margin-top: 15px; }
        .link-count { color: #666; font-size: 0.9rem; margin-bottom: 10px; }
        .watch-btn { display: block; width: 100%; padding: 12px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 8px; font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.3s ease; }
        .watch-btn:hover { transform: scale(1.05); box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4); }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 1000; justify-content: center; align-items: center; padding: 20px; }
        .modal.active { display: flex; }
        .modal-content { background: white; border-radius: 20px; max-width: 600px; width: 100%; max-height: 80vh; overflow-y: auto; padding: 30px; position: relative; animation: modalSlideIn 0.3s ease-out; }
        .close-btn { position: absolute; top: 15px; right: 15px; background: #ff6b6b; color: white; border: none; width: 35px; height: 35px; border-radius: 50%; font-size: 1.5rem; cursor: pointer; transition: all 0.3s ease; }
        .close-btn:hover { transform: rotate(90deg); background: #ff5252; }
        .modal-title { font-size: 2rem; color: #333; margin-bottom: 20px; padding-right: 40px; }
        .links-list { list-style: none; }
        .link-item { margin-bottom: 15px; }
        .link-btn { display: block; width: 100%; padding: 15px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; text-decoration: none; border-radius: 10px; font-weight: 600; text-align: center; transition: all 0.3s ease; }
        .link-btn:hover { transform: translateX(10px); box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4); }
        .empty-state { text-align: center; color: white; padding: 60px 20px; }
        .empty-icon { font-size: 5rem; margin-bottom: 20px; }
        .empty-text { font-size: 1.5rem; margin-bottom: 10px; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        @keyframes fadeInUp { from { opacity: 0; transform: translateY(30px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes modalSlideIn { from { opacity: 0; transform: scale(0.9); } to { opacity: 1; transform: scale(1); } }
        @media (max-width: 768px) { h1 { font-size: 2rem; } .content-grid { grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px; } .card-image { height: 300px; } }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🎬 StreamHub</h1>
            <p class="subtitle">Your Ultimate Movie & Series Collection</p>
        </header>
        <div class="filters">
            <button class="filter-btn active" data-filter="all">All Content</button>
            <button class="filter-btn" data-filter="Movie">Movies</button>
            <button class="filter-btn" data-filter="Series">Web Series</button>
        </div>
        <div id="loading" class="loading"><div class="spinner"></div><p>Loading awesome content...</p></div>
        <div id="content-grid" class="content-grid"></div>
        <div id="empty-state" class="empty-state" style="display: none;"><div class="empty-icon">📭</div><p class="empty-text">No content available yet</p><p>Use the Telegram bot to add your first movie or series!</p></div>
    </div>
    <div id="modal" class="modal"><div class="modal-content"><button class="close-btn" onclick="closeModal()">&times;</button><h2 class="modal-title" id="modal-title"></h2><ul class="links-list" id="links-list"></ul></div></div>
    <script>
        let allContent=[],currentFilter='all';const API_URL=window.location.origin+'/api/content';
        async function fetchContent(){try{const r=await fetch(API_URL),d=await r.json();d.success?allContent=d.data:showEmptyState(),displayContent()}catch(e){console.error('Error:',e),showEmptyState()}finally{document.getElementById('loading').style.display='none'}}
        function displayContent(){const g=document.getElementById('content-grid'),e=document.getElementById('empty-state');let f=currentFilter==='all'?allContent:allContent.filter(i=>i.type===currentFilter);if(f.length===0){g.innerHTML='';e.style.display='block';return}e.style.display='none';g.innerHTML=f.map(i=>`<div class="content-card" onclick="openModal('${i._id}')"><img class="card-image" src="${i.thumbnail_url}" alt="${i.title}" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22280%22 height=%22400%22%3E%3Crect fill=%22%23667eea%22 width=%22280%22 height=%22400%22/%3E%3Ctext x=%2250%25%22 y=%2250%25%22 fill=%22white%22 text-anchor=%22middle%22 dy=%22.3em%22 font-size=%2224%22%3E${i.title}%3C/text%3E%3C/svg%3E'"><div class="card-content"><span class="card-type type-${i.type.toLowerCase()}">${i.type}</span><h3 class="card-title">${i.title}</h3><div class="card-links"><p class="link-count">📺 ${i.links.length} ${i.links.length===1?'Link':'Links'} Available</p><button class="watch-btn">Watch Now</button></div></div></div>`).join('')}
        function showEmptyState(){document.getElementById('content-grid').innerHTML='';document.getElementById('empty-state').style.display='block'}
        function openModal(id){const c=allContent.find(i=>i._id===id);if(!c)return;document.getElementById('modal-title').textContent=c.title;document.getElementById('links-list').innerHTML=c.links.map(l=>`<li class="link-item"><a href="${l.url}" target="_blank" class="link-btn">${l.episode_title}</a></li>`).join('');document.getElementById('modal').classList.add('active')}
        function closeModal(){document.getElementById('modal').classList.remove('active')}
        document.querySelectorAll('.filter-btn').forEach(b=>{b.addEventListener('click',()=>{document.querySelectorAll('.filter-btn').forEach(x=>x.classList.remove('active'));b.classList.add('active');currentFilter=b.dataset.filter;displayContent()})});
        document.getElementById('modal').addEventListener('click',e=>{if(e.target.id==='modal')closeModal()});
        document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});
        fetchContent();
    </script>
</body>
</html>'''

@app.route('/health')
def health():
    """Koyeb health check endpoint."""
    try:
        if content_collection is not None:
            client.admin.command('ping')
            return jsonify({"status": "healthy", "database": "connected"}), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
    
    return jsonify({"status": "unhealthy", "database": "disconnected"}), 503

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
    """REST API endpoint for the frontend to fetch content."""
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503

    try:
        # Optional query parameters for filtering
        content_type = request.args.get('type')
        limit = int(request.args.get('limit', 100))
        
        query = {}
        if content_type:
            query['type'] = content_type
        
        content_cursor = content_collection.find(query).sort("created_at", -1).limit(limit)
        
        content_list = []
        for doc in content_cursor:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            content_list.append(doc)
            
        return jsonify({
            "success": True,
            "count": len(content_list),
            "data": content_list
        }), 200
    except Exception as e:
        logger.error(f"API Fetch Error: {e}")
        return jsonify({"success": False, "error": "Failed to retrieve content."}), 500

@app.route('/api/content/<content_id>', methods=['GET'])
def get_content_by_id(content_id):
    """Get a single content item by ID."""
    if content_collection is None:
        return jsonify({"error": "Database not configured."}), 503
    
    try:
        from bson import ObjectId
        doc = content_collection.find_one({"_id": ObjectId(content_id)})
        
        if doc:
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            return jsonify({"success": True, "data": doc}), 200
        else:
            return jsonify({"success": False, "error": "Content not found"}), 404
    except Exception as e:
        logger.error(f"API Fetch Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# --- 7. APPLICATION STARTUP ---

@app.before_request
def before_first_request():
    """Initialize connections before handling requests."""
    if content_collection is None:
        init_mongodb()

if __name__ == '__main__':
    logger.info("Starting Telegram Bot Application...")
    
    # Initialize MongoDB
    if init_mongodb():
        logger.info("MongoDB initialized successfully")
    else:
        logger.warning("MongoDB initialization failed - bot will have limited functionality")
    
    # Set webhook if APP_URL is provided
    if APP_URL:
        set_webhook()
    else:
        logger.warning("APP_URL not set - webhook not configured")
    
    # Start Flask app
    logger.info(f"Starting Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
