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

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot Configuration
BOT_TOKEN = "8268736244:AAGwfDn1Hzlor58Sg5A7cczwxYwzRldVJNY"
API_ID = 23054736
API_HASH = "d538c2e1a687d414f5c3dce7bf4a743c"
MONGO_URI = "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie"
CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
WEBHOOK_URL = "https://your-domain.com/webhook"  # Change this to your domain
PORT = 8080

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
    
    async with session.post(url, json=data) as resp:
        return await resp.json()

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
        async with session.post(url, json=data) as resp:
            return await resp.json()
    except:
        pass

async def delete_message(chat_id, message_id):
    """Delete message"""
    url = f"{TELEGRAM_API}/deleteMessage"
    data = {"chat_id": chat_id, "message_id": message_id}
    try:
        async with session.post(url, json=data) as resp:
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
    
    with open(photo_path, 'rb') as f:
        data.add_field('photo', f, filename='photo.jpg')
        async with session.post(url, data=data) as resp:
            return await resp.json()

async def send_media_group(chat_id, media_files):
    """Send media group"""
    url = f"{TELEGRAM_API}/sendMediaGroup"
    data = FormData()
    data.add_field('chat_id', str(chat_id))
    
    media_array = []
    for idx, item in enumerate(media_files):
        media_array.append({
            "type": "photo",
            "media": f"attach://photo{idx}",
            "caption": item['caption']
        })
        data.add_field(f'photo{idx}', open(item['path'], 'rb'), filename=f'photo{idx}.jpg')
    
    import json
    data.add_field('media', json.dumps(media_array))
    
    async with session.post(url, data=data) as resp:
        result = await resp.json()
    
    # Close file handles
    for item in media_files:
        try:
            os.close(item['path'])
        except:
            pass
    
    return result

async def download_file(file_id, destination):
    """Download file from Telegram"""
    try:
        # Get file path
        url = f"{TELEGRAM_API}/getFile"
        async with session.get(url, params={"file_id": file_id}) as resp:
            result = await resp.json()
            
        if not result.get('ok'):
            return False
        
        file_path = result['result']['file_path']
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        
        # Download file with progress
        async with session.get(download_url) as resp:
            if resp.status == 200:
                with open(destination, 'wb') as f:
                    total = int(resp.headers.get('content-length', 0))
                    downloaded = 0
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
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
    """Process video file"""
    temp_dir = None
    video_path = None
    
    try:
        # Update status
        await edit_message(chat_id, message_id, 
            f"⬇️ **Downloading**\n\n{create_progress_bar(0)}\n📁 {file_name}")
        
        # Download
        temp_dir = tempfile.mkdtemp()
        video_path = os.path.join(temp_dir, f"video_{file_id}.mp4")
        
        logger.info(f"Starting download: {file_name}")
        download_success = await download_file(file_id, video_path)
        
        if not download_success:
            await edit_message(chat_id, message_id, "❌ Download failed!")
            return
        
        await edit_message(chat_id, message_id, 
            f"🎬 **Extracting Screenshots**\n\n{create_progress_bar(30)}")
        
        # Extract screenshots
        screenshots, duration, _ = extract_screenshots(video_path, 5)
        
        if not screenshots:
            await edit_message(chat_id, message_id, "❌ Failed to extract screenshots!")
            return
        
        await edit_message(chat_id, message_id, 
            f"📤 **Uploading to Catbox**\n\n{create_progress_bar(60)}")
        
        # Create thumbnail
        thumbnail_path = os.path.join(temp_dir, "thumbnail.jpg")
        thumbnail_created = create_thumbnail([s['path'] for s in screenshots], thumbnail_path)
        
        # Upload to Catbox
        upload_tasks = [upload_to_catbox(ss['path']) for ss in screenshots]
        if thumbnail_created:
            upload_tasks.append(upload_to_catbox(thumbnail_path))
        
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)
        screenshot_urls = [url for url in results[:5] if isinstance(url, str) and url]
        thumbnail_url = results[-1] if thumbnail_created and len(results) > 5 else None
        
        # Save to MongoDB
        await screenshots_collection.insert_one({
            "chat_id": chat_id,
            "file_name": file_name,
            "file_size": file_size,
            "duration": duration,
            "screenshot_urls": screenshot_urls,
            "thumbnail_url": thumbnail_url,
            "timestamp": datetime.utcnow()
        })
        
        await edit_message(chat_id, message_id, 
            f"✅ **Complete!**\n\n{create_progress_bar(100)}")
        
        # Send thumbnail
        if thumbnail_created:
            caption = (
                f"🎬 **{file_name}**\n"
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
            urls_text = "🔗 **Direct Links:**\n\n"
            for i, url in enumerate(screenshot_urls, 1):
                urls_text += f"{i}. {url}\n"
            await send_message(chat_id, urls_text)
        
        await delete_message(chat_id, message_id)
        
    except Exception as e:
        logger.error(f"Processing error: {e}")
        await edit_message(chat_id, message_id, f"❌ Error: {str(e)[:100]}")
    
    finally:
        # Cleanup
        if temp_dir and os.path.exists(temp_dir):
            try:
                for f in os.listdir(temp_dir):
                    os.remove(os.path.join(temp_dir, f))
                os.rmdir(temp_dir)
            except:
                pass

async def handle_webhook(request):
    """Handle incoming webhook"""
    try:
        data = await request.json()
        logger.info(f"Received update: {data}")
        
        if 'message' not in data:
            return web.Response(text="OK")
        
        message = data['message']
        chat_id = message['chat']['id']
        
        # Handle commands
        if 'text' in message:
            text = message['text']
            
            if text == '/start':
                await send_message(chat_id,
                    "👋 **Welcome!**\n\n"
                    "📹 Send video → Get 5 screenshots\n"
                    "🔗 Catbox.moe hosting\n"
                    "⚡ Instant processing\n\n"
                    "Commands:\n/start /help /stats")
                
            elif text == '/help':
                await send_message(chat_id,
                    "🤖 **How to use:**\n"
                    "1. Send video file\n"
                    "2. Wait for processing\n"
                    "3. Get screenshots + URLs\n\n"
                    "📦 Max: 2GB")
                
            elif text == '/stats':
                count = await screenshots_collection.count_documents({"chat_id": chat_id})
                await send_message(chat_id,
                    f"📊 **Your Stats**\n\n"
                    f"Videos: {count}\n"
                    f"Screenshots: {count * 5}")
        
        # Handle video
        elif 'video' in message or 'document' in message:
            file_obj = message.get('video') or message.get('document')
            
            # Validate video
            if 'document' in message:
                mime = file_obj.get('mime_type', '')
                if not mime.startswith('video/'):
                    await send_message(chat_id, "❌ Please send a video file!")
                    return web.Response(text="OK")
            
            file_id = file_obj['file_id']
            file_name = file_obj.get('file_name', 'video.mp4')
            file_size = file_obj.get('file_size', 0)
            
            # Send initial message
            result = await send_message(chat_id, 
                f"⚡ **Processing Started!**\n\n{create_progress_bar(0)}")
            message_id = result['result']['message_id']
            
            # Queue processing
            await queue_collection.insert_one({
                "chat_id": chat_id,
                "file_id": file_id,
                "file_name": file_name,
                "file_size": file_size,
                "message_id": message_id,
                "status": "queued",
                "timestamp": datetime.utcnow()
            })
            
            # Process immediately in background
            asyncio.create_task(process_video(chat_id, file_id, file_name, file_size, message_id))
        
        return web.Response(text="OK")
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(text="ERROR", status=500)

async def setup_webhook():
    """Setup webhook"""
    url = f"{TELEGRAM_API}/setWebhook"
    data = {"url": WEBHOOK_URL}
    
    async with session.post(url, json=data) as resp:
        result = await resp.json()
        logger.info(f"Webhook setup: {result}")

async def start_server(app):
    """Startup"""
    global session
    session = ClientSession()
    await setup_webhook()
    logger.info(f"Server started on port {PORT}")

async def cleanup(app):
    """Cleanup"""
    if session:
        await session.close()

# Create app
app = web.Application()
app.router.add_post('/webhook', handle_webhook)
app.on_startup.append(start_server)
app.on_cleanup.append(cleanup)

if __name__ == '__main__':
    logger.info("🚀 Starting Webhook Bot...")
    web.run_app(app, host='0.0.0.0', port=PORT)
