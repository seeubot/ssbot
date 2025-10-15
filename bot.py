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
import shutil
import hashlib

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
# NOTE: Using placeholder for security, please ensure your environment variables are set.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8268736244:AAGwfDn1Hzlor58Sg5A7cczwzRldVJNY")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://movie:movie@movie.tylkv.mongodb.net/?retryWrites=true&w=majority&appName=movie")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://confident-jemima-school1660440-5a325843.koyeb.app")
PORT = int(os.getenv("PORT", 8000))
CATBOX_UPLOAD_URL = "https://files.catbox.moe/user/api.php"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Threshold for prompting the user for confirmation (20 MB)
LARGE_FILE_THRESHOLD = 20 * 1024 * 1024

# Initialize MongoDB
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.telegram_bot
screenshots_collection = db.screenshots
pending_files_collection = db.pending_files

# Global session
session = None

# --- Utility Functions ---

def escape_markdown(text):
    """
    Escape markdown characters for MarkdownV2, excluding backticks (`)
    to allow for inline code/links to work properly.
    """
    if not isinstance(text, str):
        return ""
    # Removed ` from the list to allow inline code blocks for URLs
    escape_chars = r'_*[]()~>#+-=|{}.!'
    for char in escape_chars:
        text = text.replace(char, f'\\{char}')
    return text

def safe_filename(text):
    """Extract just the filename without path for display"""
    if not isinstance(text, str):
        return "video.mp4"
    filename = os.path.basename(text)
    safe_chars = " .-_()[]"
    cleaned = ''.join(c for c in filename if c.isalnum() or c in safe_chars)
    return cleaned if cleaned else "video.mp4"

def format_file_size(size_bytes):
    """Format file size in human readable format"""
    if size_bytes == 0:
        return "0 B"
    size_names = ["B", "KB", "MB", "GB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.1f} {size_names[i]}"

def create_progress_bar(percentage, length=10):
    """Create visual progress bar"""
    filled = int(length * percentage / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percentage:.0f}\\%" # Escaping % for MarkdownV2

def generate_file_hash(file_id):
    """Generate short hash for file_id to use in callback data"""
    return hashlib.md5(file_id.encode()).hexdigest()[:8]

async def answer_callback_query(callback_query_id, text=None):
    """Answer callback query to stop loading animation (Fixes button non-responsiveness)"""
    url = f"{TELEGRAM_API}/answerCallbackQuery"
    data = {"callback_query_id": callback_query_id}
    if text:
        data["text"] = text
    try:
        async with session.post(url, json=data, timeout=ClientTimeout(total=5)) as resp:
            return await resp.json()
    except Exception as e:
        logger.error(f"Answer callback query exception: {e}")
        return None

async def send_message(chat_id, text, reply_markup=None, parse_mode="MarkdownV2"):
    """Send message via Telegram API"""
    url = f"{TELEGRAM_API}/sendMessage"
    
    # Always escape text for markdown, unless specifically instructed not to (like for URLs in backticks)
    if parse_mode == "MarkdownV2":
        # The URL list uses backticks, which are not escaped by the modified function
        text = escape_markdown(text)
    
    data = {
        "chat_id": chat_id,
        "text": text,
    }
    
    if parse_mode:
        data["parse_mode"] = parse_mode
    
    if reply_markup:
        data["reply_markup"] = reply_markup
    
    try:
        async with session.post(url, json=data, timeout=ClientTimeout(total=30)) as resp:
            result = await resp.json()
            if not result.get('ok'):
                logger.error(f"Send message error: {result}")
                # Retry without markdown if it fails (due to escaping issues)
                if parse_mode and "can't parse entities" in str(result):
                    data.pop("parse_mode")
                    async with session.post(url, json=data, timeout=ClientTimeout(total=30)) as resp2:
                        return await resp2.json()
            return result
    except Exception as e:
        logger.error(f"Send message exception: {e}")
        return None

async def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="MarkdownV2"):
    """Edit message via Telegram API"""
    url = f"{TELEGRAM_API}/editMessageText"
    
    # Always escape text for markdown
    if parse_mode == "MarkdownV2":
        text = escape_markdown(text)
    
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    
    if parse_mode:
        data["parse_mode"] = parse_mode
        
    if reply_markup:
        data["reply_markup"] = reply_markup
    
    try:
        async with session.post(url, json=data, timeout=ClientTimeout(total=10)) as resp:
            result = await resp.json()
            if not result.get('ok') and resp.status != 400:
                logger.error(f"Edit message error: {result}")
            return result
    except Exception as e:
        logger.error(f"Edit message exception: {e}")
        return None

async def delete_message(chat_id, message_id):
    """Delete message"""
    url = f"{TELEGRAM_API}/deleteMessage"
    data = {"chat_id": chat_id, "message_id": message_id}
    try:
        async with session.post(url, json=data, timeout=ClientTimeout(total=10)) as resp:
            return await resp.json()
    except Exception as e:
        logger.error(f"Delete message exception: {e}")
        return None

async def send_photo(chat_id, photo_path, caption, parse_mode="MarkdownV2"):
    """Send photo via Telegram API"""
    url = f"{TELEGRAM_API}/sendPhoto"
    data = FormData()
    data.add_field('chat_id', str(chat_id))
    data.add_field('caption', escape_markdown(caption) if parse_mode == "MarkdownV2" else caption)
    
    if parse_mode:
        data.add_field('parse_mode', parse_mode)
    
    try:
        with open(photo_path, 'rb') as f:
            data.add_field('photo', f, filename='photo.jpg')
            async with session.post(url, data=data, timeout=ClientTimeout(total=60)) as resp:
                return await resp.json()
    except Exception as e:
        logger.error(f"Send photo exception: {e}")
        return None

async def send_media_group(chat_id, media_files, parse_mode="MarkdownV2"):
    """Send media group"""
    url = f"{TELEGRAM_API}/sendMediaGroup"
    
    data = FormData()
    data.add_field('chat_id', str(chat_id))
    
    media_array = []
    files_to_close = []
    
    for idx, item in enumerate(media_files):
        caption = escape_markdown(item['caption']) if parse_mode == "MarkdownV2" else item['caption']
        media_item = {
            "type": "photo",
            "media": f"attach://photo{idx}",
            "caption": caption,
        }
        if parse_mode:
            media_item["parse_mode"] = parse_mode
            
        media_array.append(media_item)
        f = open(item['path'], 'rb')
        files_to_close.append(f)
        data.add_field(f'photo{idx}', f, filename=f'photo{idx}.jpg')
    
    data.add_field('media', json.dumps(media_array))
    
    try:
        async with session.post(url, data=data, timeout=ClientTimeout(total=120)) as resp:
            result = await resp.json()
            for f in files_to_close:
                f.close()
            return result
    except Exception as e:
        for f in files_to_close:
            f.close()
        logger.error(f"Send media group exception: {e}")
        return None

# --- Download/Upload/Processing Functions ---

async def download_large_file(file_id, destination, chat_id, message_id):
    """Download large files with progress updates"""
    try:
        url = f"{TELEGRAM_API}/getFile"
        async with session.get(url, params={"file_id": file_id}, timeout=ClientTimeout(total=30)) as resp:
            result = await resp.json()
            
        if not result.get('ok'):
            logger.error(f"Get file failed: {result}")
            return False
        
        file_path = result['result']['file_path']
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        
        async with session.get(download_url, timeout=ClientTimeout(total=1800)) as resp:
            if resp.status == 200:
                total_size = int(resp.headers.get('content-length', 0))
                downloaded = 0
                file_name = safe_filename(destination)
                
                with open(destination, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if total_size > 0:
                                progress = (downloaded / total_size) * 100
                                # Update progress from 0% to 40% (reserving 40-100 for processing)
                                scaled_progress = int(progress * 0.4)
                                if int(progress) % 10 == 0:  # Update every 10% of download
                                    await edit_message(chat_id, message_id, 
                                        f"⬇️ Downloading File\n\n{create_progress_bar(scaled_progress)}\n"
                                        f"📁 {file_name}\n"
                                        f"💾 {downloaded/(1024*1024):.1f}MB / {total_size/(1024*1024):.1f}MB")
                
                return True
        return False
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False

async def upload_to_catbox(file_path):
    """Upload to Catbox.moe for file sharing"""
    try:
        data = FormData()
        data.add_field('reqtype', 'fileupload')
        
        with open(file_path, 'rb') as f:
            data.add_field('fileToUpload', f, filename=os.path.basename(file_path))
            
            async with session.post(CATBOX_UPLOAD_URL, data=data, timeout=ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    url = await resp.text()
                    if url.startswith("https://"):
                        return url.strip()
                    else:
                        logger.error(f"Catbox returned non-URL: {url}")
                        return None
    except Exception as e:
        logger.error(f"Catbox upload error: {e}")
    return None

async def reduce_video_size(input_path, output_path, reduction_percentage):
    """Reduce video size by specified percentage using FFmpeg"""
    logger.info(f"Starting size reduction: {reduction_percentage}%")
    
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        return False
    
    try:
        # Test if ffmpeg is available
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("FFmpeg not available, skipping optimization")
            return False
    except Exception as e:
        logger.warning(f"FFmpeg check failed: {e}, skipping optimization")
        return False
    
    # Adjust video quality based on reduction percentage
    if reduction_percentage == 30:
        # Mild reduction - maintain good quality, target CRF 23
        cmd = [
            'ffmpeg', '-i', input_path, '-y',
            '-c:v', 'libx264', '-crf', '25', '-preset', 'medium',
            '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart',
            output_path
        ]
    elif reduction_percentage == 50:
        # Medium reduction - balance quality and size, target CRF 28
        cmd = [
            'ffmpeg', '-i', input_path, '-y',
            '-c:v', 'libx264', '-crf', '29', '-preset', 'fast',
            '-c:a', 'aac', '-b:a', '96k',
            '-movflags', '+faststart',
            output_path
        ]
    elif reduction_percentage == 70:
        # Aggressive reduction - maximum size reduction, target CRF 32 and lower resolution
        cmd = [
            'ffmpeg', '-i', input_path, '-y',
            '-vf', 'scale=854:-2',  # Scale to 480p equivalent
            '-c:v', 'libx264', '-crf', '33', '-preset', 'veryfast',
            '-c:a', 'aac', '-b:a', '64k',
            '-movflags', '+faststart',
            output_path
        ]
    else:
        logger.error(f"Invalid reduction percentage: {reduction_percentage}")
        return False
        
    try:
        logger.info(f"Running FFmpeg for {reduction_percentage}% reduction")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # NOTE: Not reading output for progress as it's complex, just waiting for completion
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0 and os.path.exists(output_path):
            logger.info(f"Size reduction successful for {reduction_percentage}%")
            return True
        else:
            logger.error(f"FFmpeg failed with code {process.returncode}")
            logger.error(f"FFmpeg stderr: {stderr.decode()}")
            return False
            
    except Exception as e:
        logger.error(f"FFmpeg execution error: {e}")
        return False

# Other video utilities remain the same
# ... (get_video_duration, extract_screenshots_efficient, create_comprehensive_thumbnail) ...

async def get_video_duration(video_path):
    """Get video duration using FFprobe"""
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', video_path
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            info = json.loads(stdout.decode())
            duration = float(info['format']['duration'])
            return duration
    except Exception as e:
        logger.error(f"Error getting video duration: {e}")
    
    return 0

def extract_screenshots_efficient(video_path, num_screenshots=5):
    """Extract screenshots from video at equal intervals"""
    screenshots = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.error(f"Cannot open video: {video_path}")
            return [], 0, temp_dir
        
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        if fps <= 0 or total_frames <= 0:
            cap.set(cv2.CAP_PROP_POS_AVI_RATIO, 1)
            duration = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            cap.set(cv2.CAP_PROP_POS_AVI_RATIO, 0)
            fps = 25
            total_frames = int(duration * fps)
        else:
            duration = total_frames / fps
        
        logger.info(f"Video info - Frames: {total_frames}, FPS: {fps:.2f}, Duration: {duration:.2f}s")
        
        if total_frames > 0:
            frame_positions = []
            for i in range(num_screenshots):
                position = int((i + 1) * total_frames / (num_screenshots + 1))
                frame_positions.append(min(position, total_frames - 1))
        else:
            frame_positions = []

        logger.info(f"Extracting screenshots at positions: {frame_positions}")
        
        for idx, frame_pos in enumerate(frame_positions):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_pos)
            ret, frame = cap.read()
            
            if ret and frame is not None:
                screenshot_path = os.path.join(temp_dir, f"screenshot_{idx+1}.jpg")
                success = cv2.imwrite(screenshot_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                
                if success and os.path.exists(screenshot_path):
                    timestamp = frame_pos / fps if fps > 0 else (idx * duration / num_screenshots)
                    screenshots.append({
                        'path': screenshot_path,
                        'timestamp': timestamp
                    })
                    logger.info(f"Extracted screenshot {idx+1} at {timestamp:.2f}s")
                else:
                    logger.warning(f"Failed to write screenshot {idx+1}")
            else:
                logger.warning(f"Could not read frame at position {frame_pos}")
        
        cap.release()
        logger.info(f"Successfully extracted {len(screenshots)} screenshots")
        return screenshots, duration, temp_dir
        
    except Exception as e:
        logger.error(f"Screenshot extraction error: {e}")
        try:
            cap.release()
        except:
            pass
        return [], 0, temp_dir

def create_comprehensive_thumbnail(video_path, output_path, num_frames=9):
    """Create a comprehensive thumbnail with multiple frames from the entire video"""
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.error(f"Cannot open video for thumbnail: {video_path}")
            return None
        
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        if total_frames <= 0:
            cap.set(cv2.CAP_PROP_POS_AVI_RATIO, 1)
            duration_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            cap.set(cv2.CAP_PROP_POS_AVI_RATIO, 0)
            total_frames = int((duration_ms / 1000) * (fps if fps > 0 else 25))
        
        frames = []
        frame_positions = []
        
        # Get frames from throughout the entire video
        for i in range(num_frames):
            position = int((i + 0.5) * total_frames / num_frames)  # Spread evenly
            frame_positions.append(min(position, total_frames - 1))
        
        for pos in frame_positions:
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
            ret, frame = cap.read()
            if ret and frame is not None:
                # Resize frame to thumbnail size
                frame = cv2.resize(frame, (200, 150))
                frames.append(frame)
        
        cap.release()
        
        if not frames:
            logger.error("No frames extracted for thumbnail")
            return None
        
        # Create grid layout
        cols = 3
        rows = math.ceil(len(frames) / cols)
        width = 200 * cols
        height = 150 * rows
        
        thumbnail = Image.new('RGB', (width, height), (0, 0, 0))
        
        for idx, frame in enumerate(frames):
            # Convert BGR to RGB
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb)
            
            x = (idx % cols) * 200
            y = (idx // cols) * 150
            thumbnail.paste(img, (x, y))
        
        thumbnail.save(output_path, 'JPEG', quality=90)
        logger.info(f"Created comprehensive thumbnail with {len(frames)} frames")
        return output_path
        
    except Exception as e:
        logger.error(f"Comprehensive thumbnail error: {e}")
        return None

async def process_video_final_steps(chat_id, video_path, original_file_name, original_file_size, message_id, reduction_percentage=None):
    """Final processing steps after download/optimization"""
    temp_dir = os.path.dirname(video_path)
    
    try:
        safe_file_name = safe_filename(original_file_name)
        current_size = os.path.getsize(video_path)
        
        # Extract screenshots (Progress 50% to 70%)
        await edit_message(chat_id, message_id, 
            f"🎬 Extracting Screenshots\n\n{create_progress_bar(60)}\n"
            f"📁 {safe_file_name}")
        
        screenshots, duration, screenshot_temp_dir = await asyncio.to_thread(
            extract_screenshots_efficient, video_path, 5
        )
        
        if not screenshots:
            await edit_message(chat_id, message_id, 
                f"❌ Failed to extract screenshots from {safe_file_name}")
            return
        
        # Create comprehensive thumbnail from video (Progress 70% to 80%)
        await edit_message(chat_id, message_id, 
            f"🖼️ Creating Thumbnail\n\n{create_progress_bar(75)}\n"
            f"📁 {safe_file_name}")
        
        comprehensive_thumbnail_path = os.path.join(temp_dir, "comprehensive_thumbnail.jpg")
        comprehensive_thumbnail = await asyncio.to_thread(
            create_comprehensive_thumbnail, video_path, comprehensive_thumbnail_path, 9
        )
        
        final_thumbnail_path = comprehensive_thumbnail_path if comprehensive_thumbnail else None
        
        # Upload to Catbox (Progress 80% to 100%)
        await edit_message(chat_id, message_id, 
            f"📤 Uploading to Catbox\n\n{create_progress_bar(85)}\n"
            f"📁 {safe_file_name}")
        
        upload_tasks = []
        for ss in screenshots:
            upload_tasks.append(upload_to_catbox(ss['path']))
        
        thumbnail_url = None
        if final_thumbnail_path and os.path.exists(final_thumbnail_path):
            thumbnail_url = await upload_to_catbox(final_thumbnail_path)
        
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)
        screenshot_urls = [url for url in results if isinstance(url, str) and url]
        
        # Save to database
        try:
            await screenshots_collection.insert_one({
                "chat_id": chat_id,
                "file_name": original_file_name,
                "original_file_size": original_file_size,
                "final_video_size": current_size,
                "duration": duration,
                "screenshot_urls": screenshot_urls,
                "thumbnail_url": thumbnail_url,
                "reduction_percentage": reduction_percentage,
                "large_file": original_file_size > LARGE_FILE_THRESHOLD,
                "timestamp": datetime.utcnow()
            })
        except Exception as e:
            logger.error(f"MongoDB save error: {e}")
        
        # Send completion message
        await edit_message(chat_id, message_id, 
            f"✅ Processing Complete\n\n{create_progress_bar(100)}")
        
        # Send comprehensive thumbnail
        if final_thumbnail_path and os.path.exists(final_thumbnail_path):
            size_info = ""
            if reduction_percentage is not None:
                if original_file_size > 0:
                    actual_reduction = ((original_file_size - current_size) / original_file_size) * 100
                    size_info = f"📉 Target: {reduction_percentage}\\% | Actual: {actual_reduction:.1f}\\%"
                else:
                    size_info = f"📉 Target: {reduction_percentage}\\%"
            
            caption = (
                f"🎬 {safe_file_name}\n"
                f"⏱ {int(duration//60)}:{int(duration%60):02d}\n"
                f"📦 Original: {format_file_size(original_file_size)}\n"
                f"💾 Final: {format_file_size(current_size)}\n"
                f"{size_info}\n"
                f"🔗 {len(screenshot_urls)} screenshots uploaded"
            )
            if thumbnail_url:
                caption += f"\n🖼️ Thumbnail: `{thumbnail_url}`"
            
            await send_photo(chat_id, final_thumbnail_path, caption)
        
        # Send screenshots
        if screenshots:
            media_files = []
            for idx, ss in enumerate(screenshots):
                m = int(ss['timestamp'] // 60)
                s = int(ss['timestamp'] % 60)
                # Caption formatting for media group
                cap = f"📸 {idx+1}/{len(screenshots)} - {m:02d}:{s:02d}"
                if idx < len(screenshot_urls) and screenshot_urls[idx]:
                    # Using backticks for the URL in the caption
                    cap += f"\n🔗 `{screenshot_urls[idx]}`"
                media_files.append({'path': ss['path'], 'caption': cap})
            
            # Telegram limits media groups to 10. Sending 5, so it's fine.
            await send_media_group(chat_id, media_files)
        
        # Send URL summary
        if screenshot_urls:
            urls_text = "📸 *All Screenshot Links*\n\n"
            for i, url in enumerate(screenshot_urls, 1):
                # Using backticks for inline code block for clear link display
                urls_text += f"{i}\\. `{url}`\n"
            
            await send_message(chat_id, urls_text)
        
        await delete_message(chat_id, message_id)
        logger.info(f"Processing complete: {original_file_name}")
        
    except Exception as e:
        logger.error(f"Final processing error: {e}")
        await edit_message(chat_id, message_id, 
            f"❌ Processing Error\n\nError: {escape_markdown(str(e)[:200])}")
    
    finally:
        # Cleanup
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

async def process_video_download_and_reduce(chat_id, file_id, file_name, file_size, message_id, reduction_percentage):
    """Main processing function with size reduction"""
    temp_dir = tempfile.mkdtemp()
    original_path = os.path.join(temp_dir, f"original_{file_id}.mp4")
    reduced_path = os.path.join(temp_dir, f"reduced_{file_id}.mp4")
    
    try:
        safe_file_name = safe_filename(file_name)
        
        # Download file
        # Progress updates from 0% to 40% are handled within download_large_file
        await edit_message(chat_id, message_id, 
            f"⬇️ Downloading File\n\n{create_progress_bar(0)}\n"
            f"📁 {safe_file_name}\n"
            f"💾 {format_file_size(file_size)}")
        
        if not await download_large_file(file_id, original_path, chat_id, message_id):
            await edit_message(chat_id, message_id, "❌ Download failed")
            return
        
        downloaded_size = os.path.getsize(original_path)
        final_path = original_path
        
        # Reduce file size if percentage is specified (Progress 40% to 50%)
        if reduction_percentage is not None and reduction_percentage > 0:
            await edit_message(chat_id, message_id, 
                f"⚙️ Reducing File Size\n\n{create_progress_bar(45)}\n"
                f"Target: {reduction_percentage}\\% reduction\n"
                f"📁 {safe_file_name}")
            
            if await reduce_video_size(original_path, reduced_path, reduction_percentage):
                final_path = reduced_path
                reduced_size = os.path.getsize(reduced_path)
                actual_reduction = ((downloaded_size - reduced_size) / downloaded_size) * 100
                logger.info(f"Size reduced: {format_file_size(reduced_size)} (reduced by {actual_reduction:.1f}%)")
                
                # Show compressed size after reduction (User request addressed here)
                await edit_message(chat_id, message_id, 
                    f"✅ Size Reduction Complete\n\n{create_progress_bar(50)}\n"
                    f"Target Reduction: {reduction_percentage}\\%\\n"
                    f"Original Size: {format_file_size(downloaded_size)}\\n"
                    f"Reduced Size: {format_file_size(reduced_size)} ({actual_reduction:.1f}\\% less)\\n"
                    f"📁 {safe_file_name}")
            else:
                logger.warning("Size reduction failed, using original file")
                await edit_message(chat_id, message_id, 
                    f"⚠️ Size reduction failed, proceeding with original file\n\n"
                    f"📁 {safe_file_name}\n"
                    f"{create_progress_bar(50)}")
        else:
            # No reduction, update progress bar to 50% for next step
             await edit_message(chat_id, message_id, 
                f"✅ Download Complete\n\n{create_progress_bar(50)}\n"
                f"📁 {safe_file_name}")
        
        # Process final steps (Starts from 50% progress conceptually)
        await process_video_final_steps(chat_id, final_path, file_name, file_size, message_id, reduction_percentage)
        
    except Exception as e:
        logger.error(f"Processing error: {e}")
        await edit_message(chat_id, message_id, 
            f"❌ Processing Error\n\nError: {escape_markdown(str(e)[:200])}")
    
    finally:
        # Cleanup
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

# --- Webhook Handlers ---

async def handle_callback_query(update):
    """Handle callback queries from inline keyboards"""
    callback_query = update['callback_query']
    data = callback_query['data']
    chat_id = callback_query['message']['chat']['id']
    message_id = callback_query['message']['message_id']
    callback_query_id = callback_query['id'] # Get the callback query ID
    
    logger.info(f"Callback received: {data}")
    
    # IMMEDIATELY answer the query to fix button non-responsiveness
    await answer_callback_query(callback_query_id, text="Request accepted. Starting download...")
    
    if data.startswith('r30_') or data.startswith('r50_') or data.startswith('r70_'):
        # Parse reduction callback
        reduction_char = data[1]  # Get '3', '5', or '7'
        file_hash = data[3:]      # Get the file hash
        
        reduction_percentage = int(reduction_char + '0')  # Convert to 30, 50, 70
        
        # Find file by hash
        file_metadata = await pending_files_collection.find_one({"file_hash": file_hash})
        if not file_metadata:
            await edit_message(chat_id, message_id, "❌ File data expired or invalid")
            return

        file_id = file_metadata['file_id']
        file_name = file_metadata['file_name']
        file_size = file_metadata['file_size']
        safe_name = safe_filename(file_name)
        
        # Delete from pending collection to prevent reuse
        await pending_files_collection.delete_one({"file_hash": file_hash})
        
        await edit_message(chat_id, message_id, 
            f"✅ Starting {reduction_percentage}\\% size reduction\n"
            f"📁 {safe_name}\n"
            f"📦 Original size: {format_file_size(file_size)}")
        
        # Start processing with size reduction
        asyncio.create_task(
            process_video_download_and_reduce(
                chat_id, file_id, file_name, file_size, message_id, reduction_percentage
            )
        )
    
    elif data.startswith('proc_'):
        # Process without reduction
        file_hash = data[5:]  # Get the file hash
        
        # Find file by hash
        file_metadata = await pending_files_collection.find_one({"file_hash": file_hash})
        if not file_metadata:
            await edit_message(chat_id, message_id, "❌ File data expired or invalid")
            return

        file_id = file_metadata['file_id']
        file_name = file_metadata['file_name']
        file_size = file_metadata['file_size']
        
        # Delete from pending collection to prevent reuse
        await pending_files_collection.delete_one({"file_hash": file_hash})
        
        await edit_message(chat_id, message_id, 
            f"✅ Starting processing without size reduction\n"
            f"📁 {safe_filename(file_name)}\n"
            f"📦 Size: {format_file_size(file_size)}")
        
        asyncio.create_task(
            process_video_download_and_reduce(
                chat_id, file_id, file_name, file_size, message_id, None
            )
        )

async def handle_webhook(request):
    """Main webhook handler"""
    try:
        data = await request.json()
        logger.info(f"Webhook received: {list(data.keys())}")
        
        if 'message' in data:
            message = data['message']
            chat_id = message['chat']['id']
            
            # Handle text commands
            if 'text' in message:
                text = message['text']
                
                if text == '/start':
                    await send_message(chat_id, 
                        "🎬 *Welcome to Video Screenshot Bot\\!*\n\n"
                        "I can help you:\n"
                        "• Extract screenshots from videos\n"
                        "• Reduce video file size\n"
                        "• Upload screenshots to cloud\n\n"
                        "Simply send me any video file to get started\\!")
                elif text == '/help':
                    await send_message(chat_id, 
                        "📖 *How to use this bot:*\n\n"
                        "1\\. Send a video file\n"
                        "2\\. For files over 20MB, choose size reduction option\n"
                        "3\\. Wait for processing\n"
                        "4\\. Get screenshots and download links\n\n"
                        "*Size Reduction Options:*\n"
                        "• *30\\%* \\- Good quality, mild reduction\n"
                        "• *50\\%* \\- Balanced quality and size\n"
                        "• *70\\%* \\- Maximum size reduction\n\n"
                        "All screenshots are uploaded to Catbox\\.moe for easy sharing\\!")
                elif text == '/stats':
                    total = await screenshots_collection.count_documents({})
                    user_total = await screenshots_collection.count_documents({"chat_id": chat_id})
                    await send_message(chat_id, 
                        f"📊 *Your Statistics:*\n"
                        f"• Videos processed: {user_total}\n"
                        f"• Total screenshots: {user_total * 5}\n"
                        f"• Global total: {total}")
            
            # Handle video/files
            elif 'video' in message or 'document' in message:
                file_obj = message.get('video') or message.get('document')
                file_id = file_obj['file_id']
                file_name = file_obj.get('file_name', 'video.mp4')
                file_size = file_obj.get('file_size', 0)
                
                logger.info(f"File received: {file_name} ({file_size} bytes)")
                
                # Check if it's a video file
                if 'document' in message:
                    mime_type = file_obj.get('mime_type', '')
                    if not mime_type.startswith('video/'):
                        await send_message(chat_id, "❌ Please send a video file (MP4, AVI, MKV, etc\\.)")
                        return web.Response(text="OK")
                
                safe_name = safe_filename(file_name)
                formatted_size = format_file_size(file_size)
                
                # Generate short hash for callback data
                file_hash = generate_file_hash(file_id)
                
                # Store file info for callback
                await pending_files_collection.update_one(
                    {"file_id": file_id},
                    {"$set": {
                        "chat_id": chat_id,
                        "file_name": file_name,
                        "file_size": file_size,
                        "file_hash": file_hash,
                        "timestamp": datetime.utcnow()
                    }},
                    upsert=True
                )
                
                if file_size > LARGE_FILE_THRESHOLD:
                    # Large file - show size reduction options
                    reply_markup = {
                        "inline_keyboard": [
                            [
                                {"text": "🔻 Reduce 30%", "callback_data": f"r30_{file_hash}"},
                                {"text": "🔻 Reduce 50%", "callback_data": f"r50_{file_hash}"}
                            ],
                            [
                                {"text": "🔻 Reduce 70%", "callback_data": f"r70_{file_hash}"},
                                {"text": "⚡ Process Original", "callback_data": f"proc_{file_hash}"}
                            ]
                        ]
                    }
                    
                    await send_message(chat_id,
                        f"📦 *Large File Detected*\n\n"
                        f"📁 *File:* {safe_name}\n"
                        f"💾 *Size:* {formatted_size}\n\n"
                        f"Choose size reduction option:\n"
                        f"• *30\\%* \\- Mild reduction, best quality\n"
                        f"• *50\\%* \\- Balanced quality/size\n"
                        f"• *70\\%* \\- Maximum size reduction\n"
                        f"• *Original* \\- No size reduction",
                        reply_markup=reply_markup)
                    
                else:
                    # Small file - process immediately without reduction
                    result = await send_message(chat_id,
                        f"⚡ *Processing Started*\n\n{create_progress_bar(0)}\n"
                        f"📁 {safe_name}\n"
                        f"💾 Size: {formatted_size}\n\n"
                        f"⏳ Extracting screenshots...")
                    
                    if result and 'result' in result:
                        message_id = result['result']['message_id']
                        asyncio.create_task(
                            process_video_download_and_reduce(
                                chat_id, file_id, file_name, file_size, message_id, None
                            )
                        )
        
        elif 'callback_query' in data:
            await handle_callback_query(data)

        return web.Response(text="OK")
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(text="OK")

async def health_check(request):
    """Health check endpoint"""
    return web.Response(text="OK", status=200)

async def setup_webhook():
    """Setup webhook on startup"""
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL not set!")
        return
        
    webhook_url = f"{WEBHOOK_URL}/webhook"
    url = f"{TELEGRAM_API}/setWebhook"
    data = {"url": webhook_url}
    
    try:
        async with session.post(url, json=data) as resp:
            result = await resp.json()
            logger.info(f"Webhook setup: {result}")
    except Exception as e:
        logger.error(f"Webhook setup error: {e}")

async def start_server(app):
    """Startup function"""
    global session
    session = ClientSession()
    await setup_webhook()
    logger.info("🤖 Video Screenshot Bot started successfully!")
    logger.info(f"🌐 Webhook URL: {WEBHOOK_URL}")
    logger.info(f"🚀 Server running on port: {PORT}")

async def cleanup(app):
    """Cleanup function"""
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
    logger.info("🎬 Starting Video Screenshot Bot...")
    # NOTE: You need to have 'ffmpeg' and 'ffprobe' installed and in your system PATH for video processing to work.
    web.run_app(app, host='0.0.0.0', port=PORT)

