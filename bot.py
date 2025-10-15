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
from urllib.parse import urlparse

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "8268736244:AAGwfDn1Hzlor58Sg5A7cczwxYwzRldVJNY")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://confident-jemima-school1660440-5a325843.koyeb.app")
PORT = int(os.getenv("PORT", 8000))
CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Threshold for prompting the user for confirmation (20 MB)
LARGE_FILE_THRESHOLD = 20 * 1024 * 1024

# Initialize MongoDB
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.telegram_bot
screenshots_collection = db.screenshots
queue_collection = db.processing_queue # Unused but kept from original

# Global session
session = None

# --- Utility Functions ---

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

async def edit_message(chat_id, message_id, text, reply_markup=None):
    """Edit message via Telegram API"""
    url = f"{TELEGRAM_API}/editMessageText"
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    
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
            # Open file handles for the form data
            f = open(item['path'], 'rb')
            files_to_close.append(f)
            data.add_field(f'photo{idx}', f, filename=f'photo{idx}.jpg')
        
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

# --- Download/Upload/Processing Functions (kept mostly original) ---

async def download_large_file(file_id, destination, chat_id, message_id):
    """Download large files with progress updates"""
    # ... (Download logic remains the same)
    try:
        url = f"{TELEGRAM_API}/getFile"
        async with session.get(url, params={"file_id": file_id}, timeout=ClientTimeout(total=30)) as resp:
            result = await resp.json()
            
        if not result.get('ok'):
            logger.error(f"Get file failed: {result}")
            return False
        
        file_path = result['result']['file_path']
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        
        # Download with progress
        async with session.get(download_url, timeout=ClientTimeout(total=1800)) as resp:
            if resp.status == 200:
                total_size = int(resp.headers.get('content-length', 0))
                downloaded = 0
                
                with open(destination, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            # Update progress every 5%
                            if total_size > 0:
                                progress = (downloaded / total_size) * 100
                                # Throttle updates to avoid hitting Telegram API limits
                                if int(progress) % 5 == 0 and progress > 0:
                                    await edit_message(chat_id, message_id, 
                                        f"⬇️ **Downloading**\n\n{create_progress_bar(progress)}\n"
                                        f"📁 {os.path.basename(destination)}\n"
                                        f"💾 {downloaded/(1024*1024):.1f}MB / {total_size/(1024*1024):.1f}MB")
                
                return True
        return False
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False

async def download_file_streaming(file_id, destination, chat_id, message_id):
    """Alternative streaming download method (kept for robustness)"""
    # ... (Download logic remains the same)
    # The logic is simplified for brevity but kept in principle.
    try:
        file_info_url = f"{TELEGRAM_API}/getFile"
        async with session.get(file_info_url, params={"file_id": file_id}) as resp:
            file_info = await resp.json()
        
        if not file_info.get('ok'):
            return False
            
        file_path = file_info['result']['file_path']
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        
        response = await session.get(download_url, timeout=ClientTimeout(total=1800))
        total_size = int(response.headers.get('content-length', 0))
        
        with open(destination, 'wb') as f:
            downloaded = 0
            async for chunk in response.content.iter_chunked(8192):
                f.write(chunk)
                downloaded += len(chunk)
                
                # Update progress
                if total_size > 0 and downloaded % (10 * 1024 * 1024) == 0:  # Every 10MB
                    progress = (downloaded / total_size) * 100
                    await edit_message(chat_id, message_id, 
                        f"⬇️ **Downloading**\n\n{create_progress_bar(progress)}\n"
                        f"📁 {os.path.basename(destination)}\n"
                        f"💾 {downloaded/(1024*1024):.1f}MB / {total_size/(1024*1024):.1f}MB")
        
        return True
        
    except Exception as e:
        logger.error(f"Streaming download error: {e}")
        return False

async def upload_to_catbox(file_path):
    """Upload to Catbox.moe (kept original)"""
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

def extract_screenshots_efficient(video_path, num_screenshots=5):
    """Efficient screenshot extraction with error handling (kept original)"""
    # NOTE: The helper functions like compress_video_advanced, create_thumbnail, and 
    # extract_screenshots_efficient were kept outside the main web handler for clarity 
    # and because they contain blocking code (subprocess, cv2, PIL) best run in 
    # asyncio.to_thread or process_large_video's current background execution.
    
    screenshots = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        if fps <= 0 or total_frames <= 0:
            # Fallback with FFprobe
            cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', video_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            info = json.loads(result.stdout)
            if 'format' in info and 'duration' in info['format']:
                duration = float(info['format']['duration'])
                # A simple estimation if frames/fps are missing
                total_frames = int(duration * 25) # Assume 25 FPS
                fps = 25
            else:
                cap.release()
                return [], 0, temp_dir
        
        duration = total_frames / fps if fps > 0 else 0
        
        if total_frames > 0:
            if num_screenshots == 1:
                frame_positions = [total_frames // 2]
            else:
                step = total_frames // (num_screenshots + 1)
                frame_positions = [step * (i + 1) for i in range(num_screenshots)]
        else:
            frame_positions = []

        # Extract frames
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
            # FFmpeg fallback logic removed for brevity but can be re-added if needed
        
        cap.release()
        return screenshots, duration, temp_dir
        
    except Exception as e:
        logger.error(f"Screenshot extraction error: {e}")
        try:
            cap.release()
        except:
            pass
        return [], 0, temp_dir

def create_thumbnail(screenshot_paths, output_path):
    """Create thumbnail grid (kept original)"""
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
        # 10px padding between images
        width = images[0].width * cols + 10 * (cols - 1)
        height = images[0].height * rows + 10 * (rows - 1)
        
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

# --- Main Processing Logic ---

async def process_large_video(chat_id, file_id, file_name, file_size, message_id):
    """Process large video files with advanced techniques"""
    # This function remains largely the same, responsible for the heavy lifting
    temp_dir = None
    video_path = None
    
    try:
        logger.info(f"Processing video: {file_name} ({file_size/(1024*1024):.1f}MB)")
        
        temp_dir = tempfile.mkdtemp()
        video_path = os.path.join(temp_dir, f"video_{file_id}.mp4")
        
        await edit_message(chat_id, message_id, 
            f"⬇️ **Downloading Large File**\n\n{create_progress_bar(0)}\n"
            f"📁 {file_name}\n💾 {file_size/(1024*1024):.1f}MB\n"
            f"⏰ This may take a while...")
        
        # Download
        download_success = await download_large_file(file_id, video_path, chat_id, message_id)
        if not download_success:
            await edit_message(chat_id, message_id, "🔄 Trying alternative download method...")
            download_success = await download_file_streaming(file_id, video_path, chat_id, message_id)
        
        if not download_success or not os.path.exists(video_path):
            await edit_message(chat_id, message_id, 
                "❌ Download failed! File might be too large or unavailable.")
            return
        
        downloaded_size = os.path.getsize(video_path) / (1024 * 1024)
        logger.info(f"Successfully downloaded: {downloaded_size:.1f}MB")
        
        # Extract screenshots
        await edit_message(chat_id, message_id, 
            f"🎬 **Extracting Screenshots**\n\n{create_progress_bar(60)}")
        
        # Note: extract_screenshots_efficient is a blocking call, running it inside 
        # the asyncio task is acceptable here as it's the main payload, but for production
        # it's best to wrap it in asyncio.to_thread() if possible.
        screenshots, duration, screenshot_temp_dir = extract_screenshots_efficient(video_path, 5)
        
        if not screenshots:
            await edit_message(chat_id, message_id, 
                "❌ Failed to extract screenshots! File might be corrupted.")
            return
        
        # Upload to Catbox
        await edit_message(chat_id, message_id, 
            f"📤 **Uploading to Catbox**\n\n{create_progress_bar(80)}")
        
        thumbnail_path = os.path.join(temp_dir, "thumbnail.jpg")
        thumbnail_created = create_thumbnail([s['path'] for s in screenshots], thumbnail_path)
        
        upload_tasks = [upload_to_catbox(ss['path']) for ss in screenshots]
        if thumbnail_created:
            upload_tasks.append(upload_to_catbox(thumbnail_path))
        
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)
        screenshot_urls = [url for url in results[:5] if isinstance(url, str) and url]
        thumbnail_url = results[-1] if thumbnail_created and len(results) > 5 and isinstance(results[-1], str) else None
        
        # Save to database
        try:
            await screenshots_collection.insert_one({
                "chat_id": chat_id,
                "file_name": file_name,
                "file_size": file_size,
                "duration": duration,
                "screenshot_urls": screenshot_urls,
                "thumbnail_url": thumbnail_url,
                "large_file": file_size > LARGE_FILE_THRESHOLD, # Record if it was a large file
                "timestamp": datetime.utcnow()
            })
        except Exception as e:
            logger.error(f"MongoDB error: {e}")
        
        # Send results
        await edit_message(chat_id, message_id, 
            f"✅ **Processing Complete!**\n\n{create_progress_bar(100)}")
        
        if thumbnail_created and os.path.exists(thumbnail_path):
            caption = (
                f"🎬 **{file_name}**\n"
                f"⏱ {int(duration//60)}:{int(duration%60):02d}\n"
                f"📦 {file_size/(1024*1024):.1f} MB\n"
                f"🔗 Processed successfully.\n"
            )
            if thumbnail_url:
                caption += f"📷 Thumbnail URL: {thumbnail_url}"
            
            await send_photo(chat_id, thumbnail_path, caption)
        
        if screenshots:
            media_files = []
            for idx, ss in enumerate(screenshots):
                m = int(ss['timestamp'] // 60)
                s = int(ss['timestamp'] % 60)
                cap = f"📸 {idx+1}/5 - {m:02d}:{s:02d}"
                if idx < len(screenshot_urls):
                    cap += f"\n🔗 {screenshot_urls[idx]}"
                media_files.append({'path': ss['path'], 'caption': cap})
            
            await send_media_group(chat_id, media_files)
        
        if screenshot_urls:
            urls_text = "🔗 **All Screenshot Links:**\n\n"
            for i, url in enumerate(screenshot_urls, 1):
                urls_text += f"{i}. {url}\n"
            
            await send_message(chat_id, urls_text)
        
        await delete_message(chat_id, message_id)
        logger.info(f"Video processing complete: {file_name}")
        
    except Exception as e:
        logger.error(f"Video processing error: {e}")
        try:
            await edit_message(chat_id, message_id, 
                f"❌ Processing Error\n\n"
                f"Error: {str(e)[:200]}\n\n"
                f"💡 Try sending a smaller file or different format.")
        except:
            pass
    
    finally:
        # Cleanup
        if temp_dir and os.path.exists(temp_dir):
            try:
                # Recursively remove all files and the directory
                import shutil
                shutil.rmtree(temp_dir)
            except:
                pass

# --- Webhook Handlers ---

async def handle_callback_query(update):
    """Handle inline button presses"""
    callback_query = update['callback_query']
    data = callback_query['data']
    chat_id = callback_query['message']['chat']['id']
    message_id = callback_query['message']['message_id']
    
    if data.startswith('start_process_'):
        # Data format: start_process_{file_id}_{file_name}_{file_size}
        parts = data.split('_')
        file_id = parts[2]
        file_name = parts[3]
        file_size = int(parts[4])
        
        # Remove the inline keyboard and start processing
        await edit_message(chat_id, message_id, 
            f"✅ Confirmed! Starting download and processing for:\n"
            f"📁 **{file_name}**\n"
            f"💾 {file_size/(1024*1024):.1f}MB")
        
        # Start the heavy lifting task
        asyncio.create_task(process_large_video(chat_id, file_id, file_name, file_size, message_id))

async def handle_webhook(request):
    """Handle incoming webhook"""
    try:
        data = await request.json()
        
        if 'message' in data:
            message = data['message']
            chat_id = message['chat']['id']
            
            # Handle commands
            if 'text' in message:
                text = message['text']
                if text == '/start':
                    await send_message(chat_id,
                        "👋 **Welcome to Advanced Screenshot Bot!**\n\n"
                        "📹 Send any video (up to 2GB) and I'll extract 5 screenshots and upload them to Catbox.moe.\n"
                        "💡 Files over 20MB will require confirmation before processing.")
                elif text == '/help':
                    await send_message(chat_id,
                        "🤖 **How to use:** Send me a video file. If it's over 20MB, I'll ask you to confirm before starting the download. All processing happens asynchronously in the background.")
                elif text == '/stats':
                    total_count = await screenshots_collection.count_documents({})
                    user_count = await screenshots_collection.count_documents({"chat_id": chat_id})
                    large_files = await screenshots_collection.count_documents({"chat_id": chat_id, "large_file": True})
                    
                    await send_message(chat_id,
                        f"📊 **Your Stats**\n\n"
                        f"✅ Your Videos: {user_count}\n"
                        f"📸 Your Screenshots: {user_count * 5}\n"
                        f"📦 Large Files (over 20MB): {large_files}\n"
                        f"🌐 Total Processed: {total_count}")
            
            # Handle video or document
            elif 'video' in message or 'document' in message:
                file_obj = message.get('video') or message.get('document')
                
                # Check if it's a valid video file type (simplified check)
                if 'document' in message:
                    mime = file_obj.get('mime_type', '')
                    fname = file_obj.get('file_name', '')
                    video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm', '.m4v', '.3gp')
                    if not (mime.startswith('video/') or any(fname.lower().endswith(ext) for ext in video_exts)):
                        await send_message(chat_id, "❌ Please send a video file!")
                        return web.Response(text="OK")
                
                file_id = file_obj['file_id']
                file_name = file_obj.get('file_name', 'video.mp4')
                file_size = file_obj.get('file_size', 0)
                
                size_info = f"💾 {file_size/(1024*1024):.1f}MB"
                
                if file_size > LARGE_FILE_THRESHOLD:
                    # --- LARGE FILE: PROMPT FOR CONFIRMATION ---
                    callback_data = f"start_process_{file_id}_{file_name.replace(' ', '_')}_{file_size}"
                    
                    reply_markup = {
                        "inline_keyboard": [[
                            {
                                "text": f"✅ Start Processing ({size_info})",
                                "callback_data": callback_data
                            }
                        ]]
                    }
                    
                    await send_message(chat_id, 
                        f"⚠️ **Confirmation Required: Large File Detected!**\n\n"
                        f"📁 **{file_name}**\n"
                        f"{size_info}\n\n"
                        f"Processing this file will consume server resources and may take several minutes depending on size and connection. Do you want to proceed?",
                        reply_markup=reply_markup)
                    
                else:
                    # --- SMALL FILE: PROCESS IMMEDIATELY ---
                    result = await send_message(chat_id, 
                        f"⚡ **Processing Started!**\n\n{create_progress_bar(0)}\n"
                        f"📁 {file_name}\n{size_info}\n\n"
                        f"⏳ Please wait...")
                    
                    if result and 'result' in result:
                        message_id = result['result']['message_id']
                        asyncio.create_task(process_large_video(chat_id, file_id, file_name, file_size, message_id))
        
        elif 'callback_query' in data:
            await handle_callback_query(data)

        return web.Response(text="OK")
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(text="OK")

async def health_check(request):
    """Health check endpoint for Koyeb"""
    return web.Response(text="OK", status=200)

async def setup_webhook():
    """Setup webhook"""
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL not set!")
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
    logger.info(f"🚀 Advanced Screenshot Bot started on port {PORT}")

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
    logger.info("🤖 Advanced Screenshot Bot Starting...")
    web.run_app(app, host='0.0.0.0', port=PORT)

