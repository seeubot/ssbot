import os
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS # Import for Cross-Origin Access
from pymongo import MongoClient
from bson import ObjectId # Required for ID management
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
        
        db_name = os.environ.get("DB_NAME", "streamhub")
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
CORS(app) # Enable CORS for all domains

# Global state to track multi-step conversation
USER_STATE = {}

# FSM States
STATE_START = 'START'
STATE_WAITING_FOR_TYPE = 'WAITING_FOR_TYPE'
STATE_WAITING_FOR_TITLE = 'WAITING_FOR_TITLE'
STATE_WAITING_FOR_THUMBNAIL = 'WAITING_FOR_THUMBNAIL'
STATE_WAITING_FOR_TAGS = 'WAITING_FOR_TAGS' # New state for similar content key
STATE_WAITING_FOR_LINK_TITLE = 'WAITING_FOR_LINK_TITLE'
STATE_WAITING_FOR_LINK_URL = 'WAITING_FOR_LINK_URL'
STATE_CONFIRM_LINK = 'CONFIRM_LINK'

STATE_WAITING_FOR_EDIT_FIELD = 'WAITING_FOR_EDIT_FIELD'
STATE_WAITING_FOR_NEW_VALUE = 'WAITING_FOR_NEW_VALUE'
STATE_CONFIRM_DELETE = 'CONFIRM_DELETE'

# --- 3. CORE BOT FUNCTIONS (CRUD) ---

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
            "tags": [t.strip().lower() for t in content_data.get('tags', '').split(',') if t.strip()], # Save as array of lowercase strings
            "links": content_data.get('links', []),
            "created_at": datetime.utcnow()
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

# --- 4. CONVERSATION HANDLERS (FSM) ---

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
            
            # Format a concise summary
            summary = f"*{i+1}. {title}* (`{content_type}`)"
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

    if state == STATE_WAITING_FOR_TITLE:
        content_data['title'] = text.strip()
        ask_for_thumbnail(chat_id)

    elif state == STATE_WAITING_FOR_THUMBNAIL:
        if text.startswith('http'):
            content_data['thumbnail_url'] = text.strip()
            ask_for_tags(chat_id)
        else:
            send_message(chat_id, "Please send a *public URL* starting with `http` or `https`.")

    elif state == STATE_WAITING_FOR_TAGS:
        content_data['tags'] = text.strip()
        ask_for_link_title(chat_id)
    
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
    
    elif state == STATE_WAITING_FOR_NEW_VALUE:
        content_id = content_data.get('edit_id')
        field = content_data.get('edit_field')
        
        if content_id and field:
            update_fields = {field: text.strip()}
            if update_content(content_id, update_fields):
                send_message(chat_id, f"🎉 *Success!* Content ID `{content_id}`: Field *{field}* updated!")
            else:
                send_message(chat_id, "❌ Error: Update failed.")
        else:
            send_message(chat_id, "❌ Error: Lost state for update. Please start editing again with `/edit`.")
            
        USER_STATE[chat_id]['state'] = STATE_START # Reset state
        USER_STATE[chat_id]['content_data'] = {'links': []}
        return

    elif state == STATE_START:
        send_message(chat_id, "Please use the `/add` command to begin a new upload, `/view` to see content, or `/edit` to manage existing items.")

def handle_callback_query(chat_id, data):
    """Handle inline keyboard button presses."""
    state = USER_STATE.get(chat_id, {}).get('state')
    parts = data.split('_')
    action = parts[0]
    content_data = USER_STATE.get(chat_id, {}).get('content_data', {})

    if action == 'type':
        content_type = parts[1]
        USER_STATE[chat_id]['content_data']['type'] = content_type
        ask_for_title(chat_id)
        
    elif action == 'add':
        if parts[1] == 'Yes':
            ask_for_link_title(chat_id)
        elif parts[1] == 'No':
            finish_upload(chat_id)
    
    # --- Edit/Delete Flow ---
    elif action == 'delete':
        content_id = parts[2]
        if parts[1] == 'confirm':
            keyboard = {
                'inline_keyboard': [
                    [{'text': '✅ YES, Delete it!', 'callback_data': f'delete_execute_{content_id}'}],
                    [{'text': '❌ No, keep it', 'callback_data': 'edit_cancel'}]
                ]
            }
            send_message(chat_id, f"⚠️ *Are you sure you want to delete content ID* `{content_id}`?", reply_markup=keyboard)

        elif parts[1] == 'execute':
            if delete_content(content_id):
                send_message(chat_id, f"🗑️ *Deleted!* Content ID `{content_id}` removed successfully.")
            else:
                send_message(chat_id, f"❌ Error: Could not delete content ID `{content_id}`.")
            USER_STATE[chat_id]['state'] = STATE_START
    
    elif action == 'edit':
        if parts[1] == 'start':
            content_id = parts[2]
            USER_STATE[chat_id] = {'state': STATE_WAITING_FOR_EDIT_FIELD, 'content_data': {'edit_id': content_id}}
            
            keyboard = {
                'inline_keyboard': [
                    [{'text': '✏️ Title', 'callback_data': f'edit_field_title'}],
                    [{'text': '🖼️ Thumbnail URL', 'callback_data': f'edit_field_thumbnail_url'}],
                    [{'text': '🏷️ Tags (Keywords)', 'callback_data': f'edit_field_tags'}],
                    [{'text': '❌ Cancel', 'callback_data': 'edit_cancel'}]
                ]
            }
            send_message(chat_id, f"Content ID `{content_id}` selected.\n\nWhich field do you want to modify?", reply_markup=keyboard)

        elif parts[1] == 'field':
            field = parts[2]
            content_id = content_data.get('edit_id')
            
            if not content_id:
                send_message(chat_id, "❌ Error: Lost content ID. Please use `/edit` again.")
                USER_STATE[chat_id]['state'] = STATE_START
                return

            USER_STATE[chat_id]['state'] = STATE_WAITING_FOR_NEW_VALUE
            USER_STATE[chat_id]['content_data']['edit_field'] = field
            
            prompt_map = {
                'title': "Enter the *new Title*:",
                'thumbnail_url': "Enter the *new Thumbnail URL* (must start with http/s):",
                'tags': "Enter the *new Tags* (comma-separated):"
            }
            send_message(chat_id, prompt_map.get(field, "Enter the new value:"))

        elif parts[1] == 'cancel':
            send_message(chat_id, "Edit cancelled.")
            USER_STATE[chat_id]['state'] = STATE_START

# --- 5. WEBHOOK SETUP ---
# ... (set_webhook function remains unchanged)

# --- 6. FLASK ROUTES ---

@app.route('/')
def index():
    """Serve the advanced frontend page with security measures and Telegram link."""
    # Updated HTML to support the new video player modal, similar content key, and security
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

        .modal-bg {
            background-color: rgba(0, 0, 0, 0.95);
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
                StreamHub
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
                    placeholder="Search for a video or series..." 
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

    <!-- 1. Links Selection Modal -->
    <div id="links-modal" class="fixed inset-0 modal-bg z-50 flex items-center justify-center p-4 hidden transition duration-300 ease-out">
        <div class="modal-content-custom rounded-2xl max-w-lg w-full max-h-[90vh] overflow-y-auto p-6 relative shadow-2xl transition-all duration-300">
            <button class="close-btn absolute top-4 right-4 bg-red-600 hover:bg-red-700 text-white w-8 h-8 rounded-full text-xl leading-none transition-transform duration-300 transform hover:rotate-90" onclick="closeLinksModal()">&times;</button>
            <h2 class="text-3xl font-bold text-white mb-6 pr-8" id="links-modal-title"></h2>
            <p class="text-gray-400 mb-6">Select a streaming source to begin watching:</p>
            
            <ul class="space-y-4" id="links-list">
                <!-- Links will be injected here -->
            </ul>
        </div>
    </div>

    <!-- 2. Video Player Modal -->
    <div id="player-modal" class="fixed inset-0 modal-bg z-50 flex items-center justify-center p-4 hidden transition duration-300 ease-out">
        <div class="modal-content-custom rounded-2xl max-w-5xl w-full max-h-[95vh] overflow-y-auto p-6 relative shadow-2xl transition-all duration-300">
            <button class="close-btn absolute top-4 right-4 bg-red-600 hover:bg-red-700 text-white w-8 h-8 rounded-full text-xl leading-none transition-transform duration-300 transform hover:rotate-90" onclick="closePlayerModal()">&times;</button>
            <h2 class="text-3xl font-bold text-white mb-6 pr-8" id="player-modal-title"></h2>

            <!-- Video Player -->
            <div class="relative w-full pb-[56.25%] mb-8 rounded-lg overflow-hidden shadow-2xl">
                <!-- Aspect Ratio Box (16:9) for responsiveness -->
                <iframe id="video-player-iframe" 
                        class="absolute top-0 left-0 w-full h-full" 
                        src="" 
                        scrolling="no" 
                        frameborder="0" 
                        allowfullscreen="true" 
                        webkitallowfullscreen="true" 
                        mozallowfullscreen="true">
                </iframe>

                <!-- Telegram Button over Player -->
                <a href="https://t.me/+oOdTY-zbwCY3MzA1" target="_blank" class="absolute top-4 left-4 bg-blue-500 hover:bg-blue-600 text-white p-2 rounded-full shadow-lg transition duration-300 transform hover:scale-110 flex items-center">
                    <svg xmlns="http://www.w3.org/2000/svg" class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.4l-14.7 6.4c-.6.3-.6.7 0 1l3.5 1.1 9.1-5.7c.4-.3.7-.1.4.2l-7.3 6.6-1.9 4.8c-.2.6-.5.7-.9.4l-3.3-2.1c-.5-.3-.4-.5 0-.7l16.1-9.5c.6-.3.6-.6 0-.9z"/></svg>
                </a>
            </div>

            <!-- Similar Content Section -->
            <div id="similar-content-section">
                <h3 class="text-xl font-bold text-gray-300 mb-4">Similar Content (Tags)</h3>
                <div id="tags-container" class="flex flex-wrap gap-2">
                    <!-- Tags will be injected here -->
                </div>
            </div>

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
        
        function redirectTelegram() {
            window.location.href = TELEGRAM_URL;
        }

        // 1. Disable Right-Click (Context Menu)
        document.addEventListener('contextmenu', function(e) {
            e.preventDefault();
            redirectTelegram();
        });

        // 2. Disable Keyboard Shortcuts (F12, Ctrl/Cmd + Shift + I/J/C/U)
        document.addEventListener('keydown', function(e) {
            if (e.key === 'F12') {
                e.preventDefault();
                redirectTelegram();
            }

            if (e.ctrlKey || e.metaKey) { 
                const key = e.key.toLowerCase();
                if ((e.shiftKey && (key === 'i' || key === 'j' || key === 'c')) || key === 'u') {
                    e.preventDefault();
                    redirectTelegram();
                }
            }
        });

        // --- APPLICATION LOGIC ---

        let allContent = [];
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
                    showEmptyState("No content available yet. Use the Telegram bot to add your first video or series!");
                }
            } catch (error) {
                console.error('Error fetching content:', error);
                showEmptyState("Failed to load content. Check your network connection or API.");
            } finally {
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
                const typeClass = item.type === 'Video' 
                    ? 'bg-red-600' 
                    : 'bg-green-500';
                
                const fallbackImage = 'https://placehold.co/280x400/21262d/8b949e?text=' + encodeURIComponent(item.title || 'No Title');

                return `
                    <div class="content-card card-bg rounded-xl overflow-hidden shadow-xl hover:shadow-2xl transform hover:-translate-y-2 transition duration-300 cursor-pointer group" onclick="openLinksModal('${item._id}')">
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

        let searchTimeout;
        function handleSearch() {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => {
                displayContent();
            }, 300); 
        }

        function showEmptyState(message) {
            const grid = document.getElementById('content-grid');
            const emptyState = document.getElementById('empty-state');
            grid.innerHTML = '';
            const parts = message.split('.');
            emptyState.querySelector('p:nth-child(2)').textContent = parts[0] || 'No content found.';
            emptyState.querySelector('p:nth-child(3)').textContent = parts[1] ? parts[1].trim() : '';
            emptyState.classList.remove('hidden');
        }


        /**
         * Opens the Links Modal for selection.
         */
        function openLinksModal(contentId) {
            const content = allContent.find(item => item._id === contentId);
            if (!content) return;

            document.getElementById('links-modal-title').textContent = content.title;
            
            const linksList = document.getElementById('links-list');
            
            // Store content data in the links modal element temporarily
            document.getElementById('links-modal').dataset.contentId = contentId;
            
            linksList.innerHTML = content.links.map(link => `
                <li class="link-item">
                    <button onclick="openPlayerModal('${link.url}', '${content.title}', '${content.tags.join(',')}')" 
                            class="block w-full py-4 px-6 bg-gray-700 text-white font-medium rounded-xl hover:bg-indigo-600 transition duration-300 text-center shadow-md">
                        ${link.episode_title}
                    </button>
                </li>
            `).join('');

            document.getElementById('links-modal').classList.remove('hidden');
            document.getElementById('links-modal').classList.add('flex');
        }

        /**
         * Closes the Links Modal.
         */
        function closeLinksModal() {
            document.getElementById('links-modal').classList.add('hidden');
            document.getElementById('links-modal').classList.remove('flex');
        }
        
        /**
         * Opens the Video Player Modal.
         */
        function openPlayerModal(streamUrl, contentTitle, tagsString) {
            closeLinksModal(); // Close the selection modal first

            document.getElementById('player-modal-title').textContent = contentTitle;
            document.getElementById('video-player-iframe').src = streamUrl;
            
            // Handle Similar Content (Tags)
            const tagsContainer = document.getElementById('tags-container');
            tagsContainer.innerHTML = ''; // Clear old tags

            const tags = tagsString.split(',').filter(t => t.trim() !== '');

            if (tags.length > 0) {
                tags.forEach(tag => {
                    tagsContainer.innerHTML += `
                        <span class="px-3 py-1 bg-gray-700 text-indigo-400 text-sm font-medium rounded-full cursor-pointer hover:bg-indigo-500 hover:text-white transition duration-200"
                            onclick="document.getElementById('search-input').value = '${tag.trim()}'; handleSearch(); closePlayerModal();">
                            #${tag.trim()}
                        </span>
                    `;
                });
            } else {
                 tagsContainer.innerHTML = '<p class="text-gray-500">No tags provided for this content.</p>';
            }

            document.getElementById('player-modal').classList.remove('hidden');
            document.getElementById('player-modal').classList.add('flex');
        }

        /**
         * Closes the Video Player Modal and resets the iframe src.
         */
        function closePlayerModal() {
            document.getElementById('player-modal').classList.add('hidden');
            document.getElementById('player-modal').classList.remove('flex');
            // Stop video playback by clearing the source
            document.getElementById('video-player-iframe').src = '';
        }

        // Universal Escape key listener for both modals
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                if (!document.getElementById('player-modal').classList.contains('hidden')) {
                    closePlayerModal();
                } else if (!document.getElementById('links-modal').classList.contains('hidden')) {
                    closeLinksModal();
                }
            }
        });
        
        // Close modals on outside click
        document.getElementById('links-modal').addEventListener('click', (e) => {
            if (e.target.id === 'links-modal') closeLinksModal();
        });
        document.getElementById('player-modal').addEventListener('click', (e) => {
            if (e.target.id === 'player-modal') closePlayerModal();
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

