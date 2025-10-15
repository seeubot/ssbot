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
# NEW: Collection for temporary state storage during confirmation
pending_files_collection = db.pending_files

# Global session
session = None

# --- Utility Functions ---

def escape_markdown(text):
    """
    Escapes characters that have special meaning in Markdown V2.
    More comprehensive escaping to fix all parsing errors.
    """
    if not isinstance(text, str):
        return ""
    
    # More comprehensive list of characters to escape
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    
    # Escape each character
    for char in escape_chars:
        text = text.replace(char, f'\\{char}')
    
    return text

def safe_markdown_text(text):
    """
    Apply markdown escaping and ensure the text is safe for Telegram.
    This is a wrapper that can be used for all user-facing text.
    """
    return escape_markdown(text)

def create_progress_bar(percentage, length=10):
    """Create visual progress bar"""
    filled = int(length * percentage / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percentage:.0f}%"

async def send_message(chat_id, text, reply_markup=None, parse_mode="MarkdownV2"):
    """Send message via Telegram API with detailed error logging"""
    url = f"{TELEGRAM_API}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
    }
    
    if parse_mode:
        data["parse_mode"] = parse_mode
    
    if reply_markup:
        data["reply_markup"] = reply_markup
    
    max_retries = 2
    for attempt in range(max_retries):
        try:
            async with session.post(url, json=data, timeout=ClientTimeout(total=30)) as resp:
                result = await resp.json()
                if resp.status == 200 and result.get('ok', False):
                    return result
                else:
                    logger.error(f"Telegram API Error (sendMessage) attempt {attempt + 1}: Status={resp.status}, Body={result}")
                    
                    # If markdown parsing fails, retry without markdown
                    if "can't parse entities" in str(result) and parse_mode:
                        logger.info("Retrying without markdown parsing...")
                        data.pop("parse_mode", None)
                        continue
                    
                    return None
        except Exception as e:
            logger.error(f"Send message exception (attempt {attempt + 1}): {e}")
            if attempt == max_retries - 1:
                return None
    
    return None

async def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="MarkdownV2"):
    """Edit message via Telegram API with detailed error logging"""
    url = f"{TELEGRAM_API}/editMessageText"
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    
    if parse_mode:
        data["parse_mode"] = parse_mode
        
    if reply_markup:
        data["reply_markup"] = reply_markup
    
    max_retries = 2
    for attempt in range(max_retries):
        try:
            async with session.post(url, json=data, timeout=ClientTimeout(total=10)) as resp:
                result = await resp.json()
                if resp.status == 200 or resp.status == 400:  # 400 for "message not modified"
                    return result
                else:
                    logger.error(f"Telegram API Error (editMessageText) attempt {attempt + 1}: Status={resp.status}, Body={result}")
                    
                    # If markdown parsing fails, retry without markdown
                    if "can't parse entities" in str(result) and parse_mode:
                        logger.info("Retrying without markdown parsing...")
                        data.pop("parse_mode", None)
                        continue
                    
                    return None
        except Exception as e:
            logger.error(f"Edit message exception (attempt {attempt + 1}): {e}")
            if attempt == max_retries - 1:
                return None
    
    return None

async def delete_message(chat_id, message_id):
    """Delete message with detailed error logging"""
    url = f"{TELEGRAM_API}/deleteMessage"
    data = {"chat_id": chat_id, "message_id": message_id}
    try:
        async with session.post(url, json=data, timeout=ClientTimeout(total=10)) as resp:
            result = await resp.json()
            if resp.status != 200 and not result.get('ok', False):
                logger.warning(f"Telegram API Error (deleteMessage): Status={resp.status}, Body={result}")
                return None
            return result
    except Exception as e:
        logger.error(f"Delete message exception: {e}")
        return None

async def send_photo(chat_id, photo_path, caption, parse_mode="MarkdownV2"):
    """Send photo via Telegram API with detailed error logging"""
    url = f"{TELEGRAM_API}/sendPhoto"
    data = FormData()
    data.add_field('chat_id', str(chat_id))
    data.add_field('caption', caption)
    
    if parse_mode:
        data.add_field('parse_mode', parse_mode)
    
    try:
        with open(photo_path, 'rb') as f:
            data.add_field('photo', f, filename='photo.jpg')
            async with session.post(url, data=data, timeout=ClientTimeout(total=60)) as resp:
                result = await resp.json()
                if resp.status != 200 or not result.get('ok', False):
                    logger.error(f"Telegram API Error (sendPhoto): Status={resp.status}, Body={result}")
                    return None
                return result
    except Exception as e:
        logger.error(f"Send photo exception: {e}")
        return None

async def send_media_group(chat_id, media_files, parse_mode="MarkdownV2"):
    """Send media group with detailed error logging"""
    url = f"{TELEGRAM_API}/sendMediaGroup"
    
    data = FormData()
    data.add_field('chat_id', str(chat_id))
    
    media_array = []
    files_to_close = []
    
    for idx, item in enumerate(media_files):
        media_item = {
            "type": "photo",
            "media": f"attach://photo{idx}",
            "caption": item['caption'],
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
            # Close file handles regardless of API success
            for f in files_to_close:
                f.close()
            
            if resp.status != 200 or not result.get('ok', False):
                logger.error(f"Telegram API Error (sendMediaGroup): Status={resp.status}, Body={result}")
                return None
            return result
    except Exception as e:
        # Ensure file handles are closed even if a network exception occurs
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
                file_name = safe_markdown_text(os.path.basename(destination))
                
                with open(destination, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if total_size > 0:
                                progress = (downloaded / total_size) * 100
                                # Update progress every 5%
                                if int(progress) % 5 == 0 and progress > 0:
                                    await edit_message(chat_id, message_id, 
                                        f"⬇️ \\*\\*Downloading\\*\\*\n\n{create_progress_bar(progress)}\n"
                                        f"📁 {file_name}\n"
                                        f"💾 {downloaded/(1024*1024):.1f}MB / {total_size/(1024*1024):.1f}MB")
                
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

async def optimize_video(input_path, output_path, strategy):
    """
    Transcode video using FFmpeg to reduce file size.
    Strategy 'fast' is quicker; 'quality' is more aggressive size reduction (lower res).
    """
    logger.info(f"Starting optimization strategy: {strategy}")
    
    # Check if FFmpeg is available
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.error("FFmpeg not found! Skipping optimization.")
        return False
    
    # Common settings for H.264 video encoding
    base_cmd = [
        'ffmpeg', '-i', input_path, '-y',
        '-c:v', 'libx264', '-crf', '28', # Constant Rate Factor for quality-based compression
        '-c:a', 'aac', '-b:a', '128k', # AAC audio at 128kbps
        '-pix_fmt', 'yuv420p'
    ]
    
    if strategy == 'fast':
        # Approach 1: Fast, High-Speed Re-encoding
        # Uses a very fast preset and attempts to enforce a maximum bitrate
        cmd = base_cmd + [
            '-preset', 'superfast',
            '-maxrate', '1500k', # Limit bitrate to 1.5 Mbps
            '-bufsize', '3000k',
            output_path
        ]
    elif strategy == 'quality':
        # Approach 2: Aggressive Size Reduction via Resolution/Preset
        # Scales down the resolution to 720p (if larger) for significant size savings.
        cmd = base_cmd + [
            '-vf', 'scale=min(1280\\,iw):-2', # Scale to max 720p height/width
            '-preset', 'fast',
            '-maxrate', '1000k', # Limit bitrate to 1 Mbps
            '-bufsize', '2000k',
            output_path
        ]
    else:
        logger.error(f"Unknown optimization strategy: {strategy}")
        return False
        
    try:
        # Use subprocess to run FFmpeg
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # FFmpeg prints progress to stderr
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0 and os.path.exists(output_path):
            logger.info(f"Optimization successful. New size: {os.path.getsize(output_path)/(1024*1024):.1f}MB")
            return True
        else:
            logger.error(f"FFmpeg failed with return code {process.returncode}. Stderr: {stderr.decode()}")
            return False
            
    except Exception as e:
        logger.error(f"FFmpeg execution error: {e}")
        return False

def extract_screenshots_efficient(video_path, num_screenshots=5):
    """Efficient screenshot extraction with error handling and fallback"""
    screenshots = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.error(f"Could not open video file: {video_path}")
            return [], 0, temp_dir
            
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        duration = 0
        
        # If OpenCV can't get proper info, try to estimate
        if fps <= 0 or total_frames <= 0:
            logger.warning("OpenCV couldn't read video metadata, using fallback methods")
            
            # Method 1: Try with cv2.CAP_PROP_POS_MSEC
            cap.set(cv2.CAP_PROP_POS_AVI_RATIO, 1)
            duration_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            cap.set(cv2.CAP_PROP_POS_AVI_RATIO, 0)  # Reset to beginning
            
            if duration_ms > 0:
                duration = duration_ms / 1000.0
                fps = 25  # Assume standard fps
                total_frames = int(duration * fps)
            else:
                # Method 2: Use file size estimation (very rough)
                file_size = os.path.getsize(video_path)
                # Rough estimation: 1MB ≈ 1 second for compressed video
                duration = file_size / (1024 * 1024)  
                fps = 25
                total_frames = int(duration * fps)
                logger.warning(f"Using rough duration estimation: {duration:.1f}s")
        else:
            duration = total_frames / fps if fps > 0 else 0
        
        logger.info(f"Video info - Frames: {total_frames}, FPS: {fps:.2f}, Duration: {duration:.2f}s")
        
        if total_frames > 0:
            if num_screenshots == 1:
                frame_positions = [total_frames // 2]
            else:
                step = total_frames // (num_screenshots + 1)
                frame_positions = [step * (i + 1) for i in range(num_screenshots)]
        else:
            # If we still don't have frames, take screenshots at time intervals
            if duration > 0:
                time_step = duration / (num_screenshots + 1)
                frame_positions = []
                for i in range(num_screenshots):
                    time_pos = time_step * (i + 1)
                    # Convert time to approximate frame position
                    frame_pos = int(time_pos * fps) if fps > 0 else 0
                    frame_positions.append(frame_pos)
            else:
                frame_positions = []

        logger.info(f"Extracting screenshots at positions: {frame_positions}")
        
        screenshots_extracted = 0
        for idx, frame_pos in enumerate(frame_positions):
            if frame_pos > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_pos))
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
                    screenshots_extracted += 1
                else:
                    logger.warning(f"Failed to write screenshot {idx+1}")
            else:
                logger.warning(f"Could not read frame at position {frame_pos}")
        
        cap.release()
        logger.info(f"Successfully extracted {screenshots_extracted} screenshots")
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

async def process_video_final_steps(chat_id, video_path, original_file_name, original_file_size, message_id):
    """
    Perform screenshot extraction, upload, and sending results.
    This runs AFTER the video is downloaded and potentially optimized.
    """
    temp_dir = os.path.dirname(video_path)
    
    try:
        escaped_file_name = safe_markdown_text(original_file_name)
        
        # 1. Extract screenshots
        await edit_message(chat_id, message_id, 
            f"🎬 \\*\\*Extracting Screenshots\\*\\*\n\n{create_progress_bar(60)}\n"
            f"📁 {escaped_file_name}")
        
        screenshots, duration, screenshot_temp_dir = await asyncio.to_thread(
            extract_screenshots_efficient, video_path, 5
        )
        
        if not screenshots:
            await edit_message(chat_id, message_id, 
                f"❌ Failed to extract screenshots from {escaped_file_name}\\. File might be corrupted or unsupported\\.")
            return
        
        # 2. Upload to Catbox (Thumbnail & Screenshots)
        await edit_message(chat_id, message_id, 
            f"📤 \\*\\*Uploading to Catbox\\*\\*\n\n{create_progress_bar(80)}\n"
            f"📁 {escaped_file_name}")
        
        thumbnail_path = os.path.join(temp_dir, "thumbnail.jpg")
        thumbnail_created = await asyncio.to_thread(
            create_thumbnail, [s['path'] for s in screenshots], thumbnail_path
        )
        
        upload_tasks = [upload_to_catbox(ss['path']) for ss in screenshots]
        if thumbnail_created:
            upload_tasks.append(upload_to_catbox(thumbnail_path))
        
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)
        screenshot_urls = [url for url in results[:5] if isinstance(url, str) and url]
        thumbnail_url = results[-1] if thumbnail_created and len(results) > 5 and isinstance(results[-1], str) else None
        
        # 3. Save to database
        try:
            current_size = os.path.getsize(video_path)
            await screenshots_collection.insert_one({
                "chat_id": chat_id,
                "file_name": original_file_name,
                "original_file_size": original_file_size,
                "final_video_size": current_size,
                "duration": duration,
                "screenshot_urls": screenshot_urls,
                "thumbnail_url": thumbnail_url,
                "large_file": original_file_size > LARGE_FILE_THRESHOLD,
                "timestamp": datetime.utcnow()
            })
        except Exception as e:
            logger.error(f"MongoDB save error: {e}")
        
        # 4. Send results
        await edit_message(chat_id, message_id, 
            f"✅ \\*\\*Processing Complete\\*\\*\n\n{create_progress_bar(100)}")
        
        # Send thumbnail
        if thumbnail_created and os.path.exists(thumbnail_path):
            caption = (
                f"🎬 \\*\\*{escaped_file_name}\\*\\*\n"
                f"⏱ {int(duration//60)}:{int(duration%60):02d}\n"
                f"📦 Original: {original_file_size/(1024*1024):.1f} MB \\| Final: {current_size/(1024*1024):.1f} MB\n"
                f"🔗 Processed successfully\\.\n"
            )
            if thumbnail_url:
                caption += f"📷 Thumbnail URL: {safe_markdown_text(thumbnail_url)}"
            
            await send_photo(chat_id, thumbnail_path, caption)
        
        # Send screenshots
        if screenshots:
            media_files = []
            for idx, ss in enumerate(screenshots):
                m = int(ss['timestamp'] // 60)
                s = int(ss['timestamp'] % 60)
                cap = f"📸 {idx+1}/5 \\- {m:02d}:{s:02d}"
                if idx < len(screenshot_urls):
                    cap += f"\n🔗 {safe_markdown_text(screenshot_urls[idx])}"
                media_files.append({'path': ss['path'], 'caption': cap})
            
            await send_media_group(chat_id, media_files)
        
        # Send URL summary
        if screenshot_urls:
            urls_text = "\\*\\*All Screenshot Links\\*\\*\n\n"
            for i, url in enumerate(screenshot_urls, 1):
                urls_text += f"{i}\\. {safe_markdown_text(url)}\n"
            
            await send_message(chat_id, urls_text)
        
        await delete_message(chat_id, message_id)
        logger.info(f"Video processing complete: {original_file_name}")
        
    except Exception as e:
        logger.error(f"Video processing error: {e}")
        try:
            await edit_message(chat_id, message_id, 
                f"❌ Critical Processing Error\n\n"
                f"Error: {safe_markdown_text(str(e)[:200])}")
        except:
            pass
    
    finally:
        # Cleanup
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.error(f"Cleanup error for {temp_dir}: {e}")

async def process_video_download_and_optimize(chat_id, file_id, file_name, file_size, message_id, optimization_strategy):
    """Handles download, optimization, and passes to final processing."""
    temp_dir = None
    original_video_path = None
    optimized_video_path = None
    
    try:
        escaped_file_name = safe_markdown_text(file_name)
        
        # 1. Setup paths and directories
        temp_dir = tempfile.mkdtemp()
        original_video_path = os.path.join(temp_dir, f"original_{file_id}.mp4")
        optimized_video_path = os.path.join(temp_dir, f"optimized_{file_id}.mp4")
        
        # 2. Update status and Download
        await edit_message(chat_id, message_id, 
            f"⬇️ \\*\\*Downloading Large File\\*\\*\n\n{create_progress_bar(0)}\n"
            f"📁 {escaped_file_name}\n"
            f"💾 {file_size/(1024*1024):.1f}MB")
        
        download_success = await download_large_file(file_id, original_video_path, chat_id, message_id)
        
        if not download_success or not os.path.exists(original_video_path):
            await edit_message(chat_id, message_id, "❌ Download failed or file is unavailable\\.")
            return
        
        downloaded_size = os.path.getsize(original_video_path)
        logger.info(f"Successfully downloaded: {downloaded_size/(1024*1024):.1f}MB")
        
        # 3. Optimization (only if file is large enough to benefit)
        final_video_path = original_video_path
        
        if downloaded_size > 5 * 1024 * 1024:  # Only optimize files > 5MB
            await edit_message(chat_id, message_id, 
                f"⚙️ \\*\\*Optimizing Video\\*\\*\n\n{create_progress_bar(30)}\n"
                f"Strategy: \\`{optimization_strategy}\\`\n"
                f"📁 {escaped_file_name}")

            optimization_success = await optimize_video(original_video_path, optimized_video_path, optimization_strategy)
            
            if optimization_success:
                final_video_path = optimized_video_path
                optimized_size = os.path.getsize(optimized_video_path)
                logger.info(f"Optimization successful: {optimized_size/(1024*1024):.1f}MB (reduced from {downloaded_size/(1024*1024):.1f}MB)")
            else:
                logger.warning("Optimization failed, using original file")
        else:
            logger.info("File is small, skipping optimization")
        
        # 4. Proceed to final steps (extraction, upload, sending)
        await process_video_final_steps(
            chat_id, 
            final_video_path, 
            file_name, 
            file_size, 
            message_id
        )

    except Exception as e:
        logger.error(f"Download/Optimization error: {e}")
        try:
            await edit_message(chat_id, message_id, 
                f"❌ Critical Error in Download or Optimization\\.\n\n"
                f"Error: {safe_markdown_text(str(e)[:200])}")
        except:
            pass
    
    finally:
        # Cleanup will happen in process_video_final_steps finally block
        # If an error occurs here, we still need to clean up
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.error(f"Cleanup error for {temp_dir}: {e}")

# --- Webhook Handlers ---

async def handle_callback_query(update):
    """Handle inline button presses using file_id to look up metadata"""
    callback_query = update['callback_query']
    data = callback_query['data']
    chat_id = callback_query['message']['chat']['id']
    message_id = callback_query['message']['message_id']
    
    if data.startswith('confirm_'):
        # Initial confirmation to proceed with the large file
        file_id = data.split('_')[1]
        
        # 1. Look up the file metadata from the pending collection
        file_metadata = await pending_files_collection.find_one({"file_id": file_id})
        
        if not file_metadata:
            await edit_message(chat_id, message_id, "❌ Error: File link expired or processing already started\\.")
            return

        file_name = file_metadata['file_name']
        file_size = file_metadata['file_size']
        escaped_file_name = safe_markdown_text(file_name)
        size_info = f"💾 {file_size/(1024*1024):.1f}MB"
        
        # 2. Present optimization options
        
        # Store file_id in the next callback data prefix
        fast_data = f'optimize_fast_{file_id}'
        quality_data = f'optimize_quality_{file_id}'
        
        reply_markup = {
            "inline_keyboard": [
                [{
                    "text": "🚀 Fast Optimization (Quickest)",
                    "callback_data": fast_data
                }],
                [{
                    "text": "📉 Aggressive Optimization (Smallest Size)",
                    "callback_data": quality_data
                }]
            ]
        }
        
        await edit_message(chat_id, message_id,
            f"⚙️ \\*\\*Optimization Strategy\\*\\*\n\n"
            f"You chose to process \\*\\*{escaped_file_name}\\*\\* {size_info}\\.\n"
            f"Please choose an optimization method:",
            reply_markup=reply_markup
        )
        
    elif data.startswith('optimize_'):
        # Optimization choice made
        _, strategy, file_id = data.split('_')
        
        file_metadata = await pending_files_collection.find_one_and_delete({"file_id": file_id})
        
        if not file_metadata:
            await edit_message(chat_id, message_id, "❌ Error: File link expired or processing already started\\.")
            return

        file_name = file_metadata['file_name']
        file_size = file_metadata['file_size']

        await edit_message(chat_id, message_id, 
            f"✅ Confirmed! Starting download and processing with \\`{strategy}\\` optimization for:\n"
            f"📁 \\*\\*{safe_markdown_text(file_name)}\\*\\*")
        
        # Start the heavy lifting task with chosen strategy
        asyncio.create_task(
            process_video_download_and_optimize(
                chat_id, file_id, file_name, file_size, message_id, strategy
            )
        )

async def handle_webhook(request):
    """Handle incoming webhook"""
    try:
        data = await request.json()
        
        if 'message' in data:
            message = data['message']
            chat_id = message['chat']['id']
            
            if 'text' in message:
                text = message['text']
                
                if text == '/start':
                    await send_message(chat_id, 
                        "\\*\\*Welcome to Advanced Screenshot Bot\\!\\*\\*\n\n"
                        "📹 Send any video \\(up to 2GB\\) and I'll extract 5 screenshots and upload them to Catbox\\.moe\\.\n"
                        "💡 Files over 20MB will require confirmation before processing\\.")
                elif text == '/help':
                    await send_message(chat_id, 
                        "🤖 \\*\\*How to use:\\*\\* Send me a video file\\. "
                        "If it's over 20MB, I'll ask you to confirm and choose an optimization method before starting the download\\. "
                        "All processing happens asynchronously in the background\\.")
                elif text == '/stats':
                    total_count = await screenshots_collection.count_documents({})
                    user_count = await screenshots_collection.count_documents({"chat_id": chat_id})
                    large_files = await screenshots_collection.count_documents({"chat_id": chat_id, "large_file": True})
                    await send_message(chat_id, 
                        f"📊 \\*\\*Your Stats\\*\\*\n\n"
                        f"✅ Your Videos: {user_count}\n"
                        f"📸 Your Screenshots: {user_count * 5}\n"
                        f"📦 Large Files \\(over 20MB\\): {large_files}\n"
                        f"🌐 Total Processed: {total_count}")
            
            elif 'video' in message or 'document' in message:
                file_obj = message.get('video') or message.get('document')
                
                # Check for valid video file type
                if 'document' in message:
                    mime = file_obj.get('mime_type', '')
                    fname = file_obj.get('file_name', '')
                    video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm', '.m4v', '.3gp')
                    if not (mime.startswith('video/') or any(fname.lower().endswith(ext) for ext in video_exts)):
                        await send_message(chat_id, "❌ Please send a video file\\!")
                        return web.Response(text="OK")
                
                file_id = file_obj['file_id']
                file_name = file_obj.get('file_name', 'video.mp4')
                file_size = file_obj.get('file_size', 0)
                
                escaped_file_name = safe_markdown_text(file_name)
                size_info = f"💾 {file_size/(1024*1024):.1f}MB"
                
                if file_size > LARGE_FILE_THRESHOLD:
                    # --- LARGE FILE: PROMPT FOR CONFIRMATION ---
                    
                    # 1. Store metadata in MongoDB
                    await pending_files_collection.update_one(
                        {"file_id": file_id},
                        {"$set": {
                            "chat_id": chat_id,
                            "file_name": file_name,
                            "file_size": file_size,
                            "timestamp": datetime.utcnow()
                        }},
                        upsert=True
                    )
                    
                    # 2. Create callback data
                    callback_data = f"confirm_{file_id}"
                    
                    reply_markup = {
                        "inline_keyboard": [[
                            {
                                "text": f"✅ Start Processing ({size_info})",
                                "callback_data": callback_data
                            }
                        ]]
                    }
                    
                    await send_message(chat_id, 
                        f"⚠️ \\*\\*Confirmation Required: Large File Detected\\!\\*\\*\n\n"
                        f"📁 \\*\\*{escaped_file_name}\\*\\*\n"
                        f"{size_info}\n\n"
                        f"Please confirm to proceed with the download:",
                        reply_markup=reply_markup)
                    
                else:
                    # --- SMALL FILE: PROCESS IMMEDIATELY ---
                    result = await send_message(chat_id, 
                        f"⚡ \\*\\*Processing Started\\!\\*\\*\n\n{create_progress_bar(0)}\n"
                        f"📁 {escaped_file_name}\n"
                        f"{size_info}\n\n"
                        f"⏳ Please wait...")
                    
                    if result and 'result' in result:
                        message_id = result['result']['message_id']
                        # Small files use 'fast' optimization by default
                        asyncio.create_task(
                            process_video_download_and_optimize(
                                chat_id, file_id, file_name, file_size, message_id, 'fast'
                            )
                        )
        
        elif 'callback_query' in data:
            await handle_callback_query(data)

        return web.Response(text="OK")
        
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
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
