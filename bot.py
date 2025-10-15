import os
import asyncio
from datetime import datetime
from aiohttp import web, ClientSession, FormData, ClientTimeout
from motor.motor_asyncio import AsyncIOMotorClient
import cv2
import tempfile
from PIL import Image
import math
import logging
import subprocess
import json

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables (set in Koyeb)
BOT_TOKEN = os.getenv("BOT_TOKEN", "8268736244:AAGwfDn1Hzlor58Sg5A7cczwxYwzRldVJNY")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # Will be set automatically by Koyeb
PORT = int(os.getenv("PORT", 8000))
CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Initialize MongoDB
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.telegram_bot
screenshots_collection = db.screenshots
queue_collection = db.processing_queue

# Global session
session = None

def create_progress_bar(percentage, length=10):
    """Create visual progress bar"""
    filled = int(length * percentage / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percentage:.0f}%"

async def send_message(chat_id, text, reply_markup=None):
    """Send message via Telegram API"""
    url = f"{TELEGRAM_API}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    
    try:
        async with session.post(url, json=data, timeout=ClientTimeout(total=30)) as resp:
            return await resp.json()
    except Exception as e:
        logger.error(f"Send message error: {e}")
        return None

async def edit_message(chat_id, message_id, text):
    """Edit message via Telegram API"""
    url = f"{TELEGRAM_API}/editMessageText"
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        async with session.post(url, json=data, timeout=ClientTimeout(total=10)) as resp:
            return await resp.json()
    except:
        pass

async def delete_message(chat_id, message_id):
    """Delete message"""
    url = f"{TELEGRAM_API}/deleteMessage"
    data = {"chat_id": chat_id, "message_id": message_id}
    try:
        async with session.post(url, json=data, timeout=ClientTimeout(total=10)) as resp:
            return await resp.json()
    except:
        pass

async def send_photo(chat_id, photo_path, caption):
    """Send photo via Telegram API"""
    url = f"{TELEGRAM_API}/sendPhoto"
    data = FormData()
    data.add_field('chat_id', str(chat_id))
    data.add_field('caption', caption)
    data.add_field('parse_mode', 'Markdown')
    
    try:
        with open(photo_path, 'rb') as f:
            data.add_field('photo', f, filename='photo.jpg')
            async with session.post(url, data=data, timeout=ClientTimeout(total=60)) as resp:
                return await resp.json()
    except Exception as e:
        logger.error(f"Send photo error: {e}")
        return None

async def send_media_group(chat_id, media_files):
    """Send media group"""
    url = f"{TELEGRAM_API}/sendMediaGroup"
    
    try:
        data = FormData()
        data.add_field('chat_id', str(chat_id))
        
        media_array = []
        files_to_close = []
        
        for idx, item in enumerate(media_files):
            media_array.append({
                "type": "photo",
                "media": f"attach://photo{idx}",
                "caption": item['caption']
            })
            f = open(item['path'], 'rb')
            files_to_close.append(f)
            data.add_field(f'photo{idx}', f, filename=f'photo{idx}.jpg')
        
        import json
        data.add_field('media', json.dumps(media_array))
        
        async with session.post(url, data=data, timeout=ClientTimeout(total=120)) as resp:
            result = await resp.json()
        
        # Close file handles
        for f in files_to_close:
            f.close()
        
        return result
    except Exception as e:
        logger.error(f"Send media group error: {e}")
        return None

async def download_file(file_id, destination):
    """Download file from Telegram"""
    try:
        # Get file path
        url = f"{TELEGRAM_API}/getFile"
        async with session.get(url, params={"file_id": file_id}, timeout=ClientTimeout(total=30)) as resp:
            result = await resp.json()
            
        if not result.get('ok'):
            logger.error(f"Get file failed: {result}")
            return False
        
        file_path = result['result']['file_path']
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        
        # Download file
        async with session.get(download_url, timeout=ClientTimeout(total=600)) as resp:
            if resp.status == 200:
                with open(destination, 'wb') as f:
                    total = int(resp.headers.get('content-length', 0))
                    downloaded = 0
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0 and downloaded % (1024 * 1024) == 0:  # Log every MB
                            progress = (downloaded / total) * 100
                            logger.info(f"Download progress: {progress:.1f}%")
                return True
        return False
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False

async def upload_to_catbox(file_path):
    """Upload to Catbox.moe"""
    try:
        timeout = ClientTimeout(total=120)
        async with ClientSession(timeout=timeout) as sess:
            data = FormData()
            data.add_field('reqtype', 'fileupload')
            
            with open(file_path, 'rb') as f:
                data.add_field('fileToUpload', f, filename=os.path.basename(file_path))
                
                async with sess.post(CATBOX_UPLOAD_URL, data=data) as resp:
                    if resp.status == 200:
                        url = await resp.text()
                        return url.strip()
    except Exception as e:
        logger.error(f"Catbox upload error: {e}")
    return None

def compress_video(input_path, output_path, target_size_mb=15):
    """
    Compress video to target size using FFmpeg
    target_size_mb: Target size in MB (default 15MB to stay under 20MB limit)
    """
    try:
        # Get video duration
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        info = json.loads(result.stdout)
        
        duration = float(info['format']['duration'])
        
        # Calculate target bitrate (in kbps)
        target_size_bits = target_size_mb * 8 * 1024  # Convert MB to kilobits
        target_bitrate = int(target_size_bits / duration / 1024)  # kbps
        
        # Ensure minimum bitrate for quality
        target_bitrate = max(target_bitrate, 500)  # Minimum 500 kbps
        
        # Compress video using FFmpeg
        cmd = [
            'ffmpeg', '-i', input_path,
            '-c:v', 'libx264',
            '-b:v', f'{target_bitrate}k',
            '-maxrate', f'{target_bitrate}k',
            '-bufsize', f'{target_bitrate * 2}k',
            '-preset', 'medium',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-y',  # Overwrite output file
            output_path
        ]
        
        logger.info(f"Compressing video with bitrate: {target_bitrate}kbps")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0 and os.path.exists(output_path):
            compressed_size = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"Compression successful: {compressed_size:.1f}MB")
            return True
        else:
            logger.error(f"FFmpeg error: {result.stderr}")
            return False
            
    except Exception as e:
        logger.error(f"Video compression error: {e}")
        return False

def create_thumbnail(screenshot_paths, output_path):
    """Create thumbnail grid"""
    try:
        images = []
        for path in screenshot_paths[:5]:
            if os.path.exists(path):
                img = Image.open(path)
                img.thumbnail((400, 300), Image.Resampling.LANCZOS)
                images.append(img)
        
        if not images:
            return None
        
        cols = 2
        rows = math.ceil(len(images) / 2)
        width = images[0].width * cols + 10
        height = images[0].height * rows + (rows - 1) * 10
        
        thumbnail = Image.new('RGB', (width, height), (0, 0, 0))
        
        for idx, img in enumerate(images):
            x = (idx % cols) * (images[0].width + 10)
            y = (idx // cols) * (images[0].height + 10)
            thumbnail.paste(img, (x, y))
        
        thumbnail.save(output_path, 'JPEG', quality=85)
        return output_path
    except Exception as e:
        logger.error(f"Thumbnail error: {e}")
        return None

def extract_screenshots(video_path, num_screenshots=5):
    """Extract screenshots from video"""
    screenshots = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        if fps <= 0 or total_frames <= 0:
            cap.release()
            return [], 0, temp_dir
        
        duration = total_frames / fps
        
        if num_screenshots == 1:
            frame_positions = [total_frames // 2]
        else:
            step = total_frames // (num_screenshots + 1)
            frame_positions = [step * (i + 1) for i in range(num_screenshots)]
        
        for idx, frame_pos in enumerate(frame_positions):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_pos))
            ret, frame = cap.read()
            
            if ret:
                screenshot_path = os.path.join(temp_dir, f"screenshot_{idx+1}.jpg")
                cv2.imwrite(screenshot_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                
                timestamp = frame_pos / fps
                screenshots.append({
                    'path': screenshot_path,
                    'timestamp': timestamp
                })
        
        cap.release()
        return screenshots, duration, temp_dir
    except Exception as e:
        logger.error(f"Screenshot extraction error: {e}")
        return [], 0, temp_dir

async def process_video(chat_id, file_id, file_name, file_size, message_id):
    """Process video file - now handles files >20MB with compression"""
    temp_dir = None
    video_path = None
    compressed_path = None
    
    try:
        logger.info(f"Processing started for {file_name} ({file_size/(1024*1024):.1f}MB)")
        
        # Update status
        await edit_message(chat_id, message_id, 
            f"⬇️ **Downloading**\n\n{create_progress_bar(0)}\n📁 {file_name}\n💾 {file_size/(1024*1024):.1f}MB")

        # Download
        temp_dir = tempfile.mkdtemp()
        video_path = os.path.join(temp_dir, f"video_{file_id}.mp4")
        
        download_success = await download_file(file_id, video_path)
        
        if not download_success or not os.path.exists(video_path):
            await edit_message(chat_id, message_id, "❌ Download failed!")
            return
        
        downloaded_size = os.path.getsize(video_path) / (1024 * 1024)
        logger.info(f"Downloaded: {downloaded_size:.1f}MB")
        
        # Check if compression is needed
        needs_compression = downloaded_size > 20
        processing_video_path = video_path
        
        if needs_compression:
            await edit_message(chat_id, message_id, 
                f"🎬 **Compressing Video**\n\n{create_progress_bar(20)}\n📦 Reducing size for processing...")
            
            compressed_path = os.path.join(temp_dir, f"compressed_{file_id}.mp4")
            compression_success = compress_video(video_path, compressed_path)
            
            if compression_success:
                compressed_size = os.path.getsize(compressed_path) / (1024 * 1024)
                logger.info(f"Compressed from {downloaded_size:.1f}MB to {compressed_size:.1f}MB")
                processing_video_path = compressed_path
                await edit_message(chat_id, message_id, 
                    f"✅ **Compression Complete**\n\n{create_progress_bar(40)}\n📦 {compressed_size:.1f}MB (reduced from {downloaded_size:.1f}MB)")
            else:
                await edit_message(chat_id, message_id, 
                    f"⚠️ **Using Original**\n\n{create_progress_bar(40)}\n📦 Compression failed, using original file")
                # Continue with original file even if compression fails
        else:
            await edit_message(chat_id, message_id, 
                f"🎬 **Extracting Screenshots**\n\n{create_progress_bar(40)}")

        # Extract screenshots
        screenshots, duration, screenshot_temp_dir = extract_screenshots(processing_video_path, 5)
        
        if not screenshots:
            await edit_message(chat_id, message_id, "❌ Failed to extract screenshots!")
            return
        
        await edit_message(chat_id, message_id, 
            f"📤 **Uploading to Catbox**\n\n{create_progress_bar(70)}")
        
        # Create thumbnail
        thumbnail_path = os.path.join(temp_dir, "thumbnail.jpg")
        thumbnail_created = create_thumbnail([s['path'] for s in screenshots], thumbnail_path)
        
        # Upload to Catbox
        upload_tasks = [upload_to_catbox(ss['path']) for ss in screenshots]
        if thumbnail_created:
            upload_tasks.append(upload_to_catbox(thumbnail_path))
        
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)
        screenshot_urls = [url for url in results[:5] if isinstance(url, str) and url]
        thumbnail_url = results[-1] if thumbnail_created and len(results) > 5 and isinstance(results[-1], str) else None
        
        # Save to MongoDB
        try:
            await screenshots_collection.insert_one({
                "chat_id": chat_id,
                "file_name": file_name,
                "file_size": file_size,
                "compressed": needs_compression,
                "duration": duration,
                "screenshot_urls": screenshot_urls,
                "thumbnail_url": thumbnail_url,
                "timestamp": datetime.utcnow()
            })
        except Exception as e:
            logger.error(f"MongoDB error: {e}")
        
        await edit_message(chat_id, message_id, 
            f"✅ **Complete!**\n\n{create_progress_bar(100)}")
        
        # Send thumbnail
        if thumbnail_created and os.path.exists(thumbnail_path):
            compression_note = " (compressed)" if needs_compression else ""
            caption = (
                f"🎬 **{file_name}**{compression_note}\n"
                f"⏱ {int(duration//60)}:{int(duration%60):02d}\n"
                f"📦 {file_size/(1024*1024):.1f} MB\n"
            )
            if thumbnail_url:
                caption += f"🔗 {thumbnail_url}"
            
            await send_photo(chat_id, thumbnail_path, caption)
        
        # Send screenshots
        media_files = []
        for idx, ss in enumerate(screenshots):
            m = int(ss['timestamp'] // 60)
            s = int(ss['timestamp'] % 60)
            cap = f"📸 {idx+1}/5 - {m:02d}:{s:02d}"
            if idx < len(screenshot_urls):
                cap += f"\n{screenshot_urls[idx]}"
            media_files.append({'path': ss['path'], 'caption': cap})
        
        await send_media_group(chat_id, media_files)
        
        # Send URL summary
        if screenshot_urls:
            urls_text = "🔗 **All Links:**\n\n"
            for i, url in enumerate(screenshot_urls, 1):
                urls_text += f"{i}. {url}\n"
            await send_message(chat_id, urls_text)
        
        await delete_message(chat_id, message_id)
        logger.info(f"Processing complete for {file_name}")
        
    except Exception as e:
        logger.error(f"Processing error: {e}")
        try:
            await edit_message(chat_id, message_id, f"❌ Error: {str(e)[:100]}")
        except:
            pass
    
    finally:
        # Cleanup
        if temp_dir and os.path.exists(temp_dir):
            try:
                for f in os.listdir(temp_dir):
                    try:
                        os.remove(os.path.join(temp_dir, f))
                    except:
                        pass
                os.rmdir(temp_dir)
            except:
                pass

async def handle_webhook(request):
    """Handle incoming webhook"""
    try:
        data = await request.json()
        logger.info(f"Received update")
        
        if 'message' not in data:
            return web.Response(text="OK")
        
        message = data['message']
        chat_id = message['chat']['id']
        
        # Handle commands
        if 'text' in message:
            text = message['text']
            
            if text == '/start':
                await send_message(chat_id,
                    "👋 **Welcome to Screenshot Bot!**\n\n"
                    "📹 Send video → Get 5 screenshots\n"
                    "🔗 Catbox.moe hosting\n"
                    "⚡ Automatic compression for large files\n"
                    "📦 Supports files >20MB\n\n"
                    "Commands: /start /help /stats")
                
            elif text == '/help':
                await send_message(chat_id,
                    "🤖 **How to use:**\n\n"
                    "1. Send video file (any size)\n"
                    "2. Large files are automatically compressed\n"
                    "3. Wait for processing\n"
                    "4. Get screenshots + URLs\n\n"
                    "📦 Large files: Auto-compressed\n"
                    "🎬 Formats: MP4, MKV, AVI, MOV, etc.\n"
                    "⚡ Fast processing with compression")
                
            elif text == '/stats':
                count = await screenshots_collection.count_documents({"chat_id": chat_id})
                compressed_count = await screenshots_collection.count_documents({
                    "chat_id": chat_id, 
                    "compressed": True
                })
                await send_message(chat_id,
                    f"📊 **Your Stats**\n\n"
                    f"✅ Videos: {count}\n"
                    f"📸 Screenshots: {count * 5}\n"
                    f"🎬 Compressed: {compressed_count}")
        
        # Handle video
        elif 'video' in message or 'document' in message:
            file_obj = message.get('video') or message.get('document')
            
            # Validate video
            if 'document' in message:
                mime = file_obj.get('mime_type', '')
                fname = file_obj.get('file_name', '')
                video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm', '.m4v')
                if not (mime.startswith('video/') or fname.lower().endswith(video_exts)):
                    await send_message(chat_id, "❌ Please send a video file!")
                    return web.Response(text="OK")
            
            file_id = file_obj['file_id']
            file_name = file_obj.get('file_name', 'video.mp4')
            file_size = file_obj.get('file_size', 0)
            
            # No longer checking file size limit - we'll compress if needed
            
            # Send initial message
            size_info = f"💾 {file_size/(1024*1024):.1f}MB" if file_size > 0 else ""
            result = await send_message(chat_id, 
                f"⚡ **Processing Started!**\n\n{create_progress_bar(0)}\n📁 {file_name}\n{size_info}")
            
            if result and 'result' in result:
                message_id = result['result']['message_id']
                
                # Process immediately in background
                asyncio.create_task(process_video(chat_id, file_id, file_name, file_size, message_id))
        
        return web.Response(text="OK")
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(text="OK")  # Always return OK to Telegram

async def health_check(request):
    """Health check endpoint for Koyeb"""
    return web.Response(text="OK", status=200)

async def setup_webhook():
    """Setup webhook"""
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL not set! Please set it in Koyeb environment variables.")
        return
    
    webhook_endpoint = f"{WEBHOOK_URL}/webhook"
    url = f"{TELEGRAM_API}/setWebhook"
    data = {"url": webhook_endpoint}
    
    try:
        async with session.post(url, json=data, timeout=ClientTimeout(total=30)) as resp:
            result = await resp.json()
            logger.info(f"Webhook setup: {result}")
    except Exception as e:
        logger.error(f"Webhook setup error: {e}")

async def start_server(app):
    """Startup"""
    global session
    session = ClientSession()
    await setup_webhook()
    logger.info(f"🚀 Server started on port {PORT}")
    logger.info(f"📡 Webhook URL: {WEBHOOK_URL}")

async def cleanup(app):
    """Cleanup"""
    if session:
        await session.close()

# Create app
app = web.Application()
app.router.add_post('/webhook', handle_webhook)
app.router.add_get('/health', health_check)
app.router.add_get('/', health_check)
app.on_startup.append(start_server)
app.on_cleanup.append(cleanup)

if __name__ == '__main__':
    logger.info("🤖 Screenshot Bot Starting...")
    web.run_app(app, host='0.0.0.0', port=PORT)
