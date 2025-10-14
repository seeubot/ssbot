import os
import io
import logging
import cv2
import requests
import tempfile
import asyncio
import concurrent.futures
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from datetime import timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ChatAction, ParseMode
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie")
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID")  # Channel/group ID for file storage
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB
MAX_THUMBNAILS = 5  # Maximum number of thumbnails to extract
THREAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=3)

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment Validation
def validate_environment():
    """Validate required environment variables"""
    required_vars = ["TELEGRAM_TOKEN", "STORAGE_CHANNEL_ID"]
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        raise ValueError(f"Missing environment variables: {missing}")
    
    # Validate MongoDB URI format
    if MONGO_URI and not MONGO_URI.startswith(('mongodb://', 'mongodb+srv://')):
        raise ValueError("Invalid MongoDB URI format")

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
db = mongo_client['video_thumbnails'] if mongo_client is not None else None
thumbnails_collection = db['thumbnails'] if db is not None else None
storage_collection = db['storage_messages'] if db is not None else None

# Initialize MongoDB indexes
if thumbnails_collection is not None:
    try:
        thumbnails_collection.create_index("telegram_file_id", unique=True, sparse=True)
        thumbnails_collection.create_index("user_id")
        thumbnails_collection.create_index("created_at")
        logger.info("MongoDB indexes created successfully")
    except Exception as e:
        logger.error(f"Failed to create MongoDB indexes: {e}")

if storage_collection is not None:
    try:
        storage_collection.create_index("storage_message_id")
        storage_collection.create_index("original_file_id")
        storage_collection.create_index("created_at")
        logger.info("Storage collection indexes created")
    except Exception as e:
        logger.error(f"Failed to create storage indexes: {e}")

# Rate Limiting
class RateLimiter:
    def __init__(self, max_requests: int, time_window: int):
        self.max_requests = max_requests
        self.time_window = time_window
        self.user_requests = defaultdict(list)
    
    def is_limited(self, user_id: int) -> bool:
        now = datetime.utcnow()
        window_start = now - timedelta(seconds=self.time_window)
        
        # Clean old requests
        self.user_requests[user_id] = [
            req_time for req_time in self.user_requests[user_id]
            if req_time > window_start
        ]
        
        # Check limit
        if len(self.user_requests[user_id]) >= self.max_requests:
            return True
        
        self.user_requests[user_id].append(now)
        return False

# Initialize rate limiter (15 requests per minute)
rate_limiter = RateLimiter(max_requests=15, time_window=60)

# Context manager for temporary files
class TemporaryVideoFile:
    def __init__(self, video_bytes, suffix='.mp4'):
        self.video_bytes = video_bytes
        self.suffix = suffix
        self.temp_path = None
    
    def __enter__(self):
        with tempfile.NamedTemporaryFile(suffix=self.suffix, delete=False) as f:
            f.write(self.video_bytes)
            self.temp_path = f.name
        return self.temp_path
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.temp_path and os.path.exists(self.temp_path):
            try:
                os.unlink(self.temp_path)
            except Exception as e:
                logger.error(f"Error deleting temp file {self.temp_path}: {e}")

async def store_video_in_channel(context, video_bytes, filename, user_id):
    """Store video file in storage channel and return message info"""
    try:
        # Send video to storage channel
        storage_message = await context.bot.send_video(
            chat_id=STORAGE_CHANNEL_ID,
            video=video_bytes,
            caption=f"Stored video from user {user_id}\nFilename: {filename}\nTime: {datetime.utcnow().isoformat()}",
            disable_notification=True
        )
        
        # Store reference in database
        if storage_collection is not None:
            storage_doc = {
                "storage_message_id": storage_message.message_id,
                "user_id": user_id,
                "filename": filename,
                "file_size": len(video_bytes),
                "original_file_id": storage_message.video.file_id if storage_message.video else storage_message.document.file_id,
                "created_at": datetime.utcnow()
            }
            storage_collection.insert_one(storage_doc)
        
        return {
            "message_id": storage_message.message_id,
            "file_id": storage_message.video.file_id if storage_message.video else storage_message.document.file_id,
            "success": True
        }
    except Exception as e:
        logger.error(f"Failed to store video in channel: {e}")
        return {"success": False, "error": str(e)}

async def get_video_from_storage(context, storage_message_id):
    """Retrieve video file from storage channel"""
    try:
        # Forward the message to get file access
        file_message = await context.bot.forward_message(
            chat_id=context._chat_id,
            from_chat_id=STORAGE_CHANNEL_ID,
            message_id=storage_message_id
        )
        
        # Get file info
        if file_message.video:
            file_info = file_message.video
        elif file_message.document:
            file_info = file_message.document
        else:
            return None
        
        # Download file
        video_file = await context.bot.get_file(file_info.file_id)
        video_bytes = await video_file.download_as_bytearray()
        
        return video_bytes
    except Exception as e:
        logger.error(f"Failed to get video from storage: {e}")
        return None

def get_video_info(video_path):
    """Get basic video information quickly"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 1
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps if fps > 0 else 0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        return {
            'fps': fps,
            'total_frames': total_frames,
            'duration': duration,
            'width': width,
            'height': height
        }
    finally:
        cap.release()

def extract_frame_at_timestamp(video_path, timestamp_seconds, frame_num=None):
    """Extract a single frame at specific timestamp or frame number"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 1
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        if frame_num is None:
            # Calculate frame number from timestamp
            frame_num = min(int(fps * timestamp_seconds), total_frames - 1)
        
        # Use faster seeking method
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, frame = cap.read()
        
        if not ret or frame is None:
            return None
        
        # Optimize frame processing
        height, width = frame.shape[:2]
        max_dimension = 1280
        
        if max(width, height) > max_dimension:
            if width > height:
                new_width = max_dimension
                new_height = int(height * (max_dimension / width))
            else:
                new_height = max_dimension
                new_width = int(width * (max_dimension / height))
            
            frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_LANCZOS4)
        
        # Convert BGR to RGB
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # Optimize JPEG encoding
        _, buffer = cv2.imencode('.jpg', frame, [
            cv2.IMWRITE_JPEG_QUALITY, 80,
            cv2.IMWRITE_JPEG_OPTIMIZE, 1
        ])
        
        return io.BytesIO(buffer.tobytes()) if buffer is not None else None
        
    except Exception as e:
        logger.error(f"Frame extraction error at {timestamp_seconds}s: {e}")
        return None
    finally:
        cap.release()

def extract_keyframes_fast(video_path, max_frames=5):
    """Extract keyframes using multiple strategies for best coverage"""
    video_info = get_video_info(video_path)
    if not video_info:
        return []
    
    duration = video_info['duration']
    total_frames = video_info['total_frames']
    fps = video_info['fps']
    
    if duration <= 0:
        return []
    
    frames_to_extract = []
    
    # Strategy: Extract frames at strategic timestamps
    timestamps = []
    
    # Always include beginning
    timestamps.append(0)
    
    # Include middle points for longer videos
    if duration > 10:
        timestamps.extend([duration * 0.25, duration * 0.5, duration * 0.75])
    
    # Include near end
    if duration > 5:
        timestamps.append(max(0, duration - 3))
    
    # Limit to max_frames
    timestamps = timestamps[:max_frames]
    
    # Extract frames
    for timestamp in timestamps:
        frame = extract_frame_at_timestamp(video_path, timestamp)
        if frame:
            frames_to_extract.append({
                'frame': frame,
                'timestamp': timestamp,
                'position': f"{int(timestamp)}s"
            })
    
    return frames_to_extract

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

async def upload_thumbnails_parallel(thumbnails):
    """Upload multiple thumbnails to Catbox in parallel"""
    loop = asyncio.get_event_loop()
    
    def upload_single(thumbnail_data, index):
        filename = f"thumbnail_{index+1}.jpg"
        return upload_to_catbox(thumbnail_data['frame'].getvalue(), filename)
    
    upload_tasks = []
    for i, thumb_data in enumerate(thumbnails):
        task = loop.run_in_executor(THREAD_POOL, upload_single, thumb_data, i)
        upload_tasks.append(task)
    
    results = await asyncio.gather(*upload_tasks, return_exceptions=True)
    
    # Process results
    uploaded_thumbnails = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"Upload failed for thumbnail {i+1}: {result}")
        elif result:
            uploaded_thumbnails.append({
                'url': result,
                'timestamp': thumbnails[i]['timestamp'],
                'position': thumbnails[i]['position']
            })
    
    return uploaded_thumbnails

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle video messages with storage channel support"""
    user_id = update.effective_user.id
    
    # Rate limiting
    if rate_limiter.is_limited(user_id):
        await update.message.reply_text("🚫 Rate limit exceeded. Please try again in a minute.")
        return
    
    try:
        message = update.message
        
        # Validate message type and get file info
        file_info = None
        file_type = None
        
        if message.video:
            file_info = message.video
            file_type = "video"
        elif message.document:
            # Check if it's a video file by MIME type or extension
            mime_type = message.document.mime_type or ""
            file_name = message.document.file_name or ""
            
            video_mimes = ['video/', 'application/octet-stream']
            video_extensions = ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.3gp', '.mpeg', '.mpg']
            
            is_video_mime = any(mime in mime_type for mime in video_mimes)
            is_video_extension = any(file_name.lower().endswith(ext) for ext in video_extensions)
            
            if is_video_mime or is_video_extension:
                file_info = message.document
                file_type = "document"
        
        if not file_info:
            await update.message.reply_text(
                "❌ Please send a valid video file.\n\n"
                "Supported formats: MP4, MKV, AVI, MOV, WMV, FLV, WebM, M4V, 3GP, MPEG, MPG"
            )
            return
        
        # Check file size
        if file_info.file_size > MAX_FILE_SIZE:
            size_mb = file_info.file_size / (1024 * 1024)
            await update.message.reply_text(
                f"❌ File size exceeds 200MB limit. Current: {size_mb:.2f}MB"
            )
            return
        
        # Check cache first
        if thumbnails_collection is not None:
            try:
                existing = thumbnails_collection.find_one({
                    "telegram_file_id": file_info.file_id
                })
                if existing and 'thumbnails' in existing:
                    caption = f"✅ Found {len(existing['thumbnails'])} cached thumbnails!"
                    await send_thumbnails_album(update, existing['thumbnails'], caption, file_info)
                    return
            except Exception as e:
                logger.error(f"Database query error: {e}")
        
        # Download video
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, 
            action=ChatAction.UPLOAD_PHOTO
        )
        
        video_file = await context.bot.get_file(file_info.file_id)
        video_bytes = await video_file.download_as_bytearray()
        
        # Store video in storage channel
        await update.message.reply_text("💾 Storing video in secure storage...")
        storage_result = await store_video_in_channel(
            context, video_bytes, file_info.file_name or "video", user_id
        )
        
        if not storage_result['success']:
            await update.message.reply_text("❌ Failed to store video. Please try again.")
            return
        
        # Process video for thumbnails
        with TemporaryVideoFile(video_bytes) as temp_path:
            logger.info(f"Processing video: {file_info.file_name or 'unknown'} "
                       f"({len(video_bytes) / (1024*1024):.2f}MB)")
            
            await update.message.reply_text("🔄 Extracting thumbnails from different timestamps...")
            
            # Extract multiple thumbnails
            thumbnails = extract_keyframes_fast(temp_path, MAX_THUMBNAILS)
            
            if not thumbnails:
                await update.message.reply_text("❌ Failed to extract thumbnails from video.")
                return
            
            logger.info(f"Extracted {len(thumbnails)} thumbnails, uploading to Catbox...")
            
            # Upload thumbnails in parallel
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, 
                action=ChatAction.UPLOAD_PHOTO
            )
            
            uploaded_thumbnails = await upload_thumbnails_parallel(thumbnails)
            
            if not uploaded_thumbnails:
                await update.message.reply_text("❌ Failed to upload thumbnails to Catbox.")
                return
            
            # Save to database with storage reference
            if thumbnails_collection is not None:
                try:
                    thumbnail_doc = {
                        "telegram_file_id": file_info.file_id,
                        "storage_message_id": storage_result['message_id'],
                        "storage_file_id": storage_result['file_id'],
                        "user_id": user_id,
                        "filename": file_info.file_name or "video",
                        "file_size": file_info.file_size,
                        "file_type": file_type,
                        "thumbnails": uploaded_thumbnails,
                        "thumbnail_count": len(uploaded_thumbnails),
                        "created_at": datetime.utcnow()
                    }
                    thumbnails_collection.insert_one(thumbnail_doc)
                    logger.info(f"Saved {len(uploaded_thumbnails)} thumbnails to database")
                except Exception as e:
                    logger.error(f"Failed to save thumbnails to database: {e}")
            
            # Send results
            caption = (f"✅ Extracted {len(uploaded_thumbnails)} thumbnails!\n"
                      f"📸 Video size: {file_info.file_size / (1024*1024):.2f}MB\n"
                      f"💾 Stored securely in archive\n"
                      f"🕒 From different timestamps")
            
            await send_thumbnails_album(update, uploaded_thumbnails, caption, file_info)
    
    except Exception as e:
        logger.error(f"Video handler error: {e}")
        await update.message.reply_text("❌ An error occurred while processing your video.")

async def send_thumbnails_album(update, thumbnails, caption, file_info):
    """Send multiple thumbnails as an album with captions"""
    try:
        # Create media group for all thumbnails
        media_group = []
        
        for i, thumb in enumerate(thumbnails, 1):
            media_group.append(
                InputMediaPhoto(
                    media=thumb['url'],
                    caption=f"Thumbnail {i}/{len(thumbnails)}\n📍 Position: {thumb['position']}\n🔗 {thumb['url']}"
                )
            )
        
        # Send as media group (max 10 items per group)
        for i in range(0, len(media_group), 10):
            chunk = media_group[i:i + 10]
            await update.message.reply_media_group(media=chunk)
        
        # Send summary message
        summary_text = (f"{caption}\n\n"
                       f"📁 **File Info:**\n"
                       f"• Name: `{file_info.file_name or 'Unknown'}`\n"
                       f"• Size: {file_info.file_size / (1024*1024):.2f}MB\n"
                       f"• Thumbnails: {len(thumbnails)} generated\n\n"
                       f"💡 *All thumbnails are hosted on Catbox and cached for fast access*")
        
        await update.message.reply_text(summary_text, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Error sending thumbnail album: {e}")
        # Fallback: send individual messages
        for i, thumb in enumerate(thumbnails, 1):
            try:
                await update.message.reply_photo(
                    photo=thumb['url'],
                    caption=f"Thumbnail {i}/{len(thumbnails)}\n📍 {thumb['position']}\n🔗 {thumb['url']}"
                )
            except Exception as e2:
                logger.error(f"Failed to send thumbnail {i}: {e2}")

async def handle_storage_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cleanup old storage messages (Admin only)"""
    user_id = update.effective_user.id
    
    # Simple admin check (you can enhance this)
    if user_id != 123456789:  # Replace with your user ID
        await update.message.reply_text("❌ Admin only command.")
        return
    
    try:
        if storage_collection is None:
            await update.message.reply_text("❌ Storage collection not available.")
            return
        
        # Find old messages (older than 30 days)
        cutoff_date = datetime.utcnow() - timedelta(days=30)
        old_messages = storage_collection.find({
            "created_at": {"$lt": cutoff_date}
        })
        
        deleted_count = 0
        for message in old_messages:
            try:
                # Delete from storage channel
                await context.bot.delete_message(
                    chat_id=STORAGE_CHANNEL_ID,
                    message_id=message['storage_message_id']
                )
                # Remove from database
                storage_collection.delete_one({"_id": message["_id"]})
                deleted_count += 1
            except Exception as e:
                logger.error(f"Failed to delete message {message['storage_message_id']}: {e}")
        
        await update.message.reply_text(f"✅ Cleaned up {deleted_count} old storage messages.")
        
    except Exception as e:
        logger.error(f"Storage cleanup error: {e}")
        await update.message.reply_text("❌ Error during storage cleanup.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    welcome_text = """👋 *Universal Video Thumbnail Extractor Bot*

Send me ANY video file and I'll extract multiple thumbnails using secure storage!

*🌟 Features:*
• 🎬 Supports ALL video formats (MP4, MKV, AVI, MOV, WebM, WMV, FLV, 3GP, etc.)
• 💾 Secure storage in Telegram channel
• 🖼️ Extract up to 5 thumbnails (start, middle, end)
• 🌐 Free hosting on Catbox
• ⚡ Fast parallel processing
• 📱 Choose your favorite thumbnail

*🔧 How it works:*
1. Your video is stored securely in our archive channel
2. Multiple thumbnails are extracted from different timestamps
3. All thumbnails are hosted on Catbox
4. You get to choose the best one!

*📊 Limits:*
• Max file size: 200MB
• Auto-caching for repeated files

Just send any video file and watch the magic! 🚀"""
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command"""
    help_text = """*📖 How to use:*

1. Send *any video file* (document or video message)
2. I'll store it securely and extract multiple thumbnails
3. Choose your favorite thumbnail from the options

*🎯 Supported formats:*
• MP4, MKV, AVI, MOV, WMV, FLV, WebM
• M4V, 3GP, MPEG, MPG, and many more!
• Both video messages and document files

*⏱️ Extraction strategy:*
• Beginning of video (0s)
• 25%, 50%, 75% marks (for longer videos)
• Near the end
• Adaptive based on video length

*💡 Pro tips:*
• Larger videos take a bit longer
• You'll get more thumbnails from longer videos
• All files are cached for instant future access
• Your videos are stored securely in our archive"""
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot statistics"""
    if thumbnails_collection is not None and storage_collection is not None:
        try:
            total_videos = thumbnails_collection.count_documents({})
            user_videos = thumbnails_collection.count_documents({
                "user_id": update.effective_user.id
            })
            total_storage = storage_collection.count_documents({})
            
            # Get file type distribution
            pipeline = [
                {"$group": {"_id": "$file_type", "count": {"$sum": 1}}}
            ]
            type_stats = list(thumbnails_collection.aggregate(pipeline))
            
            type_info = "\n".join([f"• {stat['_id'] or 'Unknown'}: {stat['count']}" for stat in type_stats])
            
            stats_text = f"""📊 *Bot Statistics*

• Total videos processed: {total_videos}
• Your videos processed: {user_videos}
• Files in storage: {total_storage}
• Database: {'✅ Connected' if mongo_client is not None else '❌ Disconnected'}

*File Type Distribution:*
{type_info}"""
        except Exception as e:
            stats_text = f"📊 *Bot Statistics*\n\n• Error generating stats: {str(e)[:100]}"
    else:
        stats_text = """📊 *Bot Statistics*

• Database: ❌ Disconnected
• Storage: ❌ Unavailable"""

    await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check bot and database status"""
    db_status = "✅ Connected" if mongo_client is not None else "❌ Disconnected"
    storage_status = "✅ Available" if STORAGE_CHANNEL_ID else "❌ Not configured"
    
    status_text = f"""🤖 *Bot Status*

• Database: {db_status}
• Storage Channel: {storage_status}
• Max file size: 200MB
• Max thumbnails: {MAX_THUMBNAILS}
• Supported: All video formats
• Ready: {'✅' if mongo_client is not None and STORAGE_CHANNEL_ID else '❌'}"""

    await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Error handler"""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

def main():
    """Start the bot with validation"""
    try:
        validate_environment()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return
    
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not found in environment variables")
        return
    
    if not STORAGE_CHANNEL_ID:
        logger.error("STORAGE_CHANNEL_ID not found in environment variables")
        return
    
    if mongo_client is None:
        logger.warning("MongoDB connection failed - caching disabled")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("cleanup", handle_storage_cleanup))
    
    # Video handlers - support both video messages and document videos
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_video))
    
    app.add_error_handler(error_handler)
    
    logger.info("Bot started successfully with universal video support and storage channel")
    logger.info(f"Storage channel ID: {STORAGE_CHANNEL_ID}")
    
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        THREAD_POOL.shutdown(wait=True)
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        THREAD_POOL.shutdown(wait=True)

if __name__ == "__main__":
    main()
