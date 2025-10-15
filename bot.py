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
import requests
from urllib.parse import urlparse

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables (set in Koyeb)
BOT_TOKEN = os.getenv("BOT_TOKEN", "8268736244:AAGwfDn1Hzlor58Sg5A7cczwxYwzRldVJNY")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://confident-jemima-school1660440-5a325843.koyeb.app")  # Will be set automatically by Koyeb
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

async def download_large_file(file_id, destination, chat_id, message_id):
    """Download large files using advanced techniques"""
    try:
        # Method 1: Try direct download first
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
                                if int(progress) % 5 == 0:  # Update every 5%
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
    """Alternative streaming download method"""
    try:
        # Get file info
        file_info_url = f"{TELEGRAM_API}/getFile"
        async with session.get(file_info_url, params={"file_id": file_id}) as resp:
            file_info = await resp.json()
        
        if not file_info.get('ok'):
            return False
            
        file_path = file_info['result']['file_path']
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        
        # Stream download with progress
        response = await session.get(download_url)
        total_size = int(response.headers.get('content-length', 0))
        
        with open(destination, 'wb') as f:
            downloaded = 0
            async for chunk in response.content.iter_chunked(8192):
                f.write(chunk)
                downloaded += len(chunk)
                
                # Update progress
                if total_size > 0 and downloaded % (5 * 1024 * 1024) == 0:  # Every 5MB
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

def compress_video_advanced(input_path, output_path, target_size_mb=50):
    """
    Advanced video compression with multiple quality settings
    """
    try:
        # Get video info
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        info = json.loads(result.stdout)
        
        duration = float(info['format']['duration'])
        original_size = os.path.getsize(input_path) / (1024 * 1024)
        
        logger.info(f"Original size: {original_size:.1f}MB, Target: {target_size_mb}MB")
        
        # Calculate target bitrate
        target_size_bits = target_size_mb * 8 * 1024  # Convert MB to kilobits
        target_bitrate = int(target_size_bits / duration / 1024)  # kbps
        
        # Ensure reasonable bitrate limits
        target_bitrate = max(target_bitrate, 500)  # Minimum 500 kbps
        target_bitrate = min(target_bitrate, 2000)  # Maximum 2000 kbps
        
        # Try multiple compression strategies
        compression_strategies = [
            # Strategy 1: Standard compression
            [
                'ffmpeg', '-i', input_path,
                '-c:v', 'libx264',
                '-b:v', f'{target_bitrate}k',
                '-maxrate', f'{target_bitrate}k',
                '-bufsize', f'{target_bitrate * 2}k',
                '-preset', 'medium',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-y', output_path
            ],
            # Strategy 2: More aggressive compression
            [
                'ffmpeg', '-i', input_path,
                '-c:v', 'libx264',
                '-b:v', f'{target_bitrate}k',
                '-preset', 'fast',
                '-crf', '28',
                '-c:a', 'aac',
                '-b:a', '96k',
                '-y', output_path
            ],
            # Strategy 3: Even more aggressive
            [
                'ffmpeg', '-i', input_path,
                '-c:v', 'libx264',
                '-b:v', f'{max(500, target_bitrate-200)}k',
                '-preset', 'veryfast',
                '-crf', '30',
                '-c:a', 'aac',
                '-b:a', '64k',
                '-y', output_path
            ]
        ]
        
        for i, cmd in enumerate(compression_strategies):
            logger.info(f"Trying compression strategy {i+1}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            if result.returncode == 0 and os.path.exists(output_path):
                compressed_size = os.path.getsize(output_path) / (1024 * 1024)
                logger.info(f"Compression successful: {compressed_size:.1f}MB")
                
                # Check if compressed size is reasonable
                if compressed_size <= target_size_mb * 1.2:  # Within 20% of target
                    return True
                else:
                    # Try next strategy
                    continue
        
        # If all strategies fail, use the last successful one
        if os.path.exists(output_path):
            return True
            
        return False
            
    except Exception as e:
        logger.error(f"Advanced compression error: {e}")
        return False

def extract_screenshots_efficient(video_path, num_screenshots=5):
    """Efficient screenshot extraction with error handling"""
    screenshots = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        # First try with OpenCV
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        if fps <= 0 or total_frames <= 0:
            # If OpenCV fails, try with FFmpeg to get video info
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams', video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            info = json.loads(result.stdout)
            
            if 'streams' in info and len(info['streams']) > 0:
                duration = float(info['format']['duration'])
                fps = info['streams'][0].get('r_frame_rate', '25/1')
                fps = eval(fps)  # Convert fraction to float
                total_frames = int(duration * fps)
            else:
                cap.release()
                return [], 0, temp_dir
        
        duration = total_frames / fps if fps > 0 else 0
        
        # Calculate frame positions
        if num_screenshots == 1:
            frame_positions = [total_frames // 2]
        else:
            step = total_frames // (num_screenshots + 1)
            frame_positions = [step * (i + 1) for i in range(num_screenshots)]
        
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
            else:
                # Fallback: Use FFmpeg for frame extraction
                timestamp = (frame_pos / fps) if fps > 0 else (idx * duration / num_screenshots)
                screenshot_path = os.path.join(temp_dir, f"screenshot_ffmpeg_{idx+1}.jpg")
                
                cmd = [
                    'ffmpeg', '-ss', str(timestamp),
                    '-i', video_path,
                    '-vframes', '1',
                    '-q:v', '2',
                    '-y', screenshot_path
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                
                if result.returncode == 0 and os.path.exists(screenshot_path):
                    screenshots.append({
                        'path': screenshot_path,
                        'timestamp': timestamp
                    })
        
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

async def process_large_video(chat_id, file_id, file_name, file_size, message_id):
    """Process large video files with advanced techniques"""
    temp_dir = None
    video_path = None
    
    try:
        logger.info(f"Processing large video: {file_name} ({file_size/(1024*1024):.1f}MB)")
        
        # Create temp directory
        temp_dir = tempfile.mkdtemp()
        video_path = os.path.join(temp_dir, f"video_{file_id}.mp4")
        
        # Update status
        await edit_message(chat_id, message_id, 
            f"⬇️ **Downloading Large File**\n\n{create_progress_bar(0)}\n"
            f"📁 {file_name}\n💾 {file_size/(1024*1024):.1f}MB\n"
            f"⏰ This may take a while for large files...")
        
        # Try multiple download methods
        download_success = await download_large_file(file_id, video_path, chat_id, message_id)
        
        if not download_success:
            # Try alternative method
            await edit_message(chat_id, message_id, "🔄 Trying alternative download method...")
            download_success = await download_file_streaming(file_id, video_path, chat_id, message_id)
        
        if not download_success or not os.path.exists(video_path):
            await edit_message(chat_id, message_id, 
                "❌ Download failed! File might be too large or unavailable.\n\n"
                "💡 Try sending a smaller file or compressing it first.")
            return
        
        downloaded_size = os.path.getsize(video_path) / (1024 * 1024)
        logger.info(f"Successfully downloaded: {downloaded_size:.1f}MB")
        
        # Extract screenshots
        await edit_message(chat_id, message_id, 
            f"🎬 **Extracting Screenshots**\n\n{create_progress_bar(60)}")
        
        screenshots, duration, screenshot_temp_dir = extract_screenshots_efficient(video_path, 5)
        
        if not screenshots:
            await edit_message(chat_id, message_id, 
                "❌ Failed to extract screenshots!\n\n"
                "💡 The file might be corrupted or in an unsupported format.")
            return
        
        # Upload to Catbox
        await edit_message(chat_id, message_id, 
            f"📤 **Uploading to Catbox**\n\n{create_progress_bar(80)}")
        
        # Create thumbnail
        thumbnail_path = os.path.join(temp_dir, "thumbnail.jpg")
        thumbnail_created = create_thumbnail([s['path'] for s in screenshots], thumbnail_path)
        
        # Upload screenshots
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
                "large_file": True,
                "timestamp": datetime.utcnow()
            })
        except Exception as e:
            logger.error(f"MongoDB error: {e}")
        
        # Send results
        await edit_message(chat_id, message_id, 
            f"✅ **Processing Complete!**\n\n{create_progress_bar(100)}")
        
        # Send thumbnail
        if thumbnail_created and os.path.exists(thumbnail_path):
            caption = (
                f"🎬 **{file_name}**\n"
                f"⏱ {int(duration//60)}:{int(duration%60):02d}\n"
                f"📦 {file_size/(1024*1024):.1f} MB\n"
                f"🔗 Large file processed successfully!\n"
            )
            if thumbnail_url:
                caption += f"📷 {thumbnail_url}"
            
            await send_photo(chat_id, thumbnail_path, caption)
        
        # Send screenshots
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
        
        # Send URL summary
        if screenshot_urls:
            urls_text = "🔗 **All Screenshot Links:**\n\n"
            for i, url in enumerate(screenshot_urls, 1):
                urls_text += f"{i}. {url}\n"
            
            if thumbnail_url:
                urls_text += f"\n📷 **Thumbnail:** {thumbnail_url}"
            
            await send_message(chat_id, urls_text)
        
        await delete_message(chat_id, message_id)
        logger.info(f"Large video processing complete: {file_name}")
        
    except Exception as e:
        logger.error(f"Large video processing error: {e}")
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
        
        if 'message' not in data:
            return web.Response(text="OK")
        
        message = data['message']
        chat_id = message['chat']['id']
        
        # Handle commands
        if 'text' in message:
            text = message['text']
            
            if text == '/start':
                await send_message(chat_id,
                    "👋 **Welcome to Advanced Screenshot Bot!**\n\n"
                    "📹 Send any video → Get 5 screenshots\n"
                    "🔗 Catbox.moe hosting\n"
                    "⚡ Supports large files\n"
                    "🎬 Automatic processing\n\n"
                    "Commands: /start /help /stats")
                
            elif text == '/help':
                await send_message(chat_id,
                    "🤖 **How to use:**\n\n"
                    "1. Send any video file (any size)\n"
                    "2. Wait for processing\n"
                    "3. Get screenshots + URLs\n\n"
                    "📦 Supports large files\n"
                    "🎬 All formats: MP4, MKV, AVI, MOV, etc.\n"
                    "⚡ Fast processing\n"
                    "🔗 Permanent Catbox links")
                
            elif text == '/stats':
                total_count = await screenshots_collection.count_documents({})
                user_count = await screenshots_collection.count_documents({"chat_id": chat_id})
                large_files = await screenshots_collection.count_documents({
                    "chat_id": chat_id, 
                    "large_file": True
                })
                
                await send_message(chat_id,
                    f"📊 **Your Stats**\n\n"
                    f"✅ Your Videos: {user_count}\n"
                    f"📸 Your Screenshots: {user_count * 5}\n"
                    f"📦 Large Files: {large_files}\n"
                    f"🌐 Total Processed: {total_count}")
        
        # Handle video
        elif 'video' in message or 'document' in message:
            file_obj = message.get('video') or message.get('document')
            
            # Validate video
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
            
            # Send initial message
            size_info = f"💾 {file_size/(1024*1024):.1f}MB" if file_size > 0 else ""
            result = await send_message(chat_id, 
                f"⚡ **Processing Started!**\n\n{create_progress_bar(0)}\n"
                f"📁 {file_name}\n{size_info}\n\n"
                f"⏳ Please wait...")
            
            if result and 'result' in result:
                message_id = result['result']['message_id']
                
                # Process in background
                asyncio.create_task(process_large_video(chat_id, file_id, file_name, file_size, message_id))
        
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
