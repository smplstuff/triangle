from flask import Flask, request, jsonify, render_template, send_file, redirect, session
import yt_dlp
import gallery_dl
import os
import uuid
import json
import shutil
from urllib.parse import quote
import logging
import re
import time  # added to support retry delays
import subprocess

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Add secret key for session management

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create downloads directory if it doesn't exist
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def get_client_session_dir():
    """Get or create session-specific directory and cleanup previous files"""
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
    
    session_dir = os.path.join(DOWNLOAD_DIR, session['session_id'])
    
    # Cleanup any existing files for this session
    if os.path.exists(session_dir):
        shutil.rmtree(session_dir, ignore_errors=True)
    
    os.makedirs(session_dir, exist_ok=True)
    return session_dir

@app.route('/')
def index():
    # Cleanup session files on page load
    if 'session_id' in session:
        session_dir = os.path.join(DOWNLOAD_DIR, session['session_id'])
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
        session.pop('session_id', None)  # Clear existing session on refresh
    
    return render_template('index.html')

@app.route('/api/info')
def get_video_info():
    url = request.args.get('url')
    download_type = request.args.get('type', 'video')  # 'video' or 'audio'
    
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    try:
        # Configure yt-dlp options
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }
        
        # Extract video information
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        formats = []
        if download_type == 'audio':
            # Get audio formats
            for fmt in info.get('formats', []):
                if fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none':
                    filesize = fmt.get('filesize')
                    if filesize:
                        filesize = f"{filesize / 1024 / 1024:.1f} MB"
                    else:
                        filesize = "Unknown size"
                    
                    formats.append({
                        'format_id': fmt['format_id'],
                        'extension': fmt.get('ext', 'unknown'),
                        'quality': f"{fmt.get('abr', 'unknown')}kbps",
                        'filesize': filesize
                    })
        else:
            # Get video formats with audio
            for fmt in info.get('formats', []):
                if fmt.get('acodec') != 'none' and fmt.get('vcodec') != 'none':
                    filesize = fmt.get('filesize')
                    if filesize:
                        filesize = f"{filesize / 1024 / 1024:.1f} MB"
                    else:
                        filesize = "Unknown size"
                    
                    height = fmt.get('height')
                    quality = f"{height}p" if height else "unknown"
                    
                    formats.append({
                        'format_id': fmt['format_id'],
                        'extension': fmt.get('ext', 'unknown'),
                        'quality': quality,
                        'filesize': filesize
                    })
        
        # Sort formats by quality (assuming higher is better)
        formats.sort(key=lambda x: int(x['quality'].replace('p', '').replace('kbps', '')) 
                    if x['quality'].replace('p', '').replace('kbps', '').isdigit() else 0, 
                    reverse=True)
        
        # Limit to top 5 formats to avoid overwhelming the UI
        formats = formats[:5]
        
        return jsonify({
            'title': info.get('title', 'Unknown title'),
            'channel': info.get('uploader', 'Unknown channel'),
            'thumbnail': info.get('thumbnail', ''),
            'formats': formats
        })
        
    except Exception as e:
        logger.error(f"Error extracting video info: {str(e)}")
        return jsonify({'error': f'Error extracting video info: {str(e)}'}), 500

def generate_filename(info, download_type, format_style='default', quality=None):
    """
    Generate a filename based on selected format style
    
    Supported formats:
    - default: Original yt-dlp naming (no modifications)
    - detailed: service-id-quality-type
    """
    if format_style == 'default':
        # Use yt-dlp's original filename without any processing
        return yt_dlp.utils.sanitize_filename(info.get('title', 'untitled')) + f".{info.get('ext', 'mp4')}"

    # Detailed format handling
    def sanitize_filename(filename):
        cleaned = re.sub(r'[<>:"/\\|?*]', ' ', filename)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        max_length = 200
        name, ext = os.path.splitext(cleaned)
        return f"{name[:max_length]}{ext}" if len(name) > max_length else f"{name}{ext}"

    service = 'unknown'
    if 'youtube.com' in info.get('webpage_url', ''):
        service = 'youtube'
    elif 'soundcloud.com' in info.get('webpage_url', ''):
        service = 'soundcloud'
    elif 'tiktok.com' in info.get('webpage_url', ''):
        service = 'tiktok'

    file_type = 'video' if download_type == 'video' else 'audio'
    if service == 'tiktok' and info.get('extractor') == 'tiktok:photo':
        file_type = 'photo'
 
    video_id = info.get('id', 'unknown')
    ext = 'mp3' if download_type == 'audio' else 'mp4'
    detailed_name = f"{service}-{video_id}-{quality or 'auto'}-{file_type}.{ext}"
    return sanitize_filename(detailed_name)

def sanitize_filename(filename):
    cleaned = re.sub(r'[<>:"/\\|?*]', ' ', filename)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    max_length = 200
    name, ext = os.path.splitext(cleaned)
    return f"{name[:max_length]}{ext}" if len(name) > max_length else f"{name}{ext}"

@app.route('/api/direct-download')
def direct_download():
    session_dir = get_client_session_dir()
    download_id = str(uuid.uuid4())
    download_path = os.path.join(session_dir, download_id)
    os.makedirs(download_path, exist_ok=True)

    url = request.args.get('url')
    download_type = request.args.get('type', 'video')  # Default to video
    quality = request.args.get('quality', 'highest')
    filename_format = request.args.get('filename_format', 'default')
    
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    try:
        MAX_YOUTUBE_SIZE_MB = 300  # 300MB limit for YouTube
        
        if 'youtube.com' in url or 'youtu.be' in url:
            # Get filesize estimate before downloading
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                filesize = max(info.get('filesize') or 0, info.get('filesize_approx') or 0)
                filesize_mb = filesize / 1024 / 1024
                
                if filesize_mb > MAX_YOUTUBE_SIZE_MB:
                    over_by = filesize_mb - MAX_YOUTUBE_SIZE_MB
                    return jsonify({
                        'error': f'File too large ({filesize_mb:.1f}MB). Exceeds limit by {over_by:.1f}MB. Try selecting a lower quality!'
                    }), 400

        # Handle TikTok photos with gallery-dl
        if 'tiktok.com' in url and '/photo/' in url:
            download_path = os.path.join(session_dir, download_id)
            os.makedirs(download_path, exist_ok=True)

            try:
                # Use subprocess to run gallery-dl directly
                result = subprocess.run(
                    ['gallery-dl', '-D', download_path, url],
                    capture_output=True,
                    text=True,
                    check=True
                )
                
                # Find downloaded file
                downloaded_files = []
                for root, _, files in os.walk(download_path):
                    for file in files:
                        if file.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                            downloaded_files.append(os.path.join(root, file))
                
                if not downloaded_files:
                    raise Exception('No image files downloaded from TikTok photo post')

                downloaded_file = downloaded_files[0]

                # Process filename based on format preference
                photo_id = url.split('/photo/')[-1].split('?')[0].split('/')[0]
                original_filename = os.path.basename(downloaded_file)
                
                if filename_format == 'default':
                    # Split out number prefixes and UUID suffixes for default format
                    parts = original_filename.split(maxsplit=1)
                    clean_parts = [p for p in parts if not p.startswith(f"{photo_id}_")]
                    clean_name = ' '.join(clean_parts).split('[', 1)[0].strip().rstrip('_')
                    ext = os.path.splitext(original_filename)[1] or '.jpg'
                    new_filename = f"{clean_name}{ext}"
                else:
                    # Detailed format: tiktok-{id}-{type}
                    ext = os.path.splitext(original_filename)[1] or '.jpg'
                    new_filename = f"tiktok-{photo_id}-photo{ext}"
             
                # Clean filename and ensure valid characters
                sanitized_name = sanitize_filename(new_filename).replace('__', '_')
                while '  ' in sanitized_name:
                    sanitized_name = sanitized_name.replace('  ', ' ')
                new_path = os.path.join(download_path, sanitized_name)
                os.rename(downloaded_file, new_path)
                downloaded_file = new_path

                filename = os.path.basename(downloaded_file)
                response = send_file(
                    downloaded_file,
                    as_attachment=True,
                    download_name=filename,
                    mimetype='image/jpeg'
                )
                
                @response.call_on_close
                def cleanup():
                    try:
                        shutil.rmtree(session_dir)
                    except Exception as e:
                        logger.error(f"Error cleaning session directory: {str(e)}")
                
                return response

            except subprocess.CalledProcessError as e:
                return jsonify({'error': f"gallery-dl failed: {e.stderr}"}), 500

        else:
            # Existing yt-dlp handling for videos
            if 'soundcloud.com' in url:
                download_type = 'audio'
            
            download_path = os.path.join(session_dir, download_id)
            os.makedirs(download_path, exist_ok=True)

            # First extract info to get metadata
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info = ydl.extract_info(url, download=False)
            
            # Generate filename before download; use yt-dlp's default naming
            custom_filename = generate_filename(info, download_type, filename_format, quality)
            
            # Configure download options with determined filename
            ydl_opts = {
                'outtmpl': os.path.join(download_path, custom_filename),
                'quiet': False,
                'no_warnings': True,
            }

            if download_type == 'audio':
                ydl_opts.update({
                    'format': 'bestaudio',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                })
            else:
                # Video quality selection
                if quality == '1080p':
                    ydl_opts['format'] = 'bestvideo[height<=1080]+bestaudio/best'
                elif quality == '720p':
                    ydl_opts['format'] = 'bestvideo[height<=720]+bestaudio/best'
                elif quality == '480p':
                    ydl_opts['format'] = 'bestvideo[height<=480]+bestaudio/best'
                elif quality == '360p':
                    ydl_opts['format'] = 'bestvideo[height<=360]+bestaudio/best'
                elif quality == '144p':
                    ydl_opts['format'] = 'bestvideo[height<=144]+bestaudio/best'
                else:
                    ydl_opts['format'] = 'best'
            
            # Special handling for TikTok to only get h264 formats
            if 'tiktok.com' in url:
                ydl_opts['format'] = 'bestvideo[ext=mp4][format_id^=h264]+bestaudio/best'
                ydl_opts['format_sort'] = ['vcodec:h264']
            
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        downloaded_file = ydl.prepare_filename(info)
                        if not os.path.exists(downloaded_file):
                            for file in os.listdir(download_path):
                                if file.startswith(os.path.splitext(os.path.basename(downloaded_file))[0]):
                                    downloaded_file = os.path.join(download_path, file)
                                    break
                    break
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise e
                    time.sleep(1)
        
            filename = os.path.basename(downloaded_file)
            response = send_file(
                downloaded_file,
                as_attachment=True,
                download_name=filename,  
                mimetype='application/octet-stream'
            )
            @response.call_on_close
            def cleanup():
                try:
                    shutil.rmtree(session_dir)
                except Exception as e:
                    logger.error(f"Error cleaning session directory: {str(e)}")
            return response
            
    except Exception as e:
        error_str = re.sub(r'\x1B\[[0-?]*[ -/]*[@-~]', '', str(e))
        logger.error(f"Error downloading track: {error_str}")
        
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
        
        return jsonify({'error': f'Error downloading track: {error_str}'}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
