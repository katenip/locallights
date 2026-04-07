import json
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
GROUPS_FILE = Path.home() / "light_groups.json"
DEFAULT_VERSION = 3.3
POLL_INTERVAL_SECONDS = 5
HOST = "0.0.0.0"
PORT = 8080

FAKE_TEMP_PRESETS = {
    "warm": {"h": 35, "s": 170, "v": 255},
    "neutral": {"h": 42, "s": 70, "v": 255},
    "cool": {"h": 210, "s": 35, "v": 255},
}

SCENE_DP_OPTIONS = {
    "6": "scene_data",
    "7": "flash_scene_1",
    "8": "flash_scene_2",
    "9": "flash_scene_3",
    "10": "flash_scene_4",
}

DEFAULT_GROUP_RULES = {
    "Bathroom": ["bathroom"],
    "Kate Room": ["kateroom", "room"],
    "Ungrouped": [],
}

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


def load_group_rules() -> Dict[str, List[str]]:
    if not GROUPS_FILE.exists():
        save_group_rules(DEFAULT_GROUP_RULES)
        return dict(DEFAULT_GROUP_RULES)
    with GROUPS_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    cleaned: Dict[str, List[str]] = {}
    for group_name, patterns in data.items():
        cleaned[group_name] = [str(p).strip().lower() for p in patterns if str(p).strip()]
    if "Ungrouped" not in cleaned:
        cleaned["Ungrouped"] = []
    return cleaned


def save_group_rules(rules: Dict[str, List[str]]) -> None:
    with GROUPS_FILE.open("w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2)


def assign_group(device_name: str, rules: Dict[str, List[str]]) -> str:
    lowered = device_name.lower()
    for group_name, patterns in rules.items():
        if group_name == "Ungrouped":
            continue
        if any(pattern in lowered for pattern in patterns):
            return group_name
    return "Ungrouped"


def apply_groups(device_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rules = load_group_rules()
    output = []
    for d in device_list:
        enriched = dict(d)
        enriched["group"] = assign_group(d["name"], rules)
        output.append(enriched)
    return output


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
        devices = apply_groups(load_devices())
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
    mapping = device.get("mapping", {}) or {}
    supports_temp = "4" in mapping or any(v.get("code") == "temp_value" for v in mapping.values() if isinstance(v, dict))
    on = bool(dps.get("1", False))
    mode = dps.get("2", "white")
    brightness = int(dps.get("3", 25) or 25)
    colour_temp = int(dps.get("4", 128) or 128) if supports_temp else None
    colour_raw = dps.get("5", "")
    hsv = parse_hsv_string(colour_raw) if isinstance(colour_raw, str) else {"h": 0, "s": 0, "v": 0}

    scene_payloads = {}
    for dp in SCENE_DP_OPTIONS:
        if dp in dps:
            scene_payloads[dp] = dps.get(dp)

    return {
        "id": device["id"],
        "name": device["name"],
        "group": device.get("group", "Ungrouped"),
        "ip": device.get("ip", ""),
        "product_name": device.get("product_name", ""),
        "is_on": on,
        "mode": mode,
        "brightness": brightness,
        "temp": colour_temp,
        "supports_temp": supports_temp,
        "colour_raw": colour_raw,
        "hsv": hsv,
        "scene_payloads": scene_payloads,
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


def set_temp(device_id: str, temp: int) -> Dict[str, Any]:
    temp = max(0, min(255, int(temp)))
    device = find_device(device_id)
    if device is None:
        raise KeyError("Unknown device")
    mapping = device.get("mapping", {}) or {}
    supports_temp = "4" in mapping or any(v.get("code") == "temp_value" for v in mapping.values() if isinstance(v, dict))
    client = get_client(device_id)
    client.set_mode("white")
    if supports_temp:
        client.set_value("4", temp)
    else:
        h = int(35 + (temp / 255.0) * (210 - 35))
        s = int(170 - (temp / 255.0) * (170 - 35))
        client.set_mode("colour")
        client.set_hsv(max(1, min(360, h)), max(1, min(255, s)), 255)
    time.sleep(0.15)
    refresh_device_status(find_device(device_id))
    return last_status.get(device_id, {})


def set_colour(device_id: str, h: int, s: int, v: int) -> Dict[str, Any]:
    h = max(1, min(360, int(h)))
    s = max(1, min(255, int(s)))
    v = max(1, min(255, int(v)))
    client = get_client(device_id)
    client.set_mode("colour")
    client.set_hsv(h, s, v)
    time.sleep(0.15)
    refresh_device_status(find_device(device_id))
    return last_status.get(device_id, {})


def devices_for_group(group_name: str) -> List[Dict[str, Any]]:
    return [d for d in devices if d.get("group") == group_name]


def set_scene_payload(device_id: str, dp: str, payload: str) -> Dict[str, Any]:
    dp = str(dp).strip()
    if not dp.isdigit():
        raise ValueError(f"DP must be numeric, got: {dp}")
    payload = str(payload).strip()
    if not payload:
        raise ValueError("Scene payload cannot be empty")
    client = get_client(device_id)
    try:
        client.set_value("2", "scene")
    except Exception:
        pass
    client.set_value(dp, payload)
    time.sleep(0.2)
    refresh_device_status(find_device(device_id))
    return last_status.get(device_id, {})


def run_group_action(group_name: str, action: str, payload: Optional[Dict[str, Any]] = None) -> None:
    payload = payload or {}
    for d in devices_for_group(group_name):
        if action == "on":
            get_client(d["id"]).turn_on()
        elif action == "off":
            get_client(d["id"]).turn_off()
        elif action == "brightness":
            set_white(d["id"], int(payload.get("brightness", 128)))
        elif action == "temp":
            set_temp(d["id"], int(payload.get("temp", 128)))
        elif action == "color":
            set_colour(d["id"], int(payload.get("h", 1)), int(payload.get("s", 255)), int(payload.get("v", 255)))
        elif action == "scene_payload":
            set_scene_payload(d["id"], str(payload.get("dp", "6")).strip(), str(payload.get("payload", "")))
    refresh_all_status()


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
      --panel-3: #202b55;
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
    .wrap { max-width: 1400px; margin: 0 auto; padding: 24px; }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 18px;
      flex-wrap: wrap;
    }
    h1 { font-size: 32px; margin: 0; }
    .sub { color: var(--muted); margin-top: 6px; }
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
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 22px;
      align-items: center;
    }
    select, input[type=text] {
      border: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--text);
      padding: 10px 12px;
      border-radius: 14px;
    }
    .group-block {
      margin-bottom: 28px;
      padding: 18px;
      background: rgba(11,16,32,.32);
      border: 1px solid var(--border);
      border-radius: 28px;
    }
    .group-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 14px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    .group-title {
      font-size: 26px;
      font-weight: 800;
    }
    .group-meta { color: var(--muted); font-size: 13px; }
    .group-actions {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      background: rgba(18,25,51,.7);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 14px;
      margin-bottom: 16px;
    }
    .section-title {
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 6px;
    }
    .row { display: flex; gap: 10px; flex-wrap: wrap; }
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
    .name { font-size: 20px; font-weight: 700; }
    .meta { color: var(--muted); font-size: 13px; line-height: 1.4; }
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
    .control-label { font-size: 13px; color: var(--muted); margin: 10px 0 6px; }
    input[type=range] { width: 100%; }
    input[type=color] {
      width: 100%;
      height: 48px;
      border: none;
      background: transparent;
      padding: 0;
    }
    .tiny { font-size: 12px; color: var(--muted); }
    .footer-note { color: var(--muted); font-size: 13px; margin-top: 20px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div>
        <h1>Katnip Lights</h1>
        <div class="sub">Local-only bulb control from your Pi</div>
      </div>
      <div class="row">
        <button class="btn" onclick="refreshNow()">Refresh</button>
        <button class="btn" onclick="reloadDevices()">Reload devices.json</button>
      </div>
    </div>

    <div class="toolbar">
      <label class="tiny">Show group</label>
      <select id="groupFilter" onchange="loadDevices()"></select>
      <label class="tiny">New group</label>
      <input id="newGroupName" type="text" placeholder="Example: Bedroom" />
      <input id="newGroupPatterns" type="text" placeholder="Patterns like bedroom, bed" style="min-width:300px;" />
      <button class="btn" onclick="saveNewGroup()">Save group rule</button>
    </div>

    <div id="groups"></div>
    <div class="footer-note">If a bulb has no real white temperature channel, the temp slider fakes warmth and coolness by switching into RGB colour mode with near-white tints. Group rules are stored in <span class="mono">~/light_groups.json</span>. Raw scene payload sending supports DPS 6–10.</div>
  </div>

<script>
let groupRules = {};
const editorState = {};

function hsvToHex(h, s, v) {
  s /= 255;
  v /= 255;

  const c = v * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = v - c;

  let r = 0;
  let g = 0;
  let b = 0;

  if (h < 60) {
    r = c; g = x; b = 0;
  } else if (h < 120) {
    r = x; g = c; b = 0;
  } else if (h < 180) {
    r = 0; g = c; b = x;
  } else if (h < 240) {
    r = 0; g = x; b = c;
  } else if (h < 300) {
    r = x; g = 0; b = c;
  } else {
    r = c; g = 0; b = x;
  }

  const toHex = (n) => Math.round((n + m) * 255).toString(16).padStart(2, '0');
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

function hexToHsv(hex) {
  const m = hex.replace('#', '');
  const r = parseInt(m.substring(0, 2), 16) / 255;
  const g = parseInt(m.substring(2, 4), 16) / 255;
  const b = parseInt(m.substring(4, 6), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
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

async function api(path, method = 'POST', body = null) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : null,
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(txt || `HTTP ${res.status}`);
  }
  return await res.json();
}

function ensureEditorState(id) {
  if (!editorState[id]) {
    editorState[id] = {
      sendDp: '6',
      sendPayload: '',
      loadedDp: '6',
      loadedPayload: '',
      batchPayload: '',
    };
  }
  return editorState[id];
}

function rememberEditorState(id) {
  const state = ensureEditorState(id);
  const sendDpEl = document.getElementById(`send-dp-${id}`);
  const sendPayloadEl = document.getElementById(`send-scenepayload-${id}`);
  const loadedDpEl = document.getElementById(`scenedp-${id}`);
  const loadedPayloadEl = document.getElementById(`loaded-scenepayload-${id}`);
  const batchPayloadEl = document.getElementById(`batch-payload-${id}`);
  if (sendDpEl) state.sendDp = sendDpEl.value;
  if (sendPayloadEl) state.sendPayload = sendPayloadEl.value;
  if (loadedDpEl) state.loadedDp = loadedDpEl.value;
  if (loadedPayloadEl) state.loadedPayload = loadedPayloadEl.value;
  if (batchPayloadEl) state.batchPayload = batchPayloadEl.value;
}

function rememberAllEditorStates() {
  Object.keys(editorState).forEach((id) => rememberEditorState(id));
  document.querySelectorAll('[id^="send-dp-"]').forEach((el) => {
    const id = el.id.replace('send-dp-', '');
    rememberEditorState(id);
  });
}

function populateGroupFilter(groups) {
  const select = document.getElementById('groupFilter');
  const current = select.value || 'All';
  select.innerHTML = '';
  const all = document.createElement('option');
  all.value = 'All';
  all.textContent = 'All';
  select.appendChild(all);
  for (const g of groups) {
    const opt = document.createElement('option');
    opt.value = g;
    opt.textContent = g;
    select.appendChild(opt);
  }
  select.value = groups.includes(current) || current === 'All' ? current : 'All';
}

function buildDeviceCard(d) {
  const state = ensureEditorState(d.id);
  const card = document.createElement('div');
  card.className = 'card';
  const statusClass = d.error ? 'err' : (d.is_on ? 'on' : 'off');
  const statusText = d.error ? 'Error' : (d.is_on ? 'On' : 'Off');
  const colorHex = hsvToHex(d.hsv?.h || 1, d.hsv?.s || 1, d.hsv?.v || 1);
  const brightness = d.brightness || 25;
  const temp = d.temp ?? 128;
  const tempLabel = d.supports_temp ? 'White temp' : 'Fake temp';
  const tempHint = d.supports_temp
    ? 'Real warm/cool white channel'
    : 'Approximate warmth using RGB colour mode';

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

    <div class="control-label">${tempLabel}: <span id="tval-${d.id}">${temp}</span></div>
    <input type="range" min="0" max="255" value="${temp}" oninput="document.getElementById('tval-${d.id}').textContent=this.value" onchange="setTemp('${d.id}', this.value)">
    <div class="tiny">${tempHint}</div>

    <div class="control-label">Color</div>
    <input type="color" value="${colorHex}" onchange="setColor('${d.id}', this.value)">

    <div class="row">
      <button class="action" onclick="setWarm('${d.id}')">Warm</button>
      <button class="action" onclick="setNeutral('${d.id}')">Neutral</button>
      <button class="action" onclick="setCool('${d.id}')">Cool</button>
    </div>

    <div class="control-label">Loaded current payload</div>
    <div class="row">
      <select id="scenedp-${d.id}" style="flex:0 0 140px;" onchange="rememberEditorState('${d.id}')">
        <option value="6" ${state.loadedDp === '6' ? 'selected' : ''}>DP 6</option>
        <option value="7" ${state.loadedDp === '7' ? 'selected' : ''}>DP 7</option>
        <option value="8" ${state.loadedDp === '8' ? 'selected' : ''}>DP 8</option>
        <option value="9" ${state.loadedDp === '9' ? 'selected' : ''}>DP 9</option>
        <option value="10" ${state.loadedDp === '10' ? 'selected' : ''}>DP 10</option>
      </select>
      <button class="action" onclick="loadScenePayload('${d.id}')">Load current</button>
      <button class="action" onclick="copyLoadedPayload('${d.id}')">Copy → send</button>
    </div>
    <textarea id="loaded-scenepayload-${d.id}" readonly style="width:100%; min-height:72px; border:1px solid var(--border); background:var(--panel-3); color:var(--text); border-radius:16px; padding:12px; resize:vertical;">${state.loadedPayload || ''}</textarea>

    <div class="control-label">Send raw payload</div>
    <div class="row">
      <input id="send-dp-${d.id}" type="text" value="${state.sendDp || '6'}" placeholder="DP" style="flex:0 0 100px;" oninput="rememberEditorState('${d.id}')" />
      <button class="action primary" onclick="sendScenePayload('${d.id}')">Send raw</button>
    </div>
    <textarea id="send-scenepayload-${d.id}" style="width:100%; min-height:90px; border:1px solid var(--border); background:var(--panel-2); color:var(--text); border-radius:16px; padding:12px; resize:vertical;" oninput="rememberEditorState('${d.id}')">${state.sendPayload || ''}</textarea>

    <div class="control-label">Batch commands</div>
    <div class="tiny">One command per line: <span class="mono">dp=value</span>. Example: <span class="mono">2=scene</span> or <span class="mono">6=00b0cf00000000</span></div>
    <div class="row">
      <button class="action primary" onclick="sendBatchPayload('${d.id}')">Send batch</button>
    </div>
    <textarea id="batch-payload-${d.id}" style="width:100%; min-height:110px; border:1px solid var(--border); background:var(--panel-2); color:var(--text); border-radius:16px; padding:12px; resize:vertical;" placeholder="2=scene&#10;6=00b0cf00000000" oninput="rememberEditorState('${d.id}')">${state.batchPayload || ''}</textarea>

    <div class="tiny" style="margin-top:10px;">Mode: ${d.mode || 'unknown'}</div>
  `;
  return card;
}

function buildGroupBlock(groupName, devices) {
  const outer = document.createElement('div');
  outer.className = 'group-block';

  const head = document.createElement('div');
  head.className = 'group-head';
  head.innerHTML = `
    <div>
      <div class="group-title">${groupName}</div>
      <div class="group-meta">${devices.length} device(s)</div>
    </div>
  `;

  const actions = document.createElement('div');
  actions.className = 'group-actions';
  actions.innerHTML = `
    <div>
      <div class="section-title">Power</div>
      <div class="row">
        <button class="action primary" onclick="groupOn('${groupName}')">All on</button>
        <button class="action" onclick="groupOff('${groupName}')">All off</button>
      </div>
    </div>

    <div>
      <div class="section-title">White brightness</div>
      <input type="range" min="25" max="255" value="128" onchange="groupBrightness('${groupName}', this.value)">
    </div>

    <div>
      <div class="section-title">Temp / fake temp</div>
      <input type="range" min="0" max="255" value="128" onchange="groupTemp('${groupName}', this.value)">
    </div>

    <div>
      <div class="section-title">Color</div>
      <input type="color" value="#ffffff" onchange="groupColor('${groupName}', this.value)">
    </div>

    <div>
      <div class="section-title">Raw scene payload</div>
      <button class="action" onclick="groupScenePayload('${groupName}')">Send to group</button>
    </div>
  `;

  const grid = document.createElement('div');
  grid.className = 'grid';
  for (const d of devices) grid.appendChild(buildDeviceCard(d));

  outer.appendChild(head);
  outer.appendChild(actions);
  outer.appendChild(grid);
  return outer;
}

async function loadDevices() {
  rememberAllEditorStates();
  const data = await fetch('/api/devices').then((r) => r.json());
  groupRules = data.group_rules || {};
  const groups = data.groups || {};
  populateGroupFilter(Object.keys(groups));
  const filter = document.getElementById('groupFilter').value || 'All';

  const root = document.getElementById('groups');
  root.innerHTML = '';

  for (const [groupName, groupDevices] of Object.entries(groups)) {
    if (filter !== 'All' && filter !== groupName) continue;
    root.appendChild(buildGroupBlock(groupName, groupDevices));
  }
}

async function turnOn(id) { await api(`/api/device/${id}/on`); }
async function turnOff(id) { await api(`/api/device/${id}/off`); }
async function setBrightness(id, value) { await api(`/api/device/${id}/brightness`, 'POST', { brightness: parseInt(value, 10) }); }
async function setTemp(id, value) { await api(`/api/device/${id}/temp`, 'POST', { temp: parseInt(value, 10) }); }
async function setColor(id, value) {
  const hsv = hexToHsv(value);
  await api(`/api/device/${id}/color`, 'POST', hsv);
}
async function refreshOne(id) { await api(`/api/device/${id}/refresh`); await loadDevices(); }
async function refreshNow() { await api('/api/refresh'); await loadDevices(); }
async function reloadDevices() { await api('/api/reload'); await loadDevices(); }

async function setWarm(id) { await setTemp(id, 40); }
async function setNeutral(id) { await setTemp(id, 128); }
async function setCool(id) { await setTemp(id, 220); }

async function groupOn(name) { await api(`/api/group/${encodeURIComponent(name)}/on`); }
async function groupOff(name) { await api(`/api/group/${encodeURIComponent(name)}/off`); }
async function groupBrightness(name, value) { await api(`/api/group/${encodeURIComponent(name)}/brightness`, 'POST', { brightness: parseInt(value, 10) }); }
async function groupTemp(name, value) { await api(`/api/group/${encodeURIComponent(name)}/temp`, 'POST', { temp: parseInt(value, 10) }); }
async function groupColor(name, value) {
  const hsv = hexToHsv(value);
  await api(`/api/group/${encodeURIComponent(name)}/color`, 'POST', hsv);
}

async function loadScenePayload(id) {
  const select = document.getElementById(`scenedp-${id}`);
  const area = document.getElementById(`loaded-scenepayload-${id}`);
  const dp = select.value;
  area.value = 'Loading...';
  try {
    await api(`/api/device/${id}/refresh`);
    const data = await fetch('/api/devices').then((r) => r.json());
    const device = (data.devices || []).find((x) => x.id === id);
    if (!device) {
      area.value = '';
      alert('Could not find that device in the refreshed device list.');
      return;
    }
    const payload = (device.scene_payloads && device.scene_payloads[dp]) || '';
    area.value = payload;
    const state = ensureEditorState(id);
    state.loadedDp = dp;
    state.loadedPayload = payload;
    if (!payload) {
      alert(`No current payload was reported for DP ${dp}.`);
    }
  } catch (err) {
    area.value = '';
    alert(`Load current failed: ${err.message || err}`);
  }
}

function copyLoadedPayload(id) {
  const loaded = document.getElementById(`loaded-scenepayload-${id}`).value;
  const sendArea = document.getElementById(`send-scenepayload-${id}`);
  const selectedDp = document.getElementById(`scenedp-${id}`).value;
  document.getElementById(`send-dp-${id}`).value = selectedDp;
  sendArea.value = loaded;
  rememberEditorState(id);
}

async function sendScenePayload(id) {
  const dp = document.getElementById(`send-dp-${id}`).value.trim();
  const payload = document.getElementById(`send-scenepayload-${id}`).value.trim();
  if (!dp) {
    alert('Enter a DP/channel to send to.');
    return;
  }
  if (!payload) {
    alert('Paste a raw scene payload first.');
    return;
  }
  await api(`/api/device/${id}/scene_payload`, 'POST', { dp, payload });
  rememberEditorState(id);
}

async function sendBatchPayload(id) {
  const raw = document.getElementById(`batch-payload-${id}`).value;
  const lines = raw.split('\\n').map((x) => x.replace(/\\r/g, '').trim()).filter((x) => x.length > 0);
  if (!lines.length) {
    alert('Enter one or more commands like dp=value');
    return;
  }
  const commands = [];
  for (const line of lines) {
    const idx = line.indexOf('=');
    if (idx === -1) {
      alert(`Invalid line: ${line}`);
      return;
    }
    const dp = line.slice(0, idx).trim();
    const value = line.slice(idx + 1).trim();
    if (!dp || !value) {
      alert(`Invalid line: ${line}`);
      return;
    }
    commands.push({ dp, value });
  }
  await api(`/api/device/${id}/multi_payload`, 'POST', { commands });
}

async function groupScenePayload(name) {
  const dp = prompt('Scene DP/channel to send? Use 6, 7, 8, 9, 10 or any numeric DP', '6');
  if (!dp) return;
  const payload = prompt('Paste raw scene payload');
  if (!payload) return;
  await api(`/api/group/${encodeURIComponent(name)}/scene_payload`, 'POST', { dp, payload });
}

async function saveNewGroup() {
  const name = document.getElementById('newGroupName').value.trim();
  const patternsRaw = document.getElementById('newGroupPatterns').value.trim();
  if (!name || !patternsRaw) {
    alert('Give the group a name and at least one match pattern.');
    return;
  }
  const patterns = patternsRaw.split(',').map((x) => x.trim()).filter(Boolean);
  await api('/api/groups', 'POST', { name, patterns });
  document.getElementById('newGroupName').value = '';
  document.getElementById('newGroupPatterns').value = '';
  await reloadDevices();
}

loadDevices();
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
                    "group": d.get("group", "Ungrouped"),
                    "ip": d.get("ip", ""),
                    "product_name": d.get("product_name", ""),
                    "is_on": False,
                    "mode": "unknown",
                    "brightness": 25,
                    "temp": 128,
                    "supports_temp": False,
                    "hsv": {"h": 0, "s": 0, "v": 0},
                }
            if device_id in last_error:
                status["error"] = last_error[device_id]
            merged.append(status)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in merged:
        group_name = item.get("group", "Ungrouped")
        grouped.setdefault(group_name, []).append(item)

    return jsonify({
        "devices": merged,
        "groups": grouped,
        "group_rules": load_group_rules(),
    })


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


@app.route("/api/device/<device_id>/temp", methods=["POST"])
def api_temp(device_id: str):
    try:
        payload = request.get_json(force=True)
        temp = int(payload.get("temp", 128))
        result = set_temp(device_id, temp)
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


@app.route("/api/device/<device_id>/scene_payload", methods=["POST"])
def api_scene_payload(device_id: str):
    try:
        payload = request.get_json(force=True)
        dp = str(payload.get("dp", "6")).strip()
        scene_payload = str(payload.get("payload", "")).strip()
        result = set_scene_payload(device_id, dp, scene_payload)
        return jsonify({"ok": True, "device": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/device/<device_id>/multi_payload", methods=["POST"])
def api_multi_payload(device_id: str):
    try:
        payload = request.get_json(force=True)
        commands = payload.get("commands", [])
        if not isinstance(commands, list) or not commands:
            return jsonify({"error": "commands must be a non-empty list"}), 400
        client = get_client(device_id)
        states = {}
        for cmd in commands:
            dp = str(cmd.get("dp", "")).strip()
            value = cmd.get("value", "")
            if not dp.isdigit():
                return jsonify({"error": f"Invalid dp: {dp}"}), 400
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered == "true":
                    parsed = True
                elif lowered == "false":
                    parsed = False
                else:
                    try:
                        parsed = int(value)
                    except Exception:
                        parsed = value
            else:
                parsed = value
            states[dp] = parsed
        client.set_multiple_values(states)
        time.sleep(0.2)
        refresh_device_status(find_device(device_id))
        return jsonify({"ok": True, "device": last_status.get(device_id, {})})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/group/<group_name>/on", methods=["POST"])
def api_group_on(group_name: str):
    try:
        run_group_action(group_name, "on")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/group/<group_name>/off", methods=["POST"])
def api_group_off(group_name: str):
    try:
        run_group_action(group_name, "off")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/group/<group_name>/brightness", methods=["POST"])
def api_group_brightness(group_name: str):
    try:
        payload = request.get_json(force=True)
        run_group_action(group_name, "brightness", payload)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/group/<group_name>/temp", methods=["POST"])
def api_group_temp(group_name: str):
    try:
        payload = request.get_json(force=True)
        run_group_action(group_name, "temp", payload)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/group/<group_name>/color", methods=["POST"])
def api_group_color(group_name: str):
    try:
        payload = request.get_json(force=True)
        run_group_action(group_name, "color", payload)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/group/<group_name>/scene_payload", methods=["POST"])
def api_group_scene_payload(group_name: str):
    try:
        payload = request.get_json(force=True)
        run_group_action(group_name, "scene_payload", payload)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/groups", methods=["POST"])
def api_groups():
    try:
        payload = request.get_json(force=True)
        name = str(payload.get("name", "")).strip()
        patterns = [str(x).strip().lower() for x in payload.get("patterns", []) if str(x).strip()]
        if not name or not patterns:
            return jsonify({"error": "Need a group name and at least one pattern"}), 400
        rules = load_group_rules()
        rules[name] = patterns
        save_group_rules(rules)
        init_clients()
        refresh_all_status()
        return jsonify({"ok": True, "group_rules": rules})
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
