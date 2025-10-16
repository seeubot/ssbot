import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS # New Import for Cross-Origin Access
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
CORS(app) # Enable CORS for all domains to allow external frontend access

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

def fetch_and_send_content_list(chat_id):
    """Fetches the latest content and sends a summary list to the user."""
    if content_collection is None:
        send_message(chat_id, "❌ Error: Database connection is unavailable.")
        return

    try:
        # Fetch the latest 10 content items
        content_cursor = content_collection.find().sort("created_at", -1).limit(10)
        
        content_list = []
        for doc in content_cursor:
            # Format a concise summary for Telegram
            title = doc.get('title', 'Untitled')
            content_type = doc.get('type', 'Item')
            links_count = len(doc.get('links', []))
            
            # Using Markdown formatting for Telegram
            content_list.append(f"*{title}* (`{content_type}`)\n- Links: {links_count}")

        if content_list:
            header = "📦 *Latest 10 Content Items:*\n"
            message = header + "\n" + ("\n---\n".join(content_list))
        else:
            message = "📭 No content found in the database. Use `/start` to add one!"
            
        send_message(chat_id, message)

    except Exception as e:
        logger.error(f"Error viewing content: {e}")
        send_message(chat_id, "❌ An error occurred while fetching content.")


def handle_text_message(chat_id, text):
    """Handle text messages based on current state."""
    state = USER_STATE.get(chat_id, {}).get('state', STATE_START)
    content_data = USER_STATE.get(chat_id, {}).get('content_data', {})

    if text.startswith('/start'):
        start_new_upload(chat_id)
        return
    
    # New command to view content list
    if text.startswith('/view'):
        fetch_and_send_content_list(chat_id)
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
        send_message(chat_id, "Please use the /start command to begin a new content upload, or `/view` to see the latest content.")

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
    """Serve the advanced frontend page with security measures and Telegram link."""
    # The advanced UI from the previous step is embedded here.
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>StreamHub - Watch Free Online</title>
    <!-- Load Tailwind CSS CDN for modern styling -->
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        /* Custom styles for modern aesthetics and animations */
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@100..900&display=swap');
        
        * {
            box-sizing: border-box;
        }

        body {
            font-family: 'Inter', sans-serif;
            background-color: #0d1117; /* Dark background */
        }

        .gradient-text {
            background: linear-gradient(90deg, #6366f1 0%, #a855f7 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .card-bg {
            background-color: #161b22;
        }

        .spinner {
            border-top-color: #a855f7;
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        /* Responsive height for placeholder image */
        .card-image-placeholder {
            width: 100%;
            height: 400px; 
            display: flex;
            align-items: center;
            justify-content: center;
            background: #21262d; 
            color: #8b949e;
            font-size: 1.5rem;
            text-align: center;
        }

        .modal-bg {
            background-color: rgba(0, 0, 0, 0.9);
        }

        .modal-content-custom {
            background-color: #1f2937;
        }
    </style>
</head>
<body class="min-h-screen p-4 sm:p-8">
    <div class="max-w-7xl mx-auto">
        
        <!-- Header -->
        <header class="text-center mb-10 pt-4 sm:pt-8 animate-in fade-in zoom-in duration-500">
            <h1 class="text-5xl sm:text-7xl font-extrabold mb-2 gradient-text">
                Advance Request
            </h1>
            <p class="text-xl sm:text-2xl text-gray-400 font-light">
                Watch Free Online
            </p>
        </header>

        <!-- Search Bar -->
        <div class="flex justify-center mb-12">
            <div class="relative w-full max-w-2xl">
                <input 
                    type="text" 
                    id="search-input" 
                    placeholder="Search for a movie or series..." 
                    oninput="handleSearch()"
                    class="w-full py-4 pl-12 pr-6 text-lg text-white card-bg rounded-xl shadow-2xl focus:ring-4 focus:ring-indigo-500 focus:border-indigo-500 border-2 border-transparent transition duration-300"
                >
                <svg class="absolute left-4 top-1/2 transform -translate-y-1/2 h-6 w-6 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
            </div>
        </div>

        <!-- Loading State -->
        <div id="loading" class="text-center py-20 text-white transition-opacity duration-500">
            <div class="spinner w-12 h-12 mx-auto rounded-full border-4 border-gray-700"></div>
            <p class="mt-4 text-lg text-gray-400">Loading your ultimate collection...</p>
        </div>

        <!-- Content Grid -->
        <div id="content-grid" class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-6">
            <!-- Content cards will be injected here -->
        </div>

        <!-- Empty State -->
        <div id="empty-state" class="text-center text-white py-20 hidden">
            <div class="text-7xl mb-4">🤷‍♂️</div>
            <p class="text-2xl font-semibold mb-2">Nothing found.</p>
            <p class="text-gray-400">Try adjusting your search term or check back later!</p>
        </div>
    </div>

    <!-- Watch Links Modal -->
    <div id="modal" class="fixed inset-0 modal-bg z-50 flex items-center justify-center p-4 hidden transition duration-300 ease-out">
        <div class="modal-content-custom rounded-2xl max-w-lg w-full max-h-[90vh] overflow-y-auto p-6 relative shadow-2xl transition-all duration-300">
            <button class="close-btn absolute top-4 right-4 bg-red-600 hover:bg-red-700 text-white w-8 h-8 rounded-full text-xl leading-none transition-transform duration-300 transform hover:rotate-90" onclick="closeModal()">&times;</button>
            <h2 class="text-3xl font-bold text-white mb-6 pr-8" id="modal-title"></h2>
            
            <ul class="space-y-4" id="links-list">
                <!-- Links will be injected here -->
            </ul>
        </div>
    </div>

    <!-- Join Telegram Button (Fixed/Sticky) -->
    <a href="https://t.me/+oOdTY-zbwCY3MzA1" target="_blank" class="fixed bottom-6 right-6 bg-cyan-500 hover:bg-cyan-600 text-white font-bold py-3 px-6 rounded-full shadow-2xl transition duration-300 transform hover:scale-105 flex items-center z-40">
        <!-- Inline Telegram Icon (SVG) -->
        <svg xmlns="http://www.w3.org/2000/svg" class="w-6 h-6 mr-2" viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.4l-14.7 6.4c-.6.3-.6.7 0 1l3.5 1.1 9.1-5.7c.4-.3.7-.1.4.2l-7.3 6.6-1.9 4.8c-.2.6-.5.7-.9.4l-3.3-2.1c-.5-.3-.4-.5 0-.7l16.1-9.5c.6-.3.6-.6 0-.9z"/></svg>
        Join Telegram
    </a>

    <script>
        // --- SECURITY/ANTI-INSPECTION LOGIC ---
        
        const TELEGRAM_URL = "https://t.me/+oOdTY-zbwCY3MzA1";
        
        /**
         * Redirects the user to the Telegram channel.
         */
        function redirectTelegram() {
            window.location.href = TELEGRAM_URL;
        }

        // 1. Disable Right-Click (Context Menu)
        document.addEventListener('contextmenu', function(e) {
            e.preventDefault();
            console.log("Right-click blocked. Redirecting...");
            redirectTelegram();
        });

        // 2. Disable Keyboard Shortcuts (F12, Ctrl/Cmd + Shift + I/J/C/U)
        document.addEventListener('keydown', function(e) {
            // F12 key check
            if (e.key === 'F12') {
                e.preventDefault();
                console.log("F12 blocked. Redirecting...");
                redirectTelegram();
            }

            // Ctrl/Cmd Key combinations
            if (e.ctrlKey || e.metaKey) { // metaKey is for Cmd on Mac
                const key = e.key.toLowerCase();
                // Ctrl+Shift+I (Developer Tools), Ctrl+Shift+J (Console), Ctrl+Shift+C (Element Selector), Ctrl+U (View Source)
                if ((e.shiftKey && (key === 'i' || key === 'j' || key === 'c')) || key === 'u') {
                    e.preventDefault();
                    console.log("Keyboard shortcut blocked. Redirecting...");
                    redirectTelegram();
                }
            }
        });

        // --- APPLICATION LOGIC ---

        let allContent = [];
        // API endpoint - will be automatically set to your Koyeb URL
        const API_URL = window.location.origin + '/api/content';

        /**
         * Fetches all content from the API.
         */
        async function fetchContent() {
            try {
                const response = await fetch(API_URL);
                const result = await response.json();
                
                if (result.success && Array.isArray(result.data)) {
                    allContent = result.data;
                    displayContent();
                } else {
                    showEmptyState("No content available yet. Use the Telegram bot to add your first movie or series!");
                }
            } catch (error) {
                console.error('Error fetching content:', error);
                showEmptyState("Failed to load content. Check your network connection or API.");
            } finally {
                // Hide loading spinner
                document.getElementById('loading').classList.add('hidden');
            }
        }

        /**
         * Filters content based on the search input and renders the grid.
         */
        function displayContent() {
            const grid = document.getElementById('content-grid');
            const emptyState = document.getElementById('empty-state');
            const searchTerm = document.getElementById('search-input').value.toLowerCase().trim();
            
            // Filter logic based on title
            const filteredContent = allContent.filter(item => 
                item.title && item.title.toLowerCase().includes(searchTerm)
            );

            if (filteredContent.length === 0) {
                grid.innerHTML = '';
                emptyState.classList.remove('hidden');
                return;
            }

            emptyState.classList.add('hidden');
            
            grid.innerHTML = filteredContent.map(item => {
                // Determine card color based on type
                const typeClass = item.type === 'Movie' 
                    ? 'bg-red-600' 
                    : 'bg-green-500';
                
                // Placeholder for image error
                const fallbackImage = 'https://placehold.co/280x400/21262d/8b949e?text=' + encodeURIComponent(item.title || 'No Title');

                return `
                    <div class="content-card card-bg rounded-xl overflow-hidden shadow-xl hover:shadow-2xl transform hover:-translate-y-2 transition duration-300 cursor-pointer group" onclick="openModal('${item._id}')">
                        <div class="relative">
                            <img class="w-full h-96 object-cover transition-opacity duration-300 group-hover:opacity-80" 
                                 src="${item.thumbnail_url}" 
                                 alt="${item.title}"
                                 onerror="this.onerror=null;this.src='${fallbackImage}';">
                            
                            <span class="absolute top-3 left-3 px-3 py-1 text-sm font-semibold text-white rounded-full ${typeClass} shadow-md">
                                ${item.type}
                            </span>
                        </div>
                        <div class="p-5">
                            <h3 class="text-xl font-bold text-white mb-2 truncate">${item.title}</h3>
                            <p class="text-gray-400 text-sm mb-4">
                                🔗 ${item.links.length} ${item.links.length === 1 ? 'Link' : 'Links'} Available
                            </p>
                            <button class="w-full py-3 bg-indigo-600 text-white font-semibold rounded-lg hover:bg-indigo-700 transition duration-300 shadow-lg hover:shadow-xl">
                                Watch Now
                            </button>
                        </div>
                    </div>
                `;
            }).join('');
        }

        /**
         * Updates the content display when the user types in the search bar.
         */
        let searchTimeout;
        function handleSearch() {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => {
                displayContent();
            }, 300); // Debounce for performance
        }

        /**
         * Updates the empty state message.
         */
        function showEmptyState(message) {
            const grid = document.getElementById('content-grid');
            const emptyState = document.getElementById('empty-state');
            grid.innerHTML = '';
            // Basic parsing of message for multi-line display
            const parts = message.split('.');
            emptyState.querySelector('p:nth-child(2)').textContent = parts[0] || 'No content found.';
            emptyState.querySelector('p:nth-child(3)').textContent = parts[1] ? parts[1].trim() : '';
            emptyState.classList.remove('hidden');
        }


        /**
         * Opens the modal with links for the selected content.
         */
        function openModal(contentId) {
            const content = allContent.find(item => item._id === contentId);
            if (!content) return;

            document.getElementById('modal-title').textContent = content.title;
            
            const linksList = document.getElementById('links-list');
            linksList.innerHTML = content.links.map(link => `
                <li class="link-item">
                    <a href="${link.url}" target="_blank" class="block py-4 px-6 bg-gray-700 text-white font-medium rounded-xl hover:bg-indigo-600 transition duration-300 text-center shadow-md">
                        ${link.episode_title}
                    </a>
                </li>
            `).join('');

            document.getElementById('modal').classList.remove('hidden');
            document.getElementById('modal').classList.add('flex');
        }

        /**
         * Closes the modal.
         */
        function closeModal() {
            document.getElementById('modal').classList.add('hidden');
            document.getElementById('modal').classList.remove('flex');
        }

        // Close modal on outside click
        document.getElementById('modal').addEventListener('click', (e) => {
            if (e.target.id === 'modal') {
                closeModal();
            }
        });

        // Close modal on Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeModal();
            }
        });

        // Load content on page load
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
        
        # Sort by creation date descending
        content_cursor = content_collection.find(query).sort("created_at", -1).limit(limit)
        
        content_list = []
        for doc in content_cursor:
            # MongoDB's ObjectId is not JSON serializable, convert to string
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
    # Ensure MongoDB is initialized if it wasn't during startup (e.g., in a serverless context)
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

