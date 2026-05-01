# ================================
# monkey patch MUST stay at top
# ================================
from gevent import monkey
monkey.patch_all()

import requests
import time
import queue
import sys
import gevent
from gevent.lock import Semaphore

from flask import Flask, Response, jsonify, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
CORS(app, resources={r"/*": {"origins": "*"}})

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["120 per minute"],
    storage_uri="memory://"
)

STREAM_URL = "https://railway.gov.gr/api/train-stream"
BASE_API = "https://railway.gov.gr/api"

MAX_CLIENTS = 4000
CLIENT_QUEUE_SIZE = 50
STREAM_TIMEOUT = 90
HEARTBEAT_INTERVAL = 15
BOARD_CACHE_TTL = 30

active_clients = set()
clients_lock = Semaphore()
GEO_ALMANAC = {}
BOARD_CACHE = {}

stats = {
    "messages_last_sec": 0,
    "current_mps": 0,
    "connected": False
}
last_known_payload = b""

def build_almanac():
    sys.stdout.write("\n📚 Building Station Almanac in RAM...\n")
    sys.stdout.flush()
    try:
        r = requests.get(f"{BASE_API}/msd/geo?mode=all&types=station,track,tunnel&limit=10000&station_railway_type=station&station_status=active", timeout=20)
        r.raise_for_status()
        GEO_ALMANAC['data'] = r.json()
        sys.stdout.write("✅ Almanac built successfully!\n\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(f"❌ Failed to build Almanac: {e}\n\n")
        sys.stdout.flush()

build_almanac()

def background_stream():
    global last_known_payload
    while True:
        try:
            stats["connected"] = False
            sys.stdout.write("\n🔄 Connecting to Hellenic Railways stream...\n")
            sys.stdout.flush()

            with requests.get(STREAM_URL, stream=True, timeout=STREAM_TIMEOUT) as r:
                r.raise_for_status()
                stats["connected"] = True
                sys.stdout.write("✅ Connected to Stream!\n")
                sys.stdout.flush()

                for chunk in r.iter_content(chunk_size=2048):
                    if not chunk: continue

                    if b"trainPositionsUx" in chunk:
                        last_known_payload = chunk

                    # FIX: Accurately count total train references per second in the raw byte stream
                    stats["messages_last_sec"] += chunk.count(b"trainNumber")

                    with clients_lock:
                        dead_clients = []
                        for q in active_clients:
                            try:
                                q.put_nowait(chunk)
                            except queue.Full:
                                dead_clients.append(q)
                        for q in dead_clients:
                            active_clients.discard(q)

        except requests.exceptions.ReadTimeout:
            sys.stdout.write("\n⚠ Stream quiet (Read Timeout). Reconnecting...\n")
            sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(f"\n⚠ Stream error: {e}. Retrying in 5s...\n")
            sys.stdout.flush()
            time.sleep(5)

def stats_monitor():
    while True:
        gevent.sleep(1)
        stats["current_mps"] = stats["messages_last_sec"]
        stats["messages_last_sec"] = 0

        status_icon = "🟢" if stats["connected"] else "🔴"
        sys.stdout.write(f"\r{status_icon} Stream Status | 👥 Users: {len(active_clients)} | ⚡ Raw Train MPS: {stats['current_mps']}   ")
        sys.stdout.flush()

        stats_event = f"event: server_stats\ndata: {stats['current_mps']}\n\n".encode('utf-8')
        with clients_lock:
            for q in active_clients:
                try: q.put_nowait(stats_event)
                except queue.Full: pass

gevent.spawn(background_stream)
gevent.spawn(stats_monitor)

def is_valid_id(val):
    return all(c.isalnum() or c == '-' for c in val) and len(val) < 50

def get_cached_board(station_id, board_type):
    cache_key = f"{station_id}_{board_type}"
    now = time.time()
    if cache_key in BOARD_CACHE:
        expiry, data = BOARD_CACHE[cache_key]
        if now < expiry: return jsonify(data)
    try:
        r = requests.get(f"{BASE_API}/public/stations/{station_id}/{board_type}?limit=100", timeout=10)
        r.raise_for_status()
        data = r.json()
        BOARD_CACHE[cache_key] = (now + BOARD_CACHE_TTL, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502

@app.route("/train-stream")
@limiter.exempt 
def train_stream():
    with clients_lock:
        if len(active_clients) >= MAX_CLIENTS: abort(503, "Too many connections")
    def generate():
        q = queue.Queue(maxsize=CLIENT_QUEUE_SIZE)
        with clients_lock: active_clients.add(q)
        if last_known_payload: yield last_known_payload
        try:
            while True:
                try: yield q.get(timeout=HEARTBEAT_INTERVAL)
                except queue.Empty: yield b":\n\n"
        finally:
            with clients_lock: active_clients.discard(q)
    return Response(generate(), content_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

@app.route("/geo")
@limiter.limit("60 per minute")
def geo():
    if 'data' in GEO_ALMANAC: return jsonify(GEO_ALMANAC['data'])
    try:
        r = requests.get(f"{BASE_API}/msd/geo?mode=all&types=station,track,tunnel&limit=10000&station_railway_type=station&station_status=active", timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 502

@app.route("/arrivals/<station_id>")
def arrivals(station_id):
    if not is_valid_id(station_id): abort(400, "Invalid station_id")
    return get_cached_board(station_id, "arrivals")

@app.route("/departures/<station_id>")
def departures(station_id):
    if not is_valid_id(station_id): abort(400, "Invalid station_id")
    return get_cached_board(station_id, "departures")

@app.route("/api/public/trains/<train_id>/stream")
def dashcam_stream_info(train_id):
    if not is_valid_id(train_id): abort(400, "Invalid train_id")
    try:
        r = requests.get(f"{BASE_API}/public/trains/{train_id}/stream", timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
# ================================
# monkey patch MUST stay at top
# ================================
from gevent import monkey
monkey.patch_all()

import requests
import time
import queue
import os
import string
import sys
import gevent
from gevent.lock import Semaphore

from flask import Flask, Response, jsonify, abort, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask import Flask, request

app = Flask(__name__)

# Tell Flask to trust the headers sent by Caddy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# ================================
# CONFIG
# ================================
STREAM_URL = "https://railway.gov.gr/api/train-stream"
BASE_API = "https://railway.gov.gr/api"

MAX_CLIENTS = 4000
CLIENT_QUEUE_SIZE = 50
STREAM_TIMEOUT = 90
HEARTBEAT_INTERVAL = 15
BOARD_CACHE_TTL = 30

# ================================
# APP SETUP
# ================================
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["120 per minute"],
    storage_uri="memory://"
)

# ================================
# GLOBAL STATE & CACHES
# ================================
active_clients = set()
clients_lock = Semaphore()

GEO_ALMANAC = {}
BOARD_CACHE = {}

stats = {
    "messages_last_sec": 0,
    "current_mps": 0,
    "connected": False
}

# The Snapshot: Memorizes the last valid payload so new users see trains instantly
last_known_payload = b""

def build_almanac():
    sys.stdout.write("\n📚 Building Station Almanac in RAM...\n")
    sys.stdout.flush()
    url = f"{BASE_API}/msd/geo?mode=all&types=station,track,tunnel&limit=10000&station_railway_type=station&station_status=active"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        GEO_ALMANAC['data'] = r.json()
        sys.stdout.write("✅ Almanac built successfully! Map will load instantly.\n\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(f"❌ Failed to build Almanac on startup: {e}\n\n")
        sys.stdout.flush()

build_almanac()

# ================================
# BACKGROUND PROCESSES
# ================================
def background_stream():
    global last_known_payload
    while True:
        try:
            stats["connected"] = False
            sys.stdout.write("\n🔄 Connecting to Hellenic Railways stream...\n")
            sys.stdout.flush()

            with requests.get(STREAM_URL, stream=True, timeout=STREAM_TIMEOUT) as r:
                r.raise_for_status()
                stats["connected"] = True
                sys.stdout.write("✅ Connected to Stream!\n")
                sys.stdout.flush()

                # Increased chunk size slightly to capture full payloads more reliably
                for chunk in r.iter_content(chunk_size=2048):
                    if not chunk:
                        continue

                    # The Snapshot Fix
                    if b"trainPositionsUx" in chunk:
                        last_known_payload = chunk

                    stats["messages_last_sec"] += 1

                    with clients_lock:
                        dead_clients = []
                        for q in active_clients:
                            try:
                                q.put_nowait(chunk)
                            except queue.Full:
                                dead_clients.append(q)

                        for q in dead_clients:
                            active_clients.discard(q)

        except requests.exceptions.ReadTimeout:
            sys.stdout.write("\n⚠ Stream quiet (Read Timeout). Reconnecting...\n")
            sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(f"\n⚠ Stream error: {e}. Retrying in 5s...\n")
            sys.stdout.flush()
            time.sleep(5)

def stats_monitor():
    while True:
        gevent.sleep(1)
        stats["current_mps"] = stats["messages_last_sec"]
        stats["messages_last_sec"] = 0

        status_icon = "🟢" if stats["connected"] else "🔴"
        sys.stdout.write(f"\r{status_icon} Stream Status | 👥 Users: {len(active_clients)} | ⚡ MPS: {stats['current_mps']}   ")
        sys.stdout.flush()

        stats_event = f"event: server_stats\ndata: {stats['current_mps']}\n\n".encode('utf-8')
        with clients_lock:
            for q in active_clients:
                try:
                    q.put_nowait(stats_event)
                except queue.Full:
                    pass

gevent.spawn(background_stream)
gevent.spawn(stats_monitor)

# ================================
# ROUTES
# ================================
@app.route("/train-stream")
def train_stream():
    with clients_lock:
        if len(active_clients) >= MAX_CLIENTS:
            abort(503, "Too many connections")

    def generate():
        q = queue.Queue(maxsize=CLIENT_QUEUE_SIZE)
        with clients_lock:
            active_clients.add(q)

        # Blast the new user with the last known data instantly
        if last_known_payload:
            yield last_known_payload

        try:
            while True:
                try:
                    chunk = q.get(timeout=HEARTBEAT_INTERVAL)
                    yield chunk
                except queue.Empty:
                    yield b":\n\n"
        finally:
            with clients_lock:
                active_clients.discard(q)

    return Response(
        generate(),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        },
    )

def is_valid_id(val):
    # Safely allows both MongoDB Hex IDs and standard Numeric OSM IDs
    return val.isalnum() and len(val) < 50

def get_cached_board(station_id, board_type):
    cache_key = f"{station_id}_{board_type}"
    now = time.time()

    if cache_key in BOARD_CACHE:
        expiry, data = BOARD_CACHE[cache_key]
        if now < expiry:
            return jsonify(data)

    url = f"{BASE_API}/public/stations/{station_id}/{board_type}?limit=100"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        BOARD_CACHE[cache_key] = (now + BOARD_CACHE_TTL, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502

@app.route("/geo")
def geo():
    if 'data' in GEO_ALMANAC:
        return jsonify(GEO_ALMANAC['data'])
    url = f"{BASE_API}/msd/geo?mode=all&types=station,track,tunnel&limit=10000&station_railway_type=station&station_status=active"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502

@app.route("/arrivals/<station_id>")
def arrivals(station_id):
    if not is_valid_id(station_id):
        abort(400, "Invalid station_id")
    return get_cached_board(station_id, "arrivals")

@app.route("/departures/<station_id>")
def departures(station_id):
    if not is_valid_id(station_id):
        abort(400, "Invalid station_id")
    return get_cached_board(station_id, "departures")

@app.route("/api/public/trains/<train_id>/stream")
def dashcam_stream_info(train_id):
    if not is_valid_id(train_id):
        abort(400, "Invalid train_id")

    url = f"{BASE_API}/public/trains/{train_id}/stream"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502

if __name__ == "__main__":
    print("⚠ Use Gunicorn in production")
    app.run(host="0.0.0.0", port=5000)
