from flask import Flask, render_template, request, send_file, Response, jsonify
import sys
import os

# Ensure UTF-8 output on Windows consoles to prevent UnicodeEncodeError with emojis
if sys.platform == 'win32':
    try:
        if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import shutil
import uuid
import queue
import threading
import time
import mimetypes
from urllib.parse import urlparse
from downloader import WebsiteDownloader, zip_directory, get_site_name

# Ensure .es files (ECMAScript modules) are served as JavaScript
mimetypes.add_type('application/javascript', '.es')

app = Flask(__name__)

# Base config
DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

def cleanup_downloads_folder():
    """Remove all files and folders from downloads directory on startup"""
    try:
        for item in os.listdir(DOWNLOAD_FOLDER):
            item_path = os.path.join(DOWNLOAD_FOLDER, item)
            if os.path.isfile(item_path):
                os.remove(item_path)
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
        print("🧹 Pasta downloads limpa com sucesso")
    except Exception as e:
        print(f"⚠️ Erro ao limpar pasta downloads: {e}")

# Cleanup downloads folder on startup
cleanup_downloads_folder()

# Thread-safe stores for sessions
state_lock = threading.Lock()
message_queues = {}
download_results = {}

def is_safe_url(target_url: str) -> tuple[bool, str]:
    """Validate that the URL is a safe HTTP/HTTPS web address."""
    if not target_url or not isinstance(target_url, str):
        return False, "URL inválida ou vazia"
    
    target_url = target_url.strip()
    try:
        parsed = urlparse(target_url)
    except Exception:
        return False, "Formato de URL inválido"
    
    if parsed.scheme not in ('http', 'https'):
        return False, "Apenas protocolos HTTP e HTTPS são permitidos"
    
    hostname = parsed.hostname
    if not hostname:
        return False, "Hostname ausente na URL"
    
    # Block loopback / local hosts
    if hostname.lower() in ('localhost', '127.0.0.1', '::1', '0.0.0.0'):
        return False, "URLs locais ou de loopback não são permitidas"
    
    return True, ""

def cleanup_abandoned_sessions():
    """Clean up sessions and zip files safely without Windows lock contention"""
    while True:
        time.sleep(120)  # Check every 2 minutes
        current_time = time.time()
        
        sessions_to_remove = []
        with state_lock:
            for session_id, result in list(download_results.items()):
                created_at = result.get('created_at', 0)
                downloaded_at = result.get('downloaded_at')
                # If downloaded, keep for 5 minutes grace period; if not, 25 minutes
                ttl = 300 if downloaded_at else 1500
                ref_time = downloaded_at if downloaded_at else created_at
                
                if ref_time and (current_time - ref_time > ttl):
                    zip_path = result.get('zip_path')
                    if zip_path and os.path.exists(zip_path):
                        try:
                            os.remove(zip_path)
                            print(f"🗑️ Removido arquivo expirado: {os.path.basename(zip_path)}")
                        except Exception as e:
                            print(f"⚠️ Erro ao remover zip expirado: {e}")
                    sessions_to_remove.append(session_id)
            
            for session_id in sessions_to_remove:
                message_queues.pop(session_id, None)
                download_results.pop(session_id, None)

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_abandoned_sessions, daemon=True)
cleanup_thread.start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start-download', methods=['POST'])
def start_download():
    """Start download process and return session ID for SSE"""
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    
    is_valid, err_msg = is_safe_url(url)
    if not is_valid:
        return jsonify({'error': err_msg}), 400
    
    # Create session
    session_id = str(uuid.uuid4())
    with state_lock:
        message_queues[session_id] = queue.Queue()
        download_results[session_id] = {
            'status': 'processing',
            'zip_path': None,
            'filename': None,
            'created_at': time.time()
        }
    
    # Start download in background thread
    thread = threading.Thread(target=process_download, args=(session_id, url))
    thread.daemon = True
    thread.start()
    
    return jsonify({'session_id': session_id})

def process_download(session_id, url):
    """Background download process"""
    with state_lock:
        q = message_queues.get(session_id)
    
    if not q:
        return
        
    download_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    zip_path = os.path.join(DOWNLOAD_FOLDER, f"{session_id}.zip")
    
    def log_callback(message):
        q.put(message)
    
    try:
        # Initialize downloader with log callback
        downloader = WebsiteDownloader(url, download_dir, log_callback=log_callback)
        
        # Process the site
        success = downloader.process()
        
        if not success:
            q.put("❌ Falha no download")
            with state_lock:
                download_results[session_id] = {'status': 'error', 'error': 'Failed to download site'}
            return
        
        # Generate filename from site name
        site_name = get_site_name(url)
        zip_filename = f"{site_name}.zip"
        
        q.put("📦 Criando arquivo ZIP...")
        zip_directory(download_dir, zip_path)
        
        # Cleanup raw directory
        if os.path.exists(download_dir):
            shutil.rmtree(download_dir, ignore_errors=True)
        
        q.put("🎉 Download pronto!")
        with state_lock:
            download_results[session_id] = {
                'status': 'complete',
                'zip_path': zip_path,
                'filename': zip_filename,
                'created_at': time.time()
            }
        
    except Exception as e:
        q.put(f"❌ Erro: {str(e)}")
        with state_lock:
            download_results[session_id] = {'status': 'error', 'error': str(e)}
        
        # Clean up any leftover files
        try:
            if os.path.exists(download_dir):
                shutil.rmtree(download_dir, ignore_errors=True)
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception:
            pass

@app.route('/stream/<session_id>')
def stream(session_id):
    """SSE endpoint for log streaming with full queue draining"""
    def generate():
        with state_lock:
            q = message_queues.get(session_id)
        
        if not q:
            yield "data: ❌ Sessão não encontrada\n\n"
            return
        
        while True:
            try:
                # Wait for next message (short timeout to react to completion)
                message = q.get(timeout=1.0)
                yield f"data: {message}\n\n"
            except queue.Empty:
                with state_lock:
                    result = download_results.get(session_id, {})
                    status = result.get('status')
                
                # Only signal completion if queue is completely drained
                if status in ['complete', 'error'] and q.empty():
                    yield f"event: done\ndata: {status}\n\n"
                    break
                
                # Send keepalive comment to keep connection alive
                yield ": keepalive\n\n"
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

@app.route('/download-file/<session_id>')
def download_file(session_id):
    """Download the generated ZIP file safely without premature Windows deletion"""
    with state_lock:
        result = download_results.get(session_id)
    
    if not result or result.get('status') != 'complete':
        return "File not ready", 404
    
    zip_path = result.get('zip_path')
    filename = result.get('filename')
    
    if not zip_path or not os.path.exists(zip_path):
        return "File not found", 404
    
    with state_lock:
        result['downloaded_at'] = time.time()
    
    try:
        return send_file(zip_path, as_attachment=True, download_name=filename)
    except Exception as e:
        print(f"❌ Erro ao enviar arquivo: {e}")
        return "Error sending file", 500

if __name__ == '__main__':
    # Development server
    app.run(debug=True, port=5001, threaded=True)
