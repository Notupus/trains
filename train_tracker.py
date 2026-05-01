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
