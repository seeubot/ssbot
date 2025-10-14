import os
import io
import logging
import cv2
import requests
from datetime import datetime
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie")
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# MongoDB Connection
def get_mongo_client():
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        logger.info("MongoDB connected successfully")
        return client
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        return None

mongo_client = get_mongo_client()
db = mongo_client['video_thumbnails'] if mongo_client else None
thumbnails_collection = db['thumbnails'] if db else None

# Initialize MongoDB indexes
if thumbnails_collection:
    thumbnails_collection.create_index("telegram_file_id", unique=True, sparse=True)
    thumbnails_collection.create_index("created_at")

def upload_to_catbox(image_bytes, filename="thumbnail.jpg"):
    """Upload image to Catbox (files.catbox.moe) and return the URL"""
    try:
        files = {'reqtype': (None, 'fileupload'), 'fileToUpload': (filename, image_bytes, 'image/jpeg')}
        response = requests.post("https://catbox.moe/user/api.php", files=files, timeout=15)
        
        if response.status_code == 200 and response.text.startswith('https://'):
            url = response.text.strip()
            logger.info(f"Uploaded to Catbox: {url}")
            return url
        else:
            logger.error(f"Catbox upload failed: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Catbox upload error: {e}")
        return None

def extract_thumbnail(video_path, timestamp_seconds=5):
    """Extract thumbnail from video at specified timestamp"""
    cap = None
    try:
        cap = cv2.VideoCapture(video_path)
        
        if not cap.isOpened():
            logger.error(f"Failed to open video: {video_path}")
            return None
        
        # Get FPS and total frames
        fps = int(cap.get(cv2.CAP_PROP_FPS)) or 1
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        # Calculate frame to extract
        frame_num = min(int(fps * timestamp_seconds), total_frames - 1)
        
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, frame = cap.read()
        
        if not ret or frame is None:
            logger.error("Failed to extract frame")
            return None
        
        # Resize and optimize
        height, width = frame.shape[:2]
        if width > 1920:
            scale = 1920 / width
            frame = cv2.resize(frame, (1920, int(height * scale)), interpolation=cv2.INTER_AREA)
        
        # Encode to JPEG
        _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return io.BytesIO(buffer.tobytes())
    
    except Exception as e:
        logger.error(f"Thumbnail extraction error: {e}")
        return None
    finally:
        if cap:
            cap.release()

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle video messages"""
    try:
        user_id = update.effective_user.id
        message = update.message
        
        # Determine if it's video or document
        if message.video:
            file_info = message.video
            file_type = "video"
        elif message.document and message.document.mime_type and message.document.mime_type.startswith('video'):
            file_info = message.document
            file_type = "document"
        else:
            await update.message.reply_text("❌ Please send a valid video file.")
            return
        
        # Check file size
        if file_info.file_size > MAX_FILE_SIZE:
            await update.message.reply_text(f"❌ File size exceeds 200MB limit. Current: {file_info.file_size / (1024*1024):.2f}MB")
            return
        
        # Check if thumbnail already exists in MongoDB
        existing = thumbnails_collection.find_one({"telegram_file_id": file_info.file_id}) if thumbnails_collection else None
        if existing:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🖼️ View Thumbnail", url=existing['catbox_url'])]])
            await update.message.reply_text(f"✅ Found cached thumbnail!\n🔗 {existing['catbox_url']}", reply_markup=keyboard)
            return
        
        # Download and process video
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
        
        video_file = await context.bot.get_file(file_info.file_id)
        video_bytes = await video_file.download_as_bytearray()
        
        # Save temporarily
        temp_path = f"/tmp/{file_info.file_id}.mp4"
        with open(temp_path, 'wb') as f:
            f.write(video_bytes)
        
        logger.info(f"Processing video: {file_info.file_name or 'unknown'} ({len(video_bytes) / (1024*1024):.2f}MB)")
        
        # Extract thumbnail
        thumbnail = extract_thumbnail(temp_path)
        if not thumbnail:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            await update.message.reply_text("❌ Failed to extract thumbnail from video.")
            return
        
        # Upload to Catbox
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
        catbox_url = upload_to_catbox(thumbnail.getvalue(), f"{file_info.file_id}.jpg")
        
        if not catbox_url:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            await update.message.reply_text("❌ Failed to upload thumbnail.")
            return
        
        # Save to MongoDB
        if thumbnails_collection:
            thumbnail_doc = {
                "telegram_file_id": file_info.file_id,
                "user_id": user_id,
                "filename": file_info.file_name or "video",
                "file_size": file_info.file_size,
                "catbox_url": catbox_url,
                "created_at": datetime.utcnow()
            }
            thumbnails_collection.insert_one(thumbnail_doc)
        
        # Send response
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🖼️ View Thumbnail", url=catbox_url)]])
        await update.message.reply_photo(photo=thumbnail.getvalue(),
                                        caption=f"✅ Thumbnail extracted!\n📸 Size: {file_info.file_size / (1024*1024):.2f}MB\n🔗 {catbox_url}",
                                        reply_markup=keyboard)
        
        # Cleanup
        if os.path.exists(temp_path):
            os.remove(temp_path)
    
    except Exception as e:
        logger.error(f"Video handler error: {e}")
        await update.message.reply_text(f"❌ Error processing video: {str(e)[:100]}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    welcome_text = """👋 *Video Thumbnail Extractor Bot*

Send me any video file and I'll extract a thumbnail and host it on Catbox!

*Features:*
• 🎬 Supports all video formats (MP4, MKV, AVI, MOV, WebM, etc.)
• 🖼️ Smart thumbnail extraction
• 🌐 Free hosting on Catbox
• 💾 Caching system with MongoDB
• ⚡ Fast processing

*Limits:*
• Max file size: 200MB
• Auto-caching for repeated videos

Just send a video and I'll do the rest! 🚀"""
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command"""
    help_text = """*How to use:*

1️⃣ Send any video file
2️⃣ I'll extract a thumbnail
3️⃣ The thumbnail is hosted on Catbox
4️⃣ Share the link anywhere!

*Supported formats:*
MP4, MKV, AVI, MOV, FLV, WebM, WMV, 3GP, and more

*Tips:*
• Larger videos take longer to process
• Thumbnails are extracted at 5 seconds by default
• Your data is cached for faster future access"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Error handler"""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

def main():
    """Start the bot"""
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not found in environment variables")
        return
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_video))
    app.add_error_handler(error_handler)
    
    logger.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
