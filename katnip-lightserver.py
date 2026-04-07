import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template_string, request
import tinytuya

# -----------------------------
# Config
# -----------------------------
DEVICES_FILE = Path.home() / "devices.json"
DEFAULT_VERSION = 3.3
POLL_INTERVAL_SECONDS = 5
HOST = "0.0.0.0"
PORT = 8080

app = Flask(__name__)

# Shared state protected by a lock because the background poller and web routes both touch it.
state_lock = threading.Lock()
devices: List[Dict[str, Any]] = []
clients: Dict[str, tinytuya.BulbDevice] = {}
last_status: Dict[str, Dict[str, Any]] = {}
last_error: Dict[str, str] = {}


# -----------------------------
# Helpers
# -----------------------------
def load_devices() -> List[Dict[str, Any]]:
    if not DEVICES_FILE.exists():
        raise FileNotFoundError(f"devices.json not found at {DEVICES_FILE}")

    with DEVICES_FILE.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    loaded = []
    for d in raw:
        loaded.append(
            {
                "name": d.get("name", "Unnamed Device"),
                "id": d["id"],
                "ip": d.get("ip", ""),
                "key": d["key"],
                "version": float(d.get("version", DEFAULT_VERSION)),
                "product_name": d.get("product_name", ""),
                "mapping": d.get("mapping", {}),
            }
        )
    return loaded


def make_client(device: Dict[str, Any]) -> tinytuya.BulbDevice:
    client = tinytuya.BulbDevice(
        dev_id=device["id"],
        address=device["ip"],
        local_key=device["key"],
        version=device.get("version", DEFAULT_VERSION),
    )
    client.set_socketPersistent(True)
    client.set_socketNODELAY(True)
    client.set_socketRetryLimit(2)
    client.set_socketTimeout(2)
    return client


def init_clients() -> None:
    global devices, clients
    with state_lock:
        devices = load_devices()
        clients = {d["id"]: make_client(d) for d in devices if d.get("ip")}



def find_device(device_id: str) -> Optional[Dict[str, Any]]:
    for d in devices:
        if d["id"] == device_id:
            return d
    return None


def get_client(device_id: str) -> tinytuya.BulbDevice:
    client = clients.get(device_id)
    if client is None:
        d = find_device(device_id)
        if d is None:
            raise KeyError(f"Unknown device id: {device_id}")
        if not d.get("ip"):
            raise ValueError(f"Device {d['name']} has no IP in devices.json")
        client = make_client(d)
        clients[device_id] = client
    return client


def parse_hsv_string(hsv_hex: str) -> Dict[str, int]:
    """
    Tuya old-style colour_data often looks like: HHHHSSSSVVVV in hex-ish packed format.
    For your bulbs, TinyTuya usually handles sending strings back, but parsing status helps the UI.
    Example seen: 3d1b00001aff3a
    We only do a best-effort parse here.
    """
    try:
        if len(hsv_hex) >= 12:
            h = int(hsv_hex[0:4], 16)
            s = int(hsv_hex[4:8], 16)
            v = int(hsv_hex[8:12], 16)
            return {"h": h, "s": s, "v": v}
    except Exception:
        pass
    return {"h": 0, "s": 0, "v": 0}



def normalize_status(device: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    dps = raw.get("dps", raw)
    on = bool(dps.get("1", False))
    mode = dps.get("2", "white")
    brightness = int(dps.get("3", 0))
    colour_raw = dps.get("5", "")
    hsv = parse_hsv_string(colour_raw) if isinstance(colour_raw, str) else {"h": 0, "s": 0, "v": 0}

    return {
        "id": device["id"],
        "name": device["name"],
        "ip": device.get("ip", ""),
        "product_name": device.get("product_name", ""),
        "is_on": on,
        "mode": mode,
        "brightness": brightness,
        "colour_raw": colour_raw,
        "hsv": hsv,
        "raw": raw,
        "updated_at": time.time(),
    }



def refresh_device_status(device: Dict[str, Any]) -> None:
    device_id = device["id"]
    try:
        client = get_client(device_id)
        raw = client.status() or {}
        normalized = normalize_status(device, raw)
        with state_lock:
            last_status[device_id] = normalized
            last_error.pop(device_id, None)
    except Exception as e:
        with state_lock:
            last_error[device_id] = str(e)



def refresh_all_status() -> None:
    for device in devices:
        refresh_device_status(device)



def poller() -> None:
    while True:
        try:
            refresh_all_status()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_SECONDS)



def set_white(device_id: str, brightness: int) -> Dict[str, Any]:
    brightness = max(25, min(255, int(brightness)))
    client = get_client(device_id)
    client.set_mode("white")
    client.set_brightness(brightness)
    time.sleep(0.15)
    refresh_device_status(find_device(device_id))
    return last_status.get(device_id, {})



def set_colour(device_id: str, h: int, s: int, v: int) -> Dict[str, Any]:
    h = max(1, min(360, int(h)))
    s = max(1, min(255, int(s)))
    v = max(1, min(255, int(v)))
    client = get_client(device_id)
    client.set_mode("colour")
    # TinyTuya bulb helper accepts HSV values.
    client.set_hsv(h, s, v)
    time.sleep(0.15)
    refresh_device_status(find_device(device_id))
    return last_status.get(device_id, {})


# -----------------------------
# Web UI
# -----------------------------
INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Katnip Lights</title>
  <style>
    :root {
      --bg: #0b1020;
      --panel: #121933;
      --panel-2: #1a2345;
      --text: #edf2ff;
      --muted: #a8b3d1;
      --accent: #8ab4ff;
      --accent-2: #c79dff;
      --good: #67d48e;
      --bad: #ff8e8e;
      --border: rgba(255,255,255,.08);
      --shadow: 0 10px 30px rgba(0,0,0,.25);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, system-ui, Arial, sans-serif;
      background: linear-gradient(180deg, #0a0f1d 0%, #111735 100%);
      color: var(--text);
    }
    .wrap {
      max-width: 1200px;
      margin: 0 auto;
      padding: 24px;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 24px;
    }
    h1 {
      font-size: 32px;
      margin: 0;
    }
    .sub {
      color: var(--muted);
      margin-top: 6px;
    }
    .btn {
      border: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--text);
      padding: 10px 14px;
      border-radius: 14px;
      cursor: pointer;
      font-weight: 600;
    }
    .btn:hover { filter: brightness(1.08); }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 18px;
    }
    .card {
      background: rgba(18,25,51,.92);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }
    .card-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 14px;
    }
    .name {
      font-size: 20px;
      font-weight: 700;
    }
    .meta {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
    }
    .pill {
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid var(--border);
    }
    .on { background: rgba(103,212,142,.15); color: var(--good); }
    .off { background: rgba(255,255,255,.06); color: var(--muted); }
    .err { background: rgba(255,142,142,.12); color: var(--bad); }
    .row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 12px 0;
    }
    .action {
      flex: 1 1 0;
      min-width: 90px;
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 12px;
      background: var(--panel-2);
      color: var(--text);
      cursor: pointer;
      font-weight: 700;
    }
    .action.primary {
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #09111f;
      border: none;
    }
    .control-label {
      font-size: 13px;
      color: var(--muted);
      margin: 10px 0 6px;
    }
    input[type=range] {
      width: 100%;
    }
    input[type=color] {
      width: 100%;
      height: 48px;
      border: none;
      background: transparent;
      padding: 0;
    }
    .footer-note {
      color: var(--muted);
      font-size: 13px;
      margin-top: 20px;
    }
    .tiny {
      font-size: 12px;
      color: var(--muted);
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div>
        <h1>Katnip Lights</h1>
        <div class="sub">Local-only bulb control from your Pi</div>
      </div>
      <button class="btn" onclick="refreshNow()">Refresh</button>
    </div>
    <div id="grid" class="grid"></div>
    <div class="footer-note">Brightness uses the bulb's white mode scale. Color uses a simple HSV conversion from the browser color picker.</div>
  </div>

<script>
function hsvToHex(h, s, v) {
  s /= 255;
  v /= 255;
  let c = v * s;
  let x = c * (1 - Math.abs((h / 60) % 2 - 1));
  let m = v - c;
  let r = 0, g = 0, b = 0;
  if (h < 60) [r,g,b] = [c,x,0];
  else if (h < 120) [r,g,b] = [x,c,0];
  else if (h < 180) [r,g,b] = [0,c,x];
  else if (h < 240) [r,g,b] = [0,x,c];
  else if (h < 300) [r,g,b] = [x,0,c];
  else [r,g,b] = [c,0,x];
  const toHex = n => Math.round((n + m) * 255).toString(16).padStart(2, '0');
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

function hexToHsv(hex) {
  const m = hex.replace('#', '');
  const r = parseInt(m.substring(0,2), 16) / 255;
  const g = parseInt(m.substring(2,4), 16) / 255;
  const b = parseInt(m.substring(4,6), 16) / 255;
  const max = Math.max(r,g,b), min = Math.min(r,g,b);
  const d = max - min;
  let h = 0;
  if (d !== 0) {
    if (max === r) h = 60 * (((g - b) / d) % 6);
    else if (max === g) h = 60 * (((b - r) / d) + 2);
    else h = 60 * (((r - g) / d) + 4);
  }
  if (h < 0) h += 360;
  const s = max === 0 ? 0 : d / max;
  const v = max;
  return {
    h: Math.max(1, Math.round(h)),
    s: Math.max(1, Math.round(s * 255)),
    v: Math.max(1, Math.round(v * 255)),
  };
}

async function api(path, method='POST', body=null) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : null
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(txt || `HTTP ${res.status}`);
  }
  return await res.json();
}

async function loadDevices() {
  const data = await fetch('/api/devices').then(r => r.json());
  const grid = document.getElementById('grid');
  grid.innerHTML = '';

  for (const d of data.devices) {
    const card = document.createElement('div');
    card.className = 'card';

    const statusClass = d.error ? 'err' : (d.is_on ? 'on' : 'off');
    const statusText = d.error ? 'Error' : (d.is_on ? 'On' : 'Off');
    const colorHex = hsvToHex(d.hsv?.h || 0, d.hsv?.s || 0, d.hsv?.v || 0);
    const brightness = d.brightness || 25;

    card.innerHTML = `
      <div class="card-top">
        <div>
          <div class="name">${d.name}</div>
          <div class="meta">${d.product_name || ''}<br>${d.ip || ''}</div>
        </div>
        <div class="pill ${statusClass}">${statusText}</div>
      </div>

      ${d.error ? `<div class="tiny" style="color:#ff8e8e; margin-bottom:10px;">${d.error}</div>` : ''}

      <div class="row">
        <button class="action primary" onclick="turnOn('${d.id}')">On</button>
        <button class="action" onclick="turnOff('${d.id}')">Off</button>
        <button class="action" onclick="refreshOne('${d.id}')">Refresh</button>
      </div>

      <div class="control-label">White brightness: <span id="bval-${d.id}">${brightness}</span></div>
      <input type="range" min="25" max="255" value="${brightness}" oninput="document.getElementById('bval-${d.id}').textContent=this.value" onchange="setBrightness('${d.id}', this.value)">

      <div class="control-label">Color</div>
      <input type="color" value="${colorHex}" onchange="setColor('${d.id}', this.value)">

      <div class="tiny" style="margin-top:10px;">Mode: ${d.mode || 'unknown'}</div>
    `;

    grid.appendChild(card);
  }
}

async function turnOn(id) {
  await api(`/api/device/${id}/on`);
  await loadDevices();
}
async function turnOff(id) {
  await api(`/api/device/${id}/off`);
  await loadDevices();
}
async function setBrightness(id, value) {
  await api(`/api/device/${id}/brightness`, 'POST', { brightness: parseInt(value, 10) });
  await loadDevices();
}
async function setColor(id, value) {
  const hsv = hexToHsv(value);
  await api(`/api/device/${id}/color`, 'POST', hsv);
  await loadDevices();
}
async function refreshOne(id) {
  await api(`/api/device/${id}/refresh`);
  await loadDevices();
}
async function refreshNow() {
  await api('/api/refresh');
  await loadDevices();
}

loadDevices();
setInterval(loadDevices, 8000);
</script>
</body>
</html>
"""


@app.route("/")
def index() -> str:
    return render_template_string(INDEX_HTML)


@app.route("/api/devices")
def api_devices():
    with state_lock:
        merged = []
        for d in devices:
            device_id = d["id"]
            status = dict(last_status.get(device_id, {}))
            if not status:
                status = {
                    "id": d["id"],
                    "name": d["name"],
                    "ip": d.get("ip", ""),
                    "product_name": d.get("product_name", ""),
                    "is_on": False,
                    "mode": "unknown",
                    "brightness": 25,
                    "hsv": {"h": 0, "s": 0, "v": 0},
                }
            if device_id in last_error:
                status["error"] = last_error[device_id]
            merged.append(status)
    return jsonify({"devices": merged})


@app.route("/api/refresh", methods=["POST"])
def api_refresh_all():
    refresh_all_status()
    return jsonify({"ok": True})


@app.route("/api/device/<device_id>/refresh", methods=["POST"])
def api_refresh_one(device_id: str):
    d = find_device(device_id)
    if d is None:
        return jsonify({"error": "Unknown device"}), 404
    refresh_device_status(d)
    return jsonify({"ok": True, "device": last_status.get(device_id, {})})


@app.route("/api/device/<device_id>/on", methods=["POST"])
def api_turn_on(device_id: str):
    try:
        client = get_client(device_id)
        client.turn_on()
        refresh_device_status(find_device(device_id))
        return jsonify({"ok": True, "device": last_status.get(device_id, {})})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/device/<device_id>/off", methods=["POST"])
def api_turn_off(device_id: str):
    try:
        client = get_client(device_id)
        client.turn_off()
        refresh_device_status(find_device(device_id))
        return jsonify({"ok": True, "device": last_status.get(device_id, {})})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/device/<device_id>/brightness", methods=["POST"])
def api_brightness(device_id: str):
    try:
        payload = request.get_json(force=True)
        brightness = int(payload.get("brightness", 128))
        result = set_white(device_id, brightness)
        return jsonify({"ok": True, "device": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/device/<device_id>/color", methods=["POST"])
def api_color(device_id: str):
    try:
        payload = request.get_json(force=True)
        h = int(payload.get("h", 1))
        s = int(payload.get("s", 255))
        v = int(payload.get("v", 255))
        result = set_colour(device_id, h, s, v)
        return jsonify({"ok": True, "device": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reload", methods=["POST"])
def api_reload():
    try:
        init_clients()
        refresh_all_status()
        return jsonify({"ok": True, "count": len(devices)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    init_clients()
    refresh_all_status()
    threading.Thread(target=poller, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False)
