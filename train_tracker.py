import json
import time
import requests
import threading
import os
import glob
from flask import Flask, jsonify
from flask_cors import CORS

PROXY_URL = "http://127.0.0.1:5000/train-stream"
STATE_FILE = "latest_state.json"

live_db = {}
daily_archive = {}
current_archive_date = ""

def load_live_db():
    global live_db
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                live_db = json.load(f)
            print(f"📁 Loaded {len(live_db)} known trains from live state.")
        except Exception as e:
            print(f"⚠ Could not load state DB: {e}")

def save_live_db():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(live_db, f)
    except Exception: pass  

def get_archive_filename(date_str):
    return f"archive_{date_str}.json"

def load_daily_archive(date_str):
    filename = get_archive_filename(date_str)
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except Exception: pass
    return {}

def save_daily_archive():
    if current_archive_date and daily_archive:
        try:
            with open(get_archive_filename(current_archive_date), "w") as f:
                json.dump(daily_archive, f)
        except Exception: pass

def watch_stream():
    global live_db, daily_archive, current_archive_date
    print("📡 Tracker thread connecting to stream...")
    
    last_save_time = time.time()
    
    while True:
        try:
            response = requests.get(PROXY_URL, stream=True, timeout=90)
            for line in response.iter_lines():
                if line:
                    decoded_line = line.decode('utf-8')
                    if decoded_line.startswith("data: "):
                        try:
                            payload = decoded_line[6:]
                            data = json.loads(payload)
                            
                            if isinstance(data, dict) and "positions" in data:
                                current_time = time.time()
                                
                                today_str = time.strftime("%Y-%m-%d", time.localtime(current_time))
                                if today_str != current_archive_date:
                                    if current_archive_date:  
                                        save_daily_archive()
                                    current_archive_date = today_str
                                    daily_archive = load_daily_archive(today_str)

                                for train in data["positions"]:
                                    # ENFORCE LOCOMOTIVE ID AS PRIMARY KEY
                                    loco_id = train.get("locomotiveId")
                                    if not loco_id:
                                        loco_id = train.get("trainId") or train.get("_id") or train.get("id")
                                        
                                    speed = float(train.get("speed", 0) or 0)
                                    heading = train.get("course", 0)
                                    lat = train.get("lat")
                                    lng = train.get("lng")

                                    # EXTRACT CLEAR IDENTITIES
                                    loco_num = train.get("locomotiveNumber") or "Unknown Loco"
                                    t_name = train.get("name") or train.get("trainName") or ""
                                    t_num = train.get("trainNumber") or train.get("scheduleNumber") or ""
                                    
                                    if t_name and t_num and t_name != t_num:
                                        route_name = f"{t_name} ({t_num})"
                                    else:
                                        route_name = t_name or t_num or "No Route Assigned"

                                    if loco_id not in live_db:
                                        live_db[loco_id] = {
                                            "loco_num": loco_num,
                                            "route_name": route_name,
                                            "max_speed": speed,
                                            "current_speed": speed,
                                            "last_seen": current_time,
                                            "packet_history": [],
                                            "last_lat": lat,
                                            "last_lng": lng,
                                            "raw_data": train,
                                            "last_path_time": 0
                                        }

                                    info = live_db[loco_id]
                                    info["loco_num"] = loco_num
                                    info["route_name"] = route_name
                                    info["max_speed"] = max(speed, info.get("max_speed", 0))
                                    info["current_speed"] = speed
                                    info["last_seen"] = current_time
                                    info["raw_data"] = train  
                                    
                                    info.setdefault("packet_history", []).append(current_time)
                                    info["packet_history"] = [t for t in info["packet_history"] if current_time - t <= 60]

                                    last_path_time = info.get("last_path_time", 0)
                                    if current_time - last_path_time >= 5:
                                        if lat != info.get("last_lat") or lng != info.get("last_lng") or (current_time - last_path_time >= 300):
                                            
                                            if loco_id not in daily_archive:
                                                daily_archive[loco_id] = {
                                                    "loco_num": loco_num,
                                                    "route_name": route_name,
                                                    "raw_data": train,
                                                    "path_history": []
                                                }
                                                
                                            daily_archive[loco_id]["path_history"].append({
                                                "t": current_time,
                                                "lat": lat,
                                                "lng": lng,
                                                "s": speed,
                                                "h": heading
                                            })
                                            
                                            info["last_path_time"] = current_time
                                            info["last_lat"] = lat
                                            info["last_lng"] = lng

                                if current_time - last_save_time > 10:
                                    save_live_db()
                                    save_daily_archive()
                                    last_save_time = current_time

                        except json.JSONDecodeError: pass
        except Exception as e:
            print(f"⚠ Tracker lost connection: {e}. Retrying in 5s...")
            time.sleep(5)

load_live_db()
threading.Thread(target=watch_stream, daemon=True).start()

app = Flask(__name__)
CORS(app)

@app.route("/api/tracker")
def get_tracker_data():
    current_time = time.time()
    results = []
    for tid, info in live_db.items():
        recent_packets = [t for t in info.get("packet_history", []) if current_time - t <= 60]
        results.append({
            "id": tid, 
            "loco_num": info.get("loco_num", "Unknown Loco"),
            "route_name": info.get("route_name", "No Route"),
            "current_speed": info.get("current_speed", 0),
            "max_speed": info.get("max_speed", 0),
            "last_seen_sec": current_time - info.get("last_seen", current_time),
            "updates_per_min": len(recent_packets),
            "lat": info.get("last_lat"),
            "lng": info.get("last_lng"),
            "raw_data": info.get("raw_data", {})
        })
    return jsonify(results)

@app.route("/api/archives/list")
def list_archives():
    files = glob.glob("archive_*.json")
    dates = [f.replace("archive_", "").replace(".json", "") for f in files]
    today = time.strftime("%Y-%m-%d")
    if today not in dates: dates.append(today)
    return jsonify(sorted(dates, reverse=True))

@app.route("/api/archives/<date_str>")
def get_archive(date_str):
    if date_str == time.strftime("%Y-%m-%d"):
        return jsonify(daily_archive)
    return jsonify(load_daily_archive(date_str))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6000)
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

                buffer = []
                for line in r.iter_lines():
                    buffer.append(line)
                    
                    # An empty line means the SSE event is fully complete
                    if not line: 
                        # Rebuild the perfect SSE block with newlines
                        event_chunk = b"\n".join(buffer) + b"\n"
                        buffer = [] # Reset buffer for next event

                        if b"trainPositionsUx" in event_chunk:
                            last_known_payload = event_chunk

                        stats["messages_last_sec"] += event_chunk.count(b"trainNumber")

                        with clients_lock:
                            dead_clients = []
                            for q in active_clients:
                                try:
                                    q.put_nowait(event_chunk)
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
trains:~# cat train_tracker.py 
import json
import time
import requests
import threading
import os
import glob
from flask import Flask, jsonify
from flask_cors import CORS

PROXY_URL = "http://127.0.0.1:5000/train-stream"
STATE_FILE = "latest_state.json"

# In-memory stores
live_db = {}
daily_archive = {}
current_archive_date = ""

def load_live_db():
    global live_db
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                live_db = json.load(f)
            print(f"📁 Loaded {len(live_db)} known trains from live state.")
        except Exception as e:
            print(f"⚠ Could not load state DB: {e}")

def save_live_db():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(live_db, f)
    except Exception: pass 

def get_archive_filename(date_str):
    return f"archive_{date_str}.json"

def load_daily_archive(date_str):
    filename = get_archive_filename(date_str)
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except Exception: pass
    return {}

def save_daily_archive():
    if current_archive_date and daily_archive:
        try:
            with open(get_archive_filename(current_archive_date), "w") as f:
                json.dump(daily_archive, f)
        except Exception: pass

def watch_stream():
    global live_db, daily_archive, current_archive_date
    print("📡 Tracker thread connecting to stream...")
    
    last_save_time = time.time()
    
    while True:
        try:
            response = requests.get(PROXY_URL, stream=True, timeout=90)
            for line in response.iter_lines():
                if line:
                    decoded_line = line.decode('utf-8')
                    if decoded_line.startswith("data: "):
                        try:
                            payload = decoded_line[6:]
                            data = json.loads(payload)
                            
                            if isinstance(data, dict) and "positions" in data:
                                current_time = time.time()
                                
                                # --- DAY ROLLOVER CHECK ---
                                today_str = time.strftime("%Y-%m-%d", time.localtime(current_time))
                                if today_str != current_archive_date:
                                    if current_archive_date: 
                                        save_daily_archive() # Save yesterday
                                    current_archive_date = today_str
                                    daily_archive = load_daily_archive(today_str) # Load today

                                for train in data["positions"]:
                                    train_id = train.get("_id") or train.get("id")
                                    speed = float(train.get("speed", 0) or 0)
                                    heading = train.get("course", 0)
                                    train_num = train.get("trainNumber", "Unknown")
                                    lat = train.get("lat")
                                    lng = train.get("lng")

                                    # 1. Update Live State
                                    if train_id not in live_db:
                                        live_db[train_id] = {
                                            "number": train_num,
                                            "max_speed": speed,
                                            "current_speed": speed,
                                            "last_seen": current_time,
                                            "packet_history": [],
                                            "last_lat": lat,
                                            "last_lng": lng,
                                            "raw_data": train,
                                            "last_path_time": 0
                                        }

                                    info = live_db[train_id]
                                    info["number"] = train_num
                                    info["max_speed"] = max(speed, info.get("max_speed", 0))
                                    info["current_speed"] = speed
                                    info["last_seen"] = current_time
                                    info["raw_data"] = train 
                                    
                                    info.setdefault("packet_history", []).append(current_time)
                                    info["packet_history"] = [t for t in info["packet_history"] if current_time - t <= 60]

                                    # 2. Update Daily Archive (Every 5 seconds)
                                    last_path_time = info.get("last_path_time", 0)
                                    if current_time - last_path_time >= 5:
                                        if lat != info.get("last_lat") or lng != info.get("last_lng") or (current_time - last_path_time >= 300):
                                            
                                            # Ensure train exists in today's archive
                                            if train_id not in daily_archive:
                                                daily_archive[train_id] = {
                                                    "number": train_num,
                                                    "raw_data": train,
                                                    "path_history": []
                                                }
                                                
                                            daily_archive[train_id]["path_history"].append({
                                                "t": current_time,
                                                "lat": lat,
                                                "lng": lng,
                                                "s": speed,
                                                "h": heading
                                            })
                                            
                                            info["last_path_time"] = current_time
                                            info["last_lat"] = lat
                                            info["last_lng"] = lng

                                if current_time - last_save_time > 10:
                                    save_live_db()
                                    save_daily_archive()
                                    last_save_time = current_time

                        except json.JSONDecodeError: pass
        except Exception as e:
            print(f"⚠ Tracker lost connection: {e}. Retrying in 5s...")
            time.sleep(5)

load_live_db()
threading.Thread(target=watch_stream, daemon=True).start()

app = Flask(__name__)
CORS(app)

# Lightweight Live Poller (No huge history arrays)
@app.route("/api/tracker")
def get_tracker_data():
    current_time = time.time()
    results = []
    for tid, info in live_db.items():
        recent_packets = [t for t in info.get("packet_history", []) if current_time - t <= 60]
        results.append({
            "id": tid,
            "number": info.get("number", "Unknown"),
            "current_speed": info.get("current_speed", 0),
            "max_speed": info.get("max_speed", 0),
            "last_seen_sec": current_time - info.get("last_seen", current_time),
            "updates_per_min": len(recent_packets),
            "lat": info.get("last_lat"),
            "lng": info.get("last_lng"),
            "raw_data": info.get("raw_data", {})
        })
    return jsonify(results)

# Returns a list of all available days (e.g., ["2026-05-01", "2026-05-02"])
@app.route("/api/archives/list")
def list_archives():
    files = glob.glob("archive_*.json")
    dates = [f.replace("archive_", "").replace(".json", "") for f in files]
    # Ensure current day is in the list even if file hasn't been created yet
    today = time.strftime("%Y-%m-%d")
    if today not in dates: dates.append(today)
    return jsonify(sorted(dates, reverse=True))

# Downloads a specific day's dataset
@app.route("/api/archives/<date_str>")
def get_archive(date_str):
    if date_str == time.strftime("%Y-%m-%d"):
        return jsonify(daily_archive) # Send RAM copy if it's today
    return jsonify(load_daily_archive(date_str))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6000)
trains:~# 
import json
import time
import requests
import threading
import os
from flask import Flask, jsonify
from flask_cors import CORS

PROXY_URL = "http://127.0.0.1:5000/train-stream"
DB_FILE = "trains_history.json"
trains_db = {}

def load_db():
    global trains_db
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                trains_db = json.load(f)
            print(f"📁 Loaded {len(trains_db)} trains from history file.")
        except Exception as e:
            print(f"⚠ Could not load history DB: {e}")

def save_db():
    try:
        with open(DB_FILE, "w") as f:
            json.dump(trains_db, f)
    except Exception as e:
        pass # Silently fail to avoid file lock crashes

def watch_stream():
    global trains_db
    print("📡 Tracker thread connecting to stream...")
    last_save_time = time.time()
    
    while True:
        try:
            response = requests.get(PROXY_URL, stream=True, timeout=90)
            for line in response.iter_lines():
                if line:
                    decoded_line = line.decode('utf-8')
                    if decoded_line.startswith("data: "):
                        try:
                            payload = decoded_line[6:]
                            data = json.loads(payload)
                            
                            if isinstance(data, dict) and "positions" in data:
                                current_time = time.time()
                                
                                for train in data["positions"]:
                                    train_id = train.get("_id") or train.get("id")
                                    speed = float(train.get("speed", 0) or 0)
                                    train_num = train.get("trainNumber", "Unknown")
                                    lat = train.get("lat")
                                    lng = train.get("lng")

                                    if train_id not in trains_db:
                                        trains_db[train_id] = {
                                            "number": train_num,
                                            "max_speed": speed,
                                            "current_speed": speed,
                                            "last_seen": current_time,
                                            "packet_history": [current_time],
                                            "last_lat": lat,
                                            "last_lng": lng,
                                            "raw_data": train # SAVE THE FULL PAYLOAD FOR THE MAP
                                        }
                                    else:
                                        info = trains_db[train_id]
                                        info["number"] = train_num
                                        info["max_speed"] = max(speed, info.get("max_speed", 0))
                                        info["current_speed"] = speed
                                        info["last_seen"] = current_time
                                        info["last_lat"] = lat
                                        info["last_lng"] = lng
                                        info["raw_data"] = train 
                                        
                                        info.setdefault("packet_history", []).append(current_time)
                                        info["packet_history"] = [t for t in info["packet_history"] if current_time - t <= 60]

                                # Auto-save to file every 10 seconds
                                if current_time - last_save_time > 10:
                                    save_db()
                                    last_save_time = current_time

                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            print(f"⚠ Tracker lost connection: {e}. Retrying in 5s...")
            time.sleep(5)

load_db()
threading.Thread(target=watch_stream, daemon=True).start()

app = Flask(__name__)
CORS(app)

@app.route("/api/tracker")
def get_tracker_data():
    current_time = time.time()
    results = []
    
    for tid, info in trains_db.items():
        recent_packets = [t for t in info.get("packet_history", []) if current_time - t <= 60]
        info["packet_history"] = recent_packets 
        
        results.append({
            "id": tid,
            "number": info.get("number", "Unknown"),
            "current_speed": info.get("current_speed", 0),
            "max_speed": info.get("max_speed", 0),
            "last_seen_sec": current_time - info.get("last_seen", current_time),
            "updates_per_min": len(recent_packets),
            "lat": info.get("last_lat"),
            "lng": info.get("last_lng"),
            "raw_data": info.get("raw_data", {}) # SEND FULL PAYLOAD TO FRONTEND
        })
        
    return jsonify(results)

if __name__ == "__main__":
    print("🚀 Master Tracker API running on port 6000")
    app.run(host="0.0.0.0", port=6000)
