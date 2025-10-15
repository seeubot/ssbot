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
pending_files_collection = db.pending_files

# Global session
session = None

# --- Utility Functions ---

def escape_markdown(text):
    """Escape all markdown characters properly"""
    if not isinstance(text, str):
        return ""
    
    # Escape all special characters for MarkdownV2
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    for char in escape_chars:
        text = text.replace(char, f'\\{char}')
    
    return text

def safe_filename(text):
    """Extract just the filename without path for display"""
    if not isinstance(text, str):
        return "video.mp4"
    
    # Get just the filename
    filename = os.path.basename(text)
    
    # Remove problematic characters but keep it readable
    safe_chars = " .-_()[]"
    cleaned = ''.join(c for c in filename if c.isalnum() or c in safe_chars)
    
    return cleaned if cleaned else "video.mp4"

def create_progress_bar(percentage, length=10):
    """Create visual progress bar"""
    filled = int(length * percentage / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percentage:.0f}%"

async def send_message(chat_id, text, reply_markup=None, parse_mode="MarkdownV2"):
    """Send message via Telegram API"""
    url = f"{TELEGRAM_API}/sendMessage"
    
    # Always escape text for markdown
    if parse_mode == "MarkdownV2":
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
                # Retry without markdown if it fails
                if parse_mode:
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
                                if int(progress) % 10 == 0:  # Update every 10%
                                    await edit_message(chat_id, message_id, 
                                        f"⬇️ Downloading\n\n{create_progress_bar(progress)}\n"
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
        data = FormData()
        data.add_field('reqtype', 'fileupload')
        
        with open(file_path, 'rb') as f:
            data.add_field('fileToUpload', f, filename=os.path.basename(file_path))
            
            async with session.post(CATBOX_UPLOAD_URL, data=data, timeout=ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    url = await resp.text()
                    return url.strip()
    except Exception as e:
        logger.error(f"Catbox upload error: {e}")
    return None

async def optimize_video(input_path, output_path, strategy):
    """Optimize video using FFmpeg"""
    logger.info(f"Starting optimization with strategy: {strategy}")
    
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
    
    base_cmd = ['ffmpeg', '-i', input_path, '-y']
    
    if strategy == 'fast':
        cmd = base_cmd + [
            '-c:v', 'libx264', '-crf', '28', '-preset', 'fast',
            '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart',
            output_path
        ]
    elif strategy == 'quality':
        cmd = base_cmd + [
            '-vf', 'scale=1280:-2',  # Scale to 720p width
            '-c:v', 'libx264', '-crf', '30', '-preset', 'medium',
            '-c:a', 'aac', '-b:a', '96k',
            '-movflags', '+faststart',
            output_path
        ]
    else:
        logger.error(f"Unknown strategy: {strategy}")
        return False
        
    try:
        logger.info(f"Running FFmpeg command: {' '.join(cmd)}")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0 and os.path.exists(output_path):
            original_size = os.path.getsize(input_path)
            optimized_size = os.path.getsize(output_path)
            reduction = ((original_size - optimized_size) / original_size) * 100
            logger.info(f"Optimization successful: {optimized_size/(1024*1024):.1f}MB (reduced by {reduction:.1f}%)")
            return True
        else:
            logger.error(f"FFmpeg failed with code {process.returncode}")
            logger.error(f"FFmpeg stderr: {stderr.decode()}")
            return False
            
    except Exception as e:
        logger.error(f"FFmpeg execution error: {e}")
        return False

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
            # Estimate duration using OpenCV
            cap.set(cv2.CAP_PROP_POS_AVI_RATIO, 1)
            duration = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            cap.set(cv2.CAP_PROP_POS_AVI_RATIO, 0)
            fps = 25
            total_frames = int(duration * fps)
        else:
            duration = total_frames / fps
        
        logger.info(f"Video info - Frames: {total_frames}, FPS: {fps:.2f}, Duration: {duration:.2f}s")
        
        if total_frames > 0:
            # Take screenshots at equal intervals throughout the video
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
        
        # Add border and title
        final_width = width + 20
        final_height = height + 40
        final_thumbnail = Image.new('RGB', (final_width, final_height), (30, 30, 30))
        final_thumbnail.paste(thumbnail, (10, 30))
        
        thumbnail.save(output_path, 'JPEG', quality=90)
        logger.info(f"Created comprehensive thumbnail with {len(frames)} frames")
        return output_path
        
    except Exception as e:
        logger.error(f"Comprehensive thumbnail error: {e}")
        return None

def create_thumbnail(screenshot_paths, output_path):
    """Create thumbnail grid from screenshots"""
    try:
        images = []
        for path in screenshot_paths[:9]:  # Use up to 9 images
            if os.path.exists(path):
                img = Image.open(path)
                img.thumbnail((200, 150), Image.Resampling.LANCZOS)
                images.append(img)
        
        if not images:
            return None
        
        cols = 3
        rows = math.ceil(len(images) / cols)
        width = 200 * cols
        height = 150 * rows
        
        thumbnail = Image.new('RGB', (width, height), (0, 0, 0))
        
        for idx, img in enumerate(images):
            x = (idx % cols) * 200
            y = (idx // cols) * 150
            thumbnail.paste(img, (x, y))
        
        thumbnail.save(output_path, 'JPEG', quality=85)
        return output_path
    except Exception as e:
        logger.error(f"Thumbnail error: {e}")
        return None

async def process_video_final_steps(chat_id, video_path, original_file_name, original_file_size, message_id):
    """Final processing steps after download/optimization"""
    temp_dir = os.path.dirname(video_path)
    
    try:
        safe_file_name = safe_filename(original_file_name)
        current_size = os.path.getsize(video_path)
        
        # Extract screenshots
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
        
        # Create comprehensive thumbnail from video
        await edit_message(chat_id, message_id, 
            f"🖼️ Creating Thumbnail\n\n{create_progress_bar(70)}\n"
            f"📁 {safe_file_name}")
        
        comprehensive_thumbnail_path = os.path.join(temp_dir, "comprehensive_thumbnail.jpg")
        comprehensive_thumbnail = await asyncio.to_thread(
            create_comprehensive_thumbnail, video_path, comprehensive_thumbnail_path, 9
        )
        
        # Also create regular thumbnail from screenshots
        regular_thumbnail_path = os.path.join(temp_dir, "thumbnail.jpg")
        regular_thumbnail = await asyncio.to_thread(
            create_thumbnail, [s['path'] for s in screenshots], regular_thumbnail_path
        )
        
        # Use comprehensive thumbnail if available, otherwise use regular
        final_thumbnail_path = comprehensive_thumbnail_path if comprehensive_thumbnail else regular_thumbnail_path
        
        # Upload to Catbox
        await edit_message(chat_id, message_id, 
            f"📤 Uploading to Catbox\n\n{create_progress_bar(80)}\n"
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
            size_reduction = ""
            if current_size < original_file_size:
                reduction = ((original_file_size - current_size) / original_file_size) * 100
                size_reduction = f"📉 Reduced by {reduction:.1f}%\n"
            
            caption = (
                f"🎬 {safe_file_name}\n"
                f"⏱ {int(duration//60)}:{int(duration%60):02d}\n"
                f"📦 Original: {original_file_size/(1024*1024):.1f}MB | Final: {current_size/(1024*1024):.1f}MB\n"
                f"{size_reduction}"
                f"🔗 {len(screenshot_urls)} screenshots uploaded"
            )
            if thumbnail_url:
                caption += f"\n📷 Thumbnail: {thumbnail_url}"
            
            await send_photo(chat_id, final_thumbnail_path, caption)
        
        # Send screenshots
        if screenshots:
            media_files = []
            for idx, ss in enumerate(screenshots):
                m = int(ss['timestamp'] // 60)
                s = int(ss['timestamp'] % 60)
                cap = f"📸 {idx+1}/{len(screenshots)} - {m:02d}:{s:02d}"
                if idx < len(screenshot_urls):
                    cap += f"\n🔗 {screenshot_urls[idx]}"
                media_files.append({'path': ss['path'], 'caption': cap})
            
            await send_media_group(chat_id, media_files)
        
        # Send URL summary
        if screenshot_urls:
            urls_text = "All Screenshot Links\n\n"
            for i, url in enumerate(screenshot_urls, 1):
                urls_text += f"{i}. {url}\n"
            
            await send_message(chat_id, urls_text, parse_mode=None)
        
        await delete_message(chat_id, message_id)
        logger.info(f"Processing complete: {original_file_name}")
        
    except Exception as e:
        logger.error(f"Final processing error: {e}")
        await edit_message(chat_id, message_id, 
            f"❌ Processing Error\n\nError: {str(e)[:200]}")
    
    finally:
        # Cleanup
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

async def process_video_download_and_optimize(chat_id, file_id, file_name, file_size, message_id, optimization_strategy):
    """Main processing function"""
    temp_dir = tempfile.mkdtemp()
    original_path = os.path.join(temp_dir, f"original_{file_id}.mp4")
    optimized_path = os.path.join(temp_dir, f"optimized_{file_id}.mp4")
    
    try:
        safe_file_name = safe_filename(file_name)
        
        # Download file
        await edit_message(chat_id, message_id, 
            f"⬇️ Downloading File\n\n{create_progress_bar(0)}\n"
            f"📁 {safe_file_name}\n"
            f"💾 {file_size/(1024*1024):.1f}MB")
        
        if not await download_large_file(file_id, original_path, chat_id, message_id):
            await edit_message(chat_id, message_id, "❌ Download failed")
            return
        
        downloaded_size = os.path.getsize(original_path)
        logger.info(f"Downloaded: {downloaded_size/(1024*1024):.1f}MB")
        
        # Optimize if strategy is provided and file is large enough
        final_path = original_path
        
        if optimization_strategy and downloaded_size > 5 * 1024 * 1024:  # Only optimize >5MB files
            await edit_message(chat_id, message_id, 
                f"⚙️ Optimizing Video\n\n{create_progress_bar(40)}\n"
                f"Strategy: {optimization_strategy}\n"
                f"📁 {safe_file_name}")
            
            if await optimize_video(original_path, optimized_path, optimization_strategy):
                final_path = optimized_path
                optimized_size = os.path.getsize(optimized_path)
                reduction = ((downloaded_size - optimized_size) / downloaded_size) * 100
                logger.info(f"Optimized: {optimized_size/(1024*1024):.1f}MB (reduced by {reduction:.1f}%)")
            else:
                logger.warning("Optimization failed, using original file")
        
        # Process final steps
        await process_video_final_steps(chat_id, final_path, file_name, file_size, message_id)
        
    except Exception as e:
        logger.error(f"Processing error: {e}")
        await edit_message(chat_id, message_id, 
            f"❌ Processing Error\n\nError: {str(e)[:200]}")
    
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
    
    logger.info(f"Callback received: {data}")
    
    if data.startswith('confirm_'):
        file_id = data.split('_')[1]
        
        # Find file metadata
        file_metadata = await pending_files_collection.find_one({"file_id": file_id})
        if not file_metadata:
            await edit_message(chat_id, message_id, "❌ File data expired")
            return

        file_name = file_metadata['file_name']
        file_size = file_metadata['file_size']
        safe_name = safe_filename(file_name)
        
        # Show optimization options
        fast_data = f'optimize_fast_{file_id}'
        quality_data = f'optimize_quality_{file_id}'
        
        reply_markup = {
            "inline_keyboard": [
                [{"text": "🚀 Fast (Quick)", "callback_data": fast_data}],
                [{"text": "📉 Quality (Smaller)", "callback_data": quality_data}]
            ]
        }
        
        await edit_message(chat_id, message_id,
            f"⚙️ Choose Optimization\n\n"
            f"File: {safe_name}\n"
            f"Size: {file_size/(1024*1024):.1f}MB\n\n"
            f"Select optimization method:",
            reply_markup=reply_markup)
        
    elif data.startswith('optimize_'):
        parts = data.split('_')
        if len(parts) >= 3:
            strategy = parts[1]
            file_id = parts[2]
            
            # Get file metadata and delete from pending
            file_metadata = await pending_files_collection.find_one_and_delete({"file_id": file_id})
            if not file_metadata:
                await edit_message(chat_id, message_id, "❌ File data expired")
                return

            file_name = file_metadata['file_name']
            file_size = file_metadata['file_size']
            safe_name = safe_filename(file_name)
            
            await edit_message(chat_id, message_id, 
                f"✅ Starting with {strategy} optimization\n"
                f"📁 {safe_name}")
            
            # Start processing
            asyncio.create_task(
                process_video_download_and_optimize(
                    chat_id, file_id, file_name, file_size, message_id, strategy
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
                        "Welcome to Screenshot Bot!\n\n"
                        "Send me any video file and I'll extract screenshots for you.\n"
                        "Files over 20MB will require confirmation.")
                elif text == '/help':
                    await send_message(chat_id, 
                        "How to use:\n"
                        "1. Send a video file\n"
                        "2. For large files (>20MB), confirm processing\n"
                        "3. Wait for screenshots\n\n"
                        "The bot will upload screenshots to Catbox.moe")
                elif text == '/stats':
                    total = await screenshots_collection.count_documents({})
                    user_total = await screenshots_collection.count_documents({"chat_id": chat_id})
                    await send_message(chat_id, 
                        f"Your Stats:\n"
                        f"Videos processed: {user_total}\n"
                        f"Total screenshots: {user_total * 5}\n"
                        f"Global total: {total}")
            
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
                        await send_message(chat_id, "❌ Please send a video file")
                        return web.Response(text="OK")
                
                safe_name = safe_filename(file_name)
                
                if file_size > LARGE_FILE_THRESHOLD:
                    # Store file info for callback
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
                    
                    # Send confirmation button
                    callback_data = f"confirm_{file_id}"
                    reply_markup = {
                        "inline_keyboard": [[
                            {
                                "text": f"✅ Process ({file_size/(1024*1024):.1f}MB)",
                                "callback_data": callback_data
                            }
                        ]]
                    }
                    
                    await send_message(chat_id,
                        f"⚠️ Large File Detected\n\n"
                        f"File: {safe_name}\n"
                        f"Size: {file_size/(1024*1024):.1f}MB\n\n"
                        f"Please confirm to start processing:",
                        reply_markup=reply_markup)
                    
                else:
                    # Small file - process immediately
                    result = await send_message(chat_id,
                        f"⚡ Processing Started\n\n{create_progress_bar(0)}\n"
                        f"📁 {safe_name}\n"
                        f"Size: {file_size/(1024*1024):.1f}MB")
                    
                    if result and 'result' in result:
                        message_id = result['result']['message_id']
                        asyncio.create_task(
                            process_video_download_and_optimize(
                                chat_id, file_id, file_name, file_size, message_id, 'fast'
                            )
                        )
        
        elif 'callback_query' in data:
            await handle_callback_query(data)

        return web.Response(text="OK")
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(text="OK")

async def health_check(request):
    return web.Response(text="OK")

async def setup_webhook():
    """Setup webhook on startup"""
    if not WEBHOOK_URL:
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
    global session
    session = ClientSession()
    await setup_webhook()
    logger.info("Bot started")

async def cleanup(app):
    if session:
        await session.close()

app = web.Application()
app.router.add_post('/webhook', handle_webhook)
app.router.add_get('/health', health_check)
app.router.add_get('/', health_check)
app.on_startup.append(start_server)
app.on_cleanup.append(cleanup)

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=PORT)
