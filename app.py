"""JAY PC Agent — desktop app (PySide6).

Connects to the JAY broker over Tailscale, executes pc/command, and provides
a GUI to edit local app/site/script mappings.

Command actions understood on pc/command:
  {"action":"open_url","url":"..."}        - open a URL
  {"action":"open_app","app":"notepad"}    - launch an app
  {"action":"run_script","script":"..."}   - run a script/command line
  {"action":"open","object":"youtube"}     - resolve via LOCAL mappings, then run
  {"action":"ping"}                         - reply pong on pc/status
"""

import configparser
import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import paho.mqtt.client as mqtt

from PySide6.QtCore import Qt, QObject, Signal, Slot, QTimer, QRectF, QUrl
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QPen, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QListWidget,
    QListWidgetItem, QStackedWidget, QLabel, QTableWidget, QTableWidgetItem,
    QPushButton, QComboBox, QLineEdit, QHeaderView, QFormLayout, QPlainTextEdit,
    QMessageBox, QAbstractItemView, QCheckBox, QFrame, QGridLayout, QScrollArea,
    QSpinBox, QSizePolicy, QToolBar, QDialog,
)

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEnginePage
    HAVE_WEBENGINE = True
except Exception:
    QWebEngineView = None
    QWebEnginePage = None
    HAVE_WEBENGINE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("jay-pc-agent")

APP_NAME = "JAY PC Agent"
__version__ = "0.1.0"

try:
    import updater
except Exception:
    updater = None

try:
    import ptt
except Exception:
    ptt = None

try:
    import ww
except Exception:
    ww = None

# Named mutex so the installer (Inno AppMutex=JayPcAgentMutex) can detect/close
# a running instance during auto-update, and to enforce single-instance.
_MUTEX_HANDLE = None
def acquire_mutex():
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return
    try:
        import ctypes
        _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, "JayPcAgentMutex")
    except Exception:
        _MUTEX_HANDLE = None


# ── Data dir / config ────────────────────────────────────────────────────────
def data_dir() -> Path:
    if getattr(sys, "frozen", False):  # packaged with PyInstaller
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", Path.home())) / "JAY-PC-Agent"
        else:
            base = Path.home() / ".local" / "share" / "jay-pc-agent"
    else:
        base = Path(__file__).parent
    base.mkdir(parents=True, exist_ok=True)
    return base


DATA_DIR = data_dir()
CONFIG_PATH = DATA_DIR / "config.ini"
MAPPINGS_PATH = DATA_DIR / "mappings.json"

DEFAULT_MAPPINGS = [
    {"phrase": "youtube", "kind": "url", "value": "youtube.com"},
    {"phrase": "github", "kind": "url", "value": "github.com"},
    {"phrase": "gmail", "kind": "url", "value": "mail.google.com"},
    {"phrase": "spotify", "kind": "app", "value": "spotify"},
    {"phrase": "notepad", "kind": "app", "value": "notepad"},
    {"phrase": "calculator", "kind": "app", "value": "calc"},
]


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH, encoding="utf-8")
    if "broker" not in cfg:
        cfg["broker"] = {"host": "100.119.255.57", "port": "1884",
                         "username": "jay-agent", "password": ""}
    if "agent" not in cfg:
        cfg["agent"] = {"name": "laptop", "client_id": "jay-pc-agent-laptop"}
    if "ptt_enabled" not in cfg["agent"]:
        cfg["agent"]["ptt_enabled"] = "false"
    if "hotkey" not in cfg["agent"]:
        cfg["agent"]["hotkey"] = "ctrl+alt+j"
    if "ww_enabled" not in cfg["agent"]:
        cfg["agent"]["ww_enabled"] = "false"
    if "ww_word" not in cfg["agent"]:
        cfg["agent"]["ww_word"] = "hey_jarvis"
    if "stt" not in cfg:
        cfg["stt"] = {"url": "http://100.119.255.57:8080/api/stt"}
    if "ui" not in cfg:
        cfg["ui"] = {"theme": "dark"}
    if "theme" not in cfg["ui"]:
        cfg["ui"]["theme"] = "dark"
    if "web" not in cfg:
        host = cfg["broker"].get("host", "100.119.255.57")
        cfg["web"] = {"url": f"http://{host}:8080/"}
    if "url" not in cfg["web"]:
        host = cfg["broker"].get("host", "100.119.255.57")
        cfg["web"]["url"] = f"http://{host}:8080/"
    if "input_device" not in cfg["agent"]:
        cfg["agent"]["input_device"] = ""
    return cfg


def save_config(cfg: configparser.ConfigParser):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)


def load_mappings() -> list:
    if MAPPINGS_PATH.exists():
        try:
            data = json.loads(MAPPINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as exc:
            log.warning("Bad mappings.json: %s", exc)
    # first run: seed defaults
    MAPPINGS_PATH.write_text(json.dumps(DEFAULT_MAPPINGS, indent=2), encoding="utf-8")
    return list(DEFAULT_MAPPINGS)


def save_mappings(mappings: list):
    MAPPINGS_PATH.write_text(json.dumps(mappings, indent=2), encoding="utf-8")


# ── Command resolution + execution ───────────────────────────────────────────
def norm(s: str) -> str:
    return " ".join((s or "").lower().split())

_TARGET_SUFFIXES = (
    " on my computer", " on the computer", " on this computer", " on computer",
    " on my laptop", " on the laptop", " on this laptop", " on laptop",
    " on my pc", " on the pc", " on this pc", " on pc",
    " on my desktop", " on the desktop", " on desktop",
    " on my machine", " on the machine", " on machine",
)

def strip_target(key: str) -> str:
    """Drop a trailing \"on <device>\" phrase the server may not have removed."""
    k = key
    for suf in _TARGET_SUFFIXES:
        if k.endswith(suf):
            return k[: -len(suf)].strip()
    return k


def resolve_object(obj: str, mappings: list):
    """Local mappings first; else treat as a domain. Returns (payload, friendly)."""
    key = strip_target(norm(obj))
    for m in mappings:
        if norm(m.get("phrase")) == key:
            kind = (m.get("kind") or "url").lower()
            val = m.get("value") or ""
            if kind == "app":
                return {"action": "open_app", "app": val}, obj.title()
            if kind == "script":
                return {"action": "run_script", "script": val}, obj.title()
            url = val if val.startswith(("http://", "https://")) else "https://" + val
            return {"action": "open_url", "url": url}, obj.title()
    domain = key.replace(" ", "")
    if "." not in domain:
        domain += ".com"
    return {"action": "open_url", "url": "https://" + domain}, obj.title()


def execute_command(payload: dict, mappings: list) -> str:
    action = (payload.get("action") or "").strip().lower()

    if action == "open":
        resolved, _ = resolve_object(payload.get("object", ""), mappings)
        return execute_command(resolved, mappings)

    if action == "open_url":
        url = (payload.get("url") or "").strip()
        if not url:
            return "error: open_url missing url"
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        webbrowser.open(url)
        return f"opened url: {url}"

    if action == "open_app":
        app = (payload.get("app") or "").strip()
        if not app:
            return "error: open_app missing app"
        try:
            if sys.platform == "win32":
                os.startfile(app)  # noqa
            else:
                subprocess.Popen(["xdg-open", app])
            return f"opened app: {app}"
        except Exception:
            try:
                subprocess.Popen(app, shell=True)
                return f"opened app (shell): {app}"
            except Exception as exc:
                return f"error: {exc}"

    if action == "run_script":
        script = (payload.get("script") or "").strip()
        if not script:
            return "error: run_script missing script"
        try:
            subprocess.Popen(script, shell=True)
            return f"ran script: {script}"
        except Exception as exc:
            return f"error: {exc}"

    if action == "ping":
        return "pong"

    return f"error: unknown action '{action}'"


# ── MQTT bridge (emits Qt signals so the GUI updates safely) ──────────────────
class Bridge(QObject):
    status = Signal(bool, str)     # connected?, detail
    activity = Signal(str)         # log line for the activity pane
    update_result = Signal(bool, bool, str, str)  # manual?, available?, tag, asset_url
    skill_state = Signal(str, str)  # skill name, retained-state JSON payload


STATE_TOPICS = ("timer",)  # retained skill/<x>/state topics the dashboards render


class AgentMQTT:
    def __init__(self, cfg, bridge: Bridge, mappings_ref):
        self.bridge = bridge
        self.mappings_ref = mappings_ref          # callable returning current LOCAL list
        self._global = []                          # from retained skill/globalmap/state
        self._global_lock = threading.Lock()
        self.cfg = cfg
        self.enabled = True
        self.client = None
        self._build()

    def _build(self):
        cfg = self.cfg
        self.host = cfg["broker"]["host"]
        self.port = int(cfg["broker"].get("port", "1884") or "1884")
        self.username = cfg["broker"]["username"]
        self.password = cfg["broker"]["password"]
        self.name = cfg["agent"].get("name", "laptop")
        self.client_id = cfg["agent"].get("client_id", f"jay-pc-agent-{self.name}")
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        if self.password:
            self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def apply_config(self, cfg):
        """Reconnect with new broker settings (called from Settings save)."""
        self.stop()
        self.cfg = cfg
        self._build()
        self.start()

    def _on_connect(self, client, userdata, flags, rc, properties):
        if rc == 0:
            client.subscribe("pc/command", qos=1)               # shared / broadcast
            client.subscribe(f"pc/command/{self.name}", qos=1)  # this machine only
            for _skill in STATE_TOPICS:
                client.subscribe(f"skill/{_skill}/state", qos=1)  # retained dashboards
            client.subscribe("skill/globalmap/state", qos=1)      # shared open-X mappings
            self.bridge.status.emit(True, f"{self.host}:{self.port}")
            self.bridge.activity.emit(f"Connected as '{self.name}' -> pc/command/{self.name}")
            self._publish_status("online")
        else:
            self.bridge.status.emit(False, f"auth/connect failed (rc={rc})")
            self.bridge.activity.emit(f"Connect failed: rc={rc}")

    def _on_disconnect(self, client, userdata, flags, rc, properties):
        self.bridge.status.emit(False, "disconnected — reconnecting")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        # Retained dashboard state updates the UI regardless of the command toggle.
        if topic.startswith("skill/") and topic.endswith("/state"):
            if topic == "skill/globalmap/state":
                try:
                    data = json.loads(msg.payload.decode() or "{}")
                    gm = data.get("mappings", []) or []
                except Exception:
                    gm = []
                with self._global_lock:
                    self._global = gm
                self.bridge.activity.emit(f"[MAP] global mappings updated ({len(gm)})")
                return
            try:
                self.bridge.skill_state.emit(topic.split("/")[1], msg.payload.decode() or "{}")
            except Exception:
                pass
            return
        if not self.enabled:
            return
        if topic not in ("pc/command", f"pc/command/{self.name}"):
            return
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            self.bridge.activity.emit(f"Bad JSON: {msg.payload!r}")
            return
        result = execute_command(payload, self._merged())
        self.bridge.activity.emit(f"{payload} -> {result}")
        self._publish_status(result)

    def _merged(self):
        """Local mappings win; global mappings fill in the rest (then domain fallback)."""
        local = list(self.mappings_ref() or [])
        with self._global_lock:
            return local + list(self._global)

    def _publish_status(self, status: str):
        try:
            self.client.publish("pc/status",
                                json.dumps({"client_id": self.client_id, "name": self.name,
                                            "status": status}), qos=1)
        except Exception:
            pass

    def publish_stt(self, text: str):
        """Publish push-to-talk transcript into the normal NLP pipeline."""
        try:
            self.client.publish("stt/text",
                                json.dumps({"text": text, "source": "pc",
                                            "name": self.name}), qos=1)
            return True
        except Exception as exc:
            self.bridge.activity.emit(f"PTT publish failed: {exc}")
            return False

    def publish_skill(self, skill: str, intent: str, data: dict = None):
        """Send a structured request straight to a skill (mirrors the web skillReq)."""
        try:
            self.client.publish(f"skill/{skill}/request",
                                json.dumps({"intent": intent, "data": data or {},
                                            "source": "pc"}), qos=1)
            return True
        except Exception as exc:
            self.bridge.activity.emit(f"skill publish failed: {exc}")
            return False

    def set_enabled(self, on: bool):
        self.enabled = on
        self._publish_status("enabled" if on else "disabled")

    def start(self):
        if not self.password:
            self.bridge.status.emit(False, "no password set — open Settings")
            return
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()

    def stop(self):
        try:
            self._publish_status("offline")
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


def list_input_devices():
    """Names of available input (microphone) devices, deduped, in order."""
    try:
        import sounddevice as sd
        seen, out = set(), []
        for d in sd.query_devices():
            if d.get("max_input_channels", 0) > 0:
                name = d.get("name", "")
                if name and name not in seen:
                    seen.add(name); out.append(name)
        return out
    except Exception:
        return []


# ── GUI ──────────────────────────────────────────────────────────────────────
# Color tokens ported from the web dashboard's CSS variables, so the app matches
# the site exactly (espresso dark + paper light, amber accent).
THEMES = {
    "dark": dict(
        bg="#14110d", bg2="#1a1610", elevated="#1e1913", surface="#221c15",
        surface2="#2a2319", hover="#2f2820",
        border="rgba(240,210,170,0.09)", border2="rgba(240,210,170,0.18)",
        text="#f2ebdc", text2="#b3a892", text3="#7c715d",
        accent="#e8a04c", accent2="#e07a3c", accent_soft="rgba(232,160,76,0.12)",
        success="#8fb96b", danger="#e07a5f",
    ),
    "light": dict(
        bg="#f3ede0", bg2="#efe8d8", elevated="#fffdf7", surface="#fffdf7",
        surface2="#f6f0e3", hover="#efe8d8",
        border="rgba(60,45,25,0.10)", border2="rgba(60,45,25,0.20)",
        text="#2a2114", text2="#6b5d48", text3="#9a8c74",
        accent="#cf7a28", accent2="#b85f24", accent_soft="rgba(207,122,40,0.10)",
        success="#5e8c3e", danger="#c75a3f",
    ),
}
ON_ACCENT = "#1c130a"  # dark ink on the amber gradient, both themes (matches web)
TOK = dict(THEMES["dark"])  # live tokens; painted widgets (timer ring) read these


def build_qss(theme: str) -> str:
    t = THEMES.get(theme, THEMES["dark"])
    TOK.clear(); TOK.update(t)
    grad = f"qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {t['accent']},stop:1 {t['accent2']})"
    return f"""
QMainWindow, QWidget {{ background: {t['bg']}; color: {t['text']};
    font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }}

#Sidebar {{ background: {t['bg2']}; border-right: 1px solid {t['border']}; }}
#Sidebar QListWidget {{ background: transparent; border: none; outline: none; }}
#Sidebar QListWidget::item {{ padding: 11px 14px; border-radius: 11px; margin: 2px 10px;
    color: {t['text2']}; }}
#Sidebar QListWidget::item:selected {{ background: {t['accent_soft']}; color: {t['accent']}; }}
#Sidebar QListWidget::item:hover:!selected {{ background: {t['hover']}; color: {t['text']}; }}
#Logo {{ background: {grad}; color: {ON_ACCENT}; border-radius: 11px;
    font-size: 20px; font-weight: 800; min-width: 38px; max-width: 38px;
    min-height: 38px; max-height: 38px; qproperty-alignment: AlignCenter; }}
#Brand {{ color: {t['text']}; font-size: 19px; font-weight: 800; }}
#BrandSub {{ color: {t['text3']}; font-size: 10px; letter-spacing: 3px; }}

#PageTitle {{ font-size: 26px; font-weight: 800; color: {t['text']}; }}
#Eyebrow {{ color: {t['accent']}; font-family: 'Consolas', monospace; font-size: 11px;
    letter-spacing: 3px; }}
#GroupTitle {{ color: {t['text3']}; font-family: 'Consolas', monospace; font-size: 11px;
    letter-spacing: 2px; }}
#Muted {{ color: {t['text3']}; }}
#CardTitle {{ color: {t['text3']}; font-family: 'Consolas', monospace; font-size: 11px;
    letter-spacing: 2px; }}

#Card {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 16px; }}
#TimerCard {{ background: {t['elevated']}; border: 1px solid {t['border']}; border-radius: 11px; }}
#TimerCard[ringing="true"] {{ border: 1px solid {t['accent']}; }}
#TimerLabel {{ color: {t['text2']}; font-weight: 600; }}

QTableWidget {{ background: {t['elevated']}; border: 1px solid {t['border']}; border-radius: 11px;
    gridline-color: {t['border']}; selection-background-color: {t['surface2']}; }}
QHeaderView::section {{ background: {t['surface2']}; color: {t['text2']}; padding: 8px;
    border: none; border-bottom: 1px solid {t['border']}; }}

QLineEdit {{ background: {t['elevated']}; border: 1px solid {t['border']};
    border-radius: 11px; padding: 11px 14px; color: {t['text']}; }}
QComboBox {{ background: {t['elevated']}; border: 1px solid {t['border']};
    border-radius: 9px; padding: 5px 10px; color: {t['text']}; min-height: 22px; }}
QSpinBox {{ background: {t['elevated']}; border: 1px solid {t['border']};
    border-radius: 11px; padding: 9px 12px; color: {t['text']};
    font-family: 'Consolas', monospace; }}
QSpinBox:focus {{ border: 1px solid {t['accent']}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none;
    background: {t['surface2']}; }}
QSpinBox::up-arrow {{ image: none; width: 7px; height: 7px;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid {t['text3']}; }}
QSpinBox::down-arrow {{ image: none; width: 7px; height: 7px;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {t['text3']}; }}
QLineEdit::placeholder {{ color: {t['text3']}; }}
QLineEdit:focus, QComboBox:focus {{ border: 1px solid {t['accent']}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{ width: 8px; height: 8px;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {t['text2']}; margin-right: 7px; }}
QComboBox QAbstractItemView {{ background: {t['elevated']}; color: {t['text']};
    selection-background-color: {t['accent_soft']}; selection-color: {t['accent']};
    border: 1px solid {t['border']}; }}

QPushButton {{ background: {grad}; color: {ON_ACCENT}; border: none; border-radius: 11px;
    padding: 11px 20px; font-weight: 700; }}
QPushButton:hover {{ background: {t['accent']}; }}
QPushButton#Ghost {{ background: transparent; color: {t['text2']};
    border: 1px solid {t['border']}; }}
QPushButton#Ghost:hover {{ border: 1px solid {t['accent']}; color: {t['accent']}; }}
QPushButton#Danger {{ background: transparent; color: {t['text3']};
    border: 1px solid {t['border']}; padding: 6px 14px; font-weight: 600; }}
QPushButton#Danger:hover {{ border: 1px solid {t['danger']}; color: {t['danger']}; }}

QPlainTextEdit {{ background: {t['elevated']}; border: 1px solid {t['border']}; border-radius: 11px;
    color: {t['text2']}; font-family: 'Consolas', monospace; font-size: 12px; }}
QCheckBox {{ color: {t['text']}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['surface2']}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
#StatusDot {{ font-size: 12px; color: {t['text3']}; padding: 10px 16px; }}
QToolBar#TopBar {{ background: {t['bg2']}; border: none; border-bottom: 1px solid {t['border']};
    padding: 6px 10px; spacing: 4px; }}
QToolBar#TopBar QToolButton {{ background: transparent; color: {t['text2']};
    border: none; border-radius: 8px; padding: 7px 13px; font-weight: 600; }}
QToolBar#TopBar QToolButton:hover {{ background: {t['hover']}; color: {t['text']}; }}
QToolBar#TopBar::separator {{ background: {t['border']}; width: 1px; margin: 6px 6px; }}
QDialog {{ background: {t['bg']}; }}
#WebFallback {{ color: {t['text2']}; font-size: 15px; }}
"""


QSS = build_qss("dark")


def dot_icon(color_hex: str) -> QIcon:
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color_hex))
    p.setPen(Qt.NoPen)
    p.drawEllipse(3, 3, 10, 10)
    p.end()
    return QIcon(pm)


def persona_logo_icon(size: int = 64) -> QIcon:
    """Dual-persona mark: rounded tile, left half orange 'J' (Jay),
    right half blue 'N' (Nova). Used as the app / taskbar icon."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    radius = size * 0.22
    # whole tile orange, then paint the right half blue (clipped)
    p.setBrush(QColor("#ff7a1a"))
    p.drawRoundedRect(0, 0, size, size, radius, radius)
    p.setClipRect(size // 2, 0, size - size // 2, size)
    p.setBrush(QColor("#2f6bff"))
    p.drawRoundedRect(0, 0, size, size, radius, radius)
    p.setClipping(False)
    # letters: J on the orange half, N on the blue half
    f = QFont("Segoe UI", int(size * 0.40))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(0, 0, size / 2, size), Qt.AlignCenter, "J")
    p.drawText(QRectF(size / 2, 0, size / 2, size), Qt.AlignCenter, "N")
    p.end()
    return QIcon(pm)


class MappingsPage(QWidget):
    def __init__(self, get_mappings, set_mappings, get_globals=None):
        super().__init__()
        self.get_mappings = get_mappings
        self.set_mappings = set_mappings
        self.get_globals = get_globals or (lambda: [])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24); lay.setSpacing(14)
        title = QLabel("Local Mappings"); title.setObjectName("PageTitle")
        lay.addWidget(title)
        lay.addWidget(QLabel("Spoken phrase → what to open on this PC. Local entries override the server's global list."))

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Phrase", "Type", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 140)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        lay.addWidget(self.table)

        btns = QHBoxLayout()
        add = QPushButton("Add"); add.clicked.connect(self.add_row)
        rem = QPushButton("Delete Selected"); rem.setObjectName("Ghost"); rem.clicked.connect(self.del_row)
        save = QPushButton("Save"); save.clicked.connect(self.save)
        btns.addWidget(add); btns.addWidget(rem); btns.addStretch(1); btns.addWidget(save)
        lay.addLayout(btns)

        gtitle = QLabel("Global (shared) mappings"); gtitle.setObjectName("PageTitle")
        lay.addWidget(gtitle)
        lay.addWidget(QLabel("Read-only \u2014 managed from the JAY website. Local entries above override these."))
        self.gtable = QTableWidget(0, 3)
        self.gtable.setHorizontalHeaderLabels(["Phrase", "Type", "Value"])
        self.gtable.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.gtable.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.gtable.setColumnWidth(1, 140)
        self.gtable.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.gtable.verticalHeader().setVisible(False)
        self.gtable.verticalHeader().setDefaultSectionSize(40)
        self.gtable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.gtable.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.gtable)
        _gm = list(self.get_globals() or [])
        for m in _gm:
            r = self.gtable.rowCount(); self.gtable.insertRow(r)
            self.gtable.setItem(r, 0, QTableWidgetItem(m.get("phrase", "")))
            self.gtable.setItem(r, 1, QTableWidgetItem((m.get("kind") or "url")))
            self.gtable.setItem(r, 2, QTableWidgetItem(m.get("value", "")))
        if not _gm:
            self.gtable.insertRow(0)
            self.gtable.setItem(0, 0, QTableWidgetItem("(none yet)"))
        self.reload()

    def _type_combo(self, current="url"):
        c = QComboBox(); c.addItems(["url", "app", "script"])
        c.setCurrentText(current if current in ("url", "app", "script") else "url")
        c.setFixedHeight(34); c.setMinimumWidth(112)
        wrap = QWidget(); wl = QHBoxLayout(wrap)
        wl.setContentsMargins(8, 0, 8, 0); wl.addWidget(c)
        wrap._combo = c
        return wrap

    def _row_combo(self, r):
        w = self.table.cellWidget(r, 1)
        return getattr(w, "_combo", w) if w else None

    def reload(self):
        rows = self.get_mappings()
        self.table.setRowCount(0)
        for m in rows:
            r = self.table.rowCount(); self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(m.get("phrase", "")))
            self.table.setCellWidget(r, 1, self._type_combo(m.get("kind", "url")))
            self.table.setItem(r, 2, QTableWidgetItem(m.get("value", "")))

    def add_row(self):
        r = self.table.rowCount(); self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(""))
        self.table.setCellWidget(r, 1, self._type_combo("url"))
        self.table.setItem(r, 2, QTableWidgetItem(""))

    def del_row(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def collect(self) -> list:
        out = []
        for r in range(self.table.rowCount()):
            phrase = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            combo = self._row_combo(r)
            kind = combo.currentText() if combo else "url"
            value = (self.table.item(r, 2).text() if self.table.item(r, 2) else "").strip()
            if phrase and value:
                out.append({"phrase": phrase, "kind": kind, "value": value})
        return out

    def save(self):
        rows = self.collect()
        save_mappings(rows)
        self.set_mappings(rows)
        QMessageBox.information(self, "Saved", f"Saved {len(rows)} mapping(s).")


class SettingsPage(QWidget):
    def __init__(self, cfg, on_save, on_theme=None):
        super().__init__()
        self.cfg = cfg; self.on_save = on_save; self.on_theme = on_theme
        lay = QVBoxLayout(self); lay.setContentsMargins(28, 24, 28, 24); lay.setSpacing(14)
        title = QLabel("Settings"); title.setObjectName("PageTitle"); lay.addWidget(title)
        form = QFormLayout(); form.setSpacing(12)
        self.theme = QComboBox(); self.theme.addItems(["dark", "light"])
        _th = cfg["ui"].get("theme", "dark")
        if self.theme.findText(_th) >= 0:
            self.theme.setCurrentText(_th)
        self.theme.currentTextChanged.connect(self._theme_changed)
        form.addRow("Theme", self.theme)
        self.name = QLineEdit(cfg["agent"].get("name", "laptop"))
        self.host = QLineEdit(cfg["broker"]["host"])
        self.port = QLineEdit(cfg["broker"]["port"])
        self.user = QLineEdit(cfg["broker"]["username"])
        self.pw = QLineEdit(cfg["broker"].get("password", "")); self.pw.setEchoMode(QLineEdit.Password)
        self.show_pw = QCheckBox("show")
        self.show_pw.toggled.connect(lambda on: self.pw.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        form.addRow("This PC's name", self.name)
        form.addRow("Broker host", self.host)
        form.addRow("Broker port", self.port)
        form.addRow("Broker user", self.user)
        pw_row = QHBoxLayout(); pw_row.addWidget(self.pw); pw_row.addWidget(self.show_pw)
        form.addRow("Broker password", pw_row)

        # ── voice / push-to-talk ──
        agent = cfg["agent"]
        self.ptt_enabled = QCheckBox("Enable push-to-talk")
        self.ptt_enabled.setChecked(agent.get("ptt_enabled", "false").lower() == "true")
        self.hotkey = QLineEdit(agent.get("hotkey", "ctrl+alt+j"))
        self.stt_url = QLineEdit(cfg["stt"].get("url", "http://100.119.255.57:8080/api/stt"))
        self.web_url = QLineEdit(cfg["web"].get("url", "http://100.119.255.57:8080/"))
        self.ww_enabled = QCheckBox("Enable wake word (always-on listening)")
        self.ww_enabled.setChecked(agent.get("ww_enabled", "false").lower() == "true")
        self.ww_word = QComboBox()
        self.ww_word.addItems(["hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy"])
        _w = agent.get("ww_word", "hey_jarvis")
        if self.ww_word.findText(_w) >= 0:
            self.ww_word.setCurrentText(_w)
        self.mic = QComboBox()
        self.mic.addItem("System default", "")
        for _name in list_input_devices():
            self.mic.addItem(_name, _name)
        _cur = agent.get("input_device", "")
        _mi = self.mic.findData(_cur)
        self.mic.setCurrentIndex(_mi if _mi >= 0 else 0)
        form.addRow("Push-to-talk", self.ptt_enabled)
        form.addRow("Hold-to-talk hotkey", self.hotkey)
        form.addRow("Wake word", self.ww_enabled)
        form.addRow("Wake phrase", self.ww_word)
        form.addRow("Microphone", self.mic)
        form.addRow("STT server URL", self.stt_url)
        form.addRow("Dashboard URL", self.web_url)
        lay.addLayout(form)
        ver = QHBoxLayout()
        ver.addWidget(QLabel(f"Version {__version__}"))
        ver.addStretch(1)
        self.upd_btn = QPushButton("Check for updates"); self.upd_btn.setObjectName("Ghost")
        ver.addWidget(self.upd_btn)
        lay.addLayout(ver)
        lay.addWidget(QLabel("Hold the hotkey to talk; release to send. "
                             "Wake word listens continuously while enabled and "
                             "uses no CPU when off."))
        save = QPushButton("Save"); save.clicked.connect(self.save)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(save); lay.addLayout(row)
        lay.addStretch(1)

    def _theme_changed(self, name):
        self.cfg["ui"]["theme"] = name
        save_config(self.cfg)
        if self.on_theme:
            self.on_theme(name)

    def save(self):
        name = self.name.text().strip() or "laptop"
        self.cfg["agent"]["name"] = name
        self.cfg["agent"]["client_id"] = f"jay-pc-agent-{name}"
        self.cfg["broker"]["host"] = self.host.text().strip()
        self.cfg["broker"]["port"] = self.port.text().strip() or "1884"
        self.cfg["broker"]["username"] = self.user.text().strip()
        self.cfg["broker"]["password"] = self.pw.text()
        self.cfg["agent"]["ptt_enabled"] = "true" if self.ptt_enabled.isChecked() else "false"
        self.cfg["agent"]["hotkey"] = self.hotkey.text().strip() or "ctrl+alt+j"
        self.cfg["agent"]["ww_enabled"] = "true" if self.ww_enabled.isChecked() else "false"
        self.cfg["agent"]["ww_word"] = self.ww_word.currentText().strip() or "hey_jarvis"
        self.cfg["agent"]["input_device"] = self.mic.currentData() or ""
        if "stt" not in self.cfg:
            self.cfg["stt"] = {}
        self.cfg["stt"]["url"] = self.stt_url.text().strip() or "http://100.119.255.57:8080/api/stt"
        if "web" not in self.cfg:
            self.cfg["web"] = {}
        self.cfg["web"]["url"] = self.web_url.text().strip() or "http://100.119.255.57:8080/"
        save_config(self.cfg)
        self.on_save(self.cfg)
        QMessageBox.information(self, "Saved", "Settings saved \u2014 reconnecting to the broker.")


class ActivityPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self); lay.setContentsMargins(28, 24, 28, 24); lay.setSpacing(14)
        title = QLabel("Activity"); title.setObjectName("PageTitle"); lay.addWidget(title)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); lay.addWidget(self.log)

    @Slot(str)
    def append(self, line):
        self.log.appendPlainText(line)


class TimerRing(QWidget):
    """Circular countdown ring, painted to match the web .ring (track + amber arc)."""
    def __init__(self):
        super().__init__()
        self.setFixedSize(96, 96)
        self._frac = 1.0      # 0..1 remaining
        self._text = "0:00"
        self._ringing = False

    def set_values(self, frac, text, ringing):
        self._frac = max(0.0, min(1.0, frac)); self._text = text; self._ringing = ringing
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height())
        m = 7
        rect = QRectF(m, m, side - 2 * m, side - 2 * m)
        # track (faint accent ring)
        track = QColor(TOK["accent"]); track.setAlpha(40)
        p.setPen(QPen(track, 6)); p.drawArc(rect, 0, 360 * 16)
        # progress arc (start at top, clockwise)
        pen = QPen(QColor(TOK["accent"]), 6); pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 90 * 16, -int(360 * self._frac * 16))
        # center text
        p.setPen(QColor(TOK["accent"] if self._ringing else TOK["text"]))
        f = QFont("Consolas"); f.setPointSize(13); f.setBold(False); p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, self._text)
        p.end()


class TimerCard(QFrame):
    """One timer: ring + label + Cancel/Dismiss, matching the web .timer-card."""
    def __init__(self, on_cancel):
        super().__init__()
        self.setObjectName("TimerCard")
        self.setFixedWidth(168)
        self._on_cancel = on_cancel
        self._id = None; self._name = ""
        v = QVBoxLayout(self); v.setContentsMargins(16, 18, 16, 14); v.setSpacing(12)
        v.setAlignment(Qt.AlignHCenter)
        self.ring = TimerRing(); v.addWidget(self.ring, 0, Qt.AlignHCenter)
        self.label = QLabel("Timer"); self.label.setObjectName("TimerLabel")
        self.label.setAlignment(Qt.AlignHCenter)
        v.addWidget(self.label)
        self.btn = QPushButton("Cancel"); self.btn.setObjectName("Danger")
        self.btn.clicked.connect(self._cancel)
        v.addWidget(self.btn)

    def _cancel(self):
        self._on_cancel(self._id, self._name)

    def set_timer(self, t):
        self._id = t["id"]; self._name = t.get("name") or ""
        ringing = t.get("status") == "ringing"
        self.label.setText(t.get("name") or t.get("duration_text") or "Timer")
        self.btn.setText("Dismiss" if ringing else "Cancel")
        self.setProperty("ringing", "true" if ringing else "false")
        self.style().unpolish(self); self.style().polish(self)

    def tick(self, remaining, total, ringing):
        if ringing:
            self.ring.set_values(1.0, "\u2713", True)
        else:
            frac = (remaining / total) if total else 0
            self.ring.set_values(frac, TimersPage._fmt(remaining), False)


class TimersPage(QWidget):
    """Active timers + a New Timer form, fed by the retained skill/timer/state topic.

    Countdown ticks locally off a monotonic clock so it stays smooth between
    state messages and is immune to clock/timezone skew vs. the server.
    """
    def __init__(self, on_create, on_cancel):
        super().__init__()
        self.on_create = on_create
        self.on_cancel = on_cancel
        self._pending_cancel = set()
        self._timers = []     # {id,name,duration_text,status,base_remaining,total,base_mono}
        self._cards = []

        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); outer.addWidget(scroll)
        page = QWidget(); scroll.setWidget(page)
        lay = QVBoxLayout(page); lay.setContentsMargins(40, 30, 40, 40); lay.setSpacing(18)

        eyebrow = QLabel("// time"); eyebrow.setObjectName("Eyebrow")
        title = QLabel("Timers"); title.setObjectName("PageTitle")
        lay.addWidget(eyebrow); lay.addWidget(title)

        # New Timer card
        new_card = QFrame(); new_card.setObjectName("Card")
        nv = QVBoxLayout(new_card); nv.setContentsMargins(26, 22, 26, 24); nv.setSpacing(16)
        nt = QLabel("NEW TIMER"); nt.setObjectName("CardTitle"); nv.addWidget(nt)
        form = QHBoxLayout(); form.setSpacing(12)
        self.name_in = QLineEdit(); self.name_in.setPlaceholderText("Label (optional)")
        self.mins_in = QSpinBox(); self.mins_in.setRange(1, 600); self.mins_in.setValue(5)
        self.mins_in.setSuffix(" min"); self.mins_in.setFixedWidth(110)
        start = QPushButton("Start"); start.clicked.connect(self._create)
        form.addWidget(self.name_in, 1); form.addWidget(self.mins_in); form.addWidget(start)
        nv.addLayout(form)
        lay.addWidget(new_card)

        # Active timers card
        act_card = QFrame(); act_card.setObjectName("Card")
        av = QVBoxLayout(act_card); av.setContentsMargins(26, 22, 26, 24); av.setSpacing(16)
        at = QLabel("ACTIVE TIMERS"); at.setObjectName("CardTitle"); av.addWidget(at)
        self.empty = QLabel("No active timers"); self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignCenter)
        av.addWidget(self.empty)
        self.grid_host = QWidget(); self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0); self.grid.setSpacing(14)
        self.grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        av.addWidget(self.grid_host)
        lay.addWidget(act_card)
        lay.addStretch(1)

        self._tick = QTimer(self); self._tick.setInterval(1000)
        self._tick.timeout.connect(self._render_tick); self._tick.start()

    def _create(self):
        name = self.name_in.text().strip()
        mins = self.mins_in.value()
        text = (f"set a {name} timer for {mins} minutes" if name
                else f"set a timer for {mins} minutes")
        self.on_create(text)
        self.name_in.clear(); self.mins_in.setValue(5)

    def _cancel(self, timer_id, name):
        if timer_id:
            self._pending_cancel.add(timer_id)
        data = {}
        if timer_id:
            data["timer_id"] = timer_id
        if name:
            data["timer_name"] = name
        self.on_cancel(data)
        self._rebuild_cards()

    @staticmethod
    def _fmt(secs):
        secs = max(0, int(round(secs)))
        h, rem = divmod(secs, 3600); m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @Slot(str)
    def update_state(self, payload):
        try:
            data = json.loads(payload or "{}")
        except Exception:
            data = {}
        now = time.monotonic()
        incoming = data.get("timers") or []
        ids = {t.get("id") for t in incoming}
        # drop pending-cancels the server has confirmed gone
        self._pending_cancel = {i for i in self._pending_cancel if i in ids}
        self._timers = [{
            "id": t.get("id"),
            "name": t.get("name"),
            "duration_text": t.get("duration_text"),
            "status": t.get("status") or "scheduled",
            "base_remaining": int(t.get("remaining_seconds") or 0),
            "total": int(t.get("total_seconds") or t.get("remaining_seconds") or 1) or 1,
            "base_mono": now,
        } for t in incoming]
        self._rebuild_cards()

    def _visible(self):
        return [t for t in self._timers if t["id"] not in self._pending_cancel]

    def _rebuild_cards(self):
        vis = self._visible()
        self.empty.setVisible(not vis)
        self.grid_host.setVisible(bool(vis))
        # clear grid
        for c in self._cards:
            c.setParent(None); c.deleteLater()
        self._cards = []
        cols = max(1, (self.width() - 130) // 182) or 1
        for i, t in enumerate(vis):
            card = TimerCard(self._cancel)
            card.set_timer(t)
            self.grid.addWidget(card, i // cols, i % cols)
            self._cards.append(card)
        self._render_tick()

    def _render_tick(self):
        now = time.monotonic()
        vis = self._visible()
        for card, t in zip(self._cards, vis):
            if t["status"] == "ringing":
                card.tick(0, t["total"], True)
            else:
                remaining = t["base_remaining"] - (now - t["base_mono"])
                card.tick(max(0, remaining), t["total"], False)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 780)
        self.cfg = load_config()
        self._mappings = load_mappings()
        self._lock = threading.Lock()

        self.bridge = Bridge()
        self.agent = AgentMQTT(self.cfg, self.bridge, self.get_mappings)
        self.ptt_ctl = None
        self.ww_ctl = None

        # toolbar holds the app-only actions; the website fills the rest of the window
        tb = QToolBar(); tb.setObjectName("TopBar"); tb.setMovable(False)
        self.addToolBar(tb)
        a_reload = QAction("\u21bb  Reload", self); a_reload.triggered.connect(self._reload_web)
        a_map = QAction("Mappings", self); a_map.triggered.connect(self.open_mappings)
        a_set = QAction("Settings", self); a_set.triggered.connect(self.open_settings)
        a_act = QAction("Activity", self); a_act.triggered.connect(self.open_activity)
        tb.addAction(a_reload); tb.addSeparator()
        tb.addAction(a_map); tb.addAction(a_set); tb.addAction(a_act)
        spacer = QWidget(); spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.status_lbl = QLabel("\u25cf connecting"); self.status_lbl.setObjectName("StatusDot")
        tb.addWidget(self.status_lbl)

        self._log = []
        self._activity_view = None

        # main content = the live JAY dashboard (exact website, all features)
        if HAVE_WEBENGINE:
            self.web = QWebEngineView()
            try:
                self.web.page().featurePermissionRequested.connect(self._on_feature_permission)
            except Exception:
                pass
            self.setCentralWidget(self.web)
            self._reload_web()
        else:
            self.web = None
            fb = QLabel("Web view unavailable in this build.\nReinstall/update the app to load the dashboard.")
            fb.setObjectName("WebFallback"); fb.setAlignment(Qt.AlignCenter)
            self.setCentralWidget(fb)

        self.bridge.status.connect(self.on_status)
        self.bridge.activity.connect(self._on_activity)
        self.bridge.update_result.connect(self.on_update_result)

        self.agent.start()
        _devs = list_input_devices()
        self.bridge.activity.emit("[MIC] inputs: " + (" | ".join(_devs) if _devs else "none detected"))
        self.bridge.activity.emit(f"[MIC] using: {self._input_device() or 'system default'}")
        self.check_updates(manual=False)  # silent check on launch
        self._reconcile_ptt()
        self._reconcile_ww()

    # ── web view ──
    def _web_url(self):
        url = (self.cfg["web"].get("url", "") or "").strip()
        if not url:
            url = f"http://{self.cfg['broker'].get('host', '127.0.0.1')}:8080/"
        return url

    def _reload_web(self):
        if self.web is not None:
            self.web.setUrl(QUrl(self._web_url()))

    def _on_feature_permission(self, origin, feature):
        # auto-grant the dashboard's mic so the in-page voice button works
        try:
            self.web.page().setFeaturePermission(
                origin, feature, QWebEnginePage.PermissionPolicy.PermissionGrantedByUser)
        except Exception:
            pass

    # ── native dialogs for app-only config ──
    def _show_dialog(self, title, widget, w=780, h=580, scroll=False):
        dlg = QDialog(self); dlg.setWindowTitle(f"{APP_NAME} — {title}"); dlg.resize(w, h)
        lay = QVBoxLayout(dlg); lay.setContentsMargins(0, 0, 0, 0)
        if scroll:
            area = QScrollArea(); area.setWidgetResizable(True)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            area.setWidget(widget)
            lay.addWidget(area)
        else:
            lay.addWidget(widget)
        dlg.exec()

    def open_mappings(self):
        page = MappingsPage(self.get_mappings, self.set_mappings, self.get_global_mappings)
        self._show_dialog("Mappings", page)

    def open_settings(self):
        page = SettingsPage(self.cfg, self.apply_settings, self.apply_theme)
        page.upd_btn.clicked.connect(lambda: self.check_updates(manual=True))
        self._show_dialog("Settings", page, scroll=True)

    def open_activity(self):
        view = QPlainTextEdit(); view.setReadOnly(True)
        view.setPlainText("\n".join(self._log))
        self._activity_view = view
        self._show_dialog("Activity", view, w=720, h=460)
        self._activity_view = None

    @Slot(str)
    def _on_activity(self, line):
        self._log.append(line)
        if len(self._log) > 800:
            self._log = self._log[-800:]
        if self._activity_view is not None:
            self._activity_view.appendPlainText(line)

    # mappings shared between GUI and mqtt thread
    def get_mappings(self):
        with self._lock:
            return list(self._mappings)

    def get_global_mappings(self):
        try:
            with self.agent._global_lock:
                return list(self.agent._global)
        except Exception:
            return []

    def set_mappings(self, rows):
        with self._lock:
            self._mappings = list(rows)

    def apply_settings(self, cfg):
        self.agent.apply_config(cfg)
        self._reconcile_ptt()
        self._reconcile_ww()
        self._reload_web()

    # ── push-to-talk ──
    def _on_ptt_status(self, msg):
        self.bridge.activity.emit(f"[PTT] {msg}")

    def _on_ptt_text(self, text):
        if self.agent.publish_stt(text):
            self.bridge.activity.emit(f'[PTT] -> stt/text (source=pc): "{text}"')

    def _reconcile_ptt(self):
        enabled = self.cfg["agent"].get("ptt_enabled", "false").lower() == "true"
        # tear down any existing listener first
        if self.ptt_ctl is not None:
            try:
                self.ptt_ctl.stop()
            except Exception:
                pass
            self.ptt_ctl = None
        if not enabled:
            return
        if ptt is None:
            self.bridge.activity.emit("[PTT] unavailable in this build")
            return
        url = self.cfg["stt"].get("url", "http://100.119.255.57:8080/api/stt")
        hotkey = self.cfg["agent"].get("hotkey", "ctrl+alt+j")
        self.ptt_ctl = ptt.PushToTalk(stt_url=url, on_text=self._on_ptt_text,
                                      on_status=self._on_ptt_status, hotkey=hotkey,
                                      input_device=self._input_device())
        self.ptt_ctl.start()

    # ── wake word ──
    def _on_ww_status(self, msg):
        self.bridge.activity.emit(f"[WW] {msg}")

    def _on_ww_text(self, text):
        if self.agent.publish_stt(text):
            self.bridge.activity.emit(f'[WW] -> stt/text (source=pc): "{text}"')

    def _reconcile_ww(self):
        enabled = self.cfg["agent"].get("ww_enabled", "false").lower() == "true"
        # Always tear down first: stops the thread, closes the mic, frees the model.
        if self.ww_ctl is not None:
            try:
                self.ww_ctl.stop()
            except Exception:
                pass
            self.ww_ctl = None
        if not enabled:
            return  # nothing running -> zero CPU
        if ww is None:
            self.bridge.activity.emit("[WW] unavailable in this build")
            return
        url = self.cfg["stt"].get("url", "http://100.119.255.57:8080/api/stt")
        word = self.cfg["agent"].get("ww_word", "hey_jarvis")
        self.ww_ctl = ww.WakeWord(model=word, stt_url=url, on_wake_text=self._on_ww_text,
                                  on_status=self._on_ww_status,
                                  input_device=self._input_device())
        self.ww_ctl.start()

    def _input_device(self):
        name = (self.cfg["agent"].get("input_device", "") or "").strip()
        return name or None

    def closeEvent(self, event):
        for ctl in (self.ptt_ctl, self.ww_ctl):
            if ctl is not None:
                try:
                    ctl.stop()
                except Exception:
                    pass
        try:
            self.agent.stop()
        except Exception:
            pass
        super().closeEvent(event)

    def apply_theme(self, name):
        QApplication.instance().setStyleSheet(build_qss(name))

    @Slot(bool, str)
    def on_status(self, connected, detail):
        color = "#8fb96b" if connected else "#e07a5f"
        word = "connected" if connected else "offline"
        self.status_lbl.setText(f"<span style='color:{color}'>●</span> {word} — {detail}")


    def check_updates(self, manual=False):
        if updater is None:
            if manual:
                QMessageBox.information(self, "Updates", "Updater not available in this build.")
            return
        def work():
            try:
                latest = updater.check_latest()
                if not latest:
                    self.bridge.update_result.emit(manual, False, "", "")
                    return
                avail = updater.is_newer(latest["tag"], __version__) and bool(latest["asset_url"])
                self.bridge.update_result.emit(manual, avail, latest["tag"], latest["asset_url"] or "")
            except Exception as exc:
                self.bridge.activity.emit(f"Update check failed: {exc}")
                if manual:
                    self.bridge.update_result.emit(True, False, "", "")
        threading.Thread(target=work, daemon=True).start()

    @Slot(bool, bool, str, str)
    def on_update_result(self, manual, available, tag, asset_url):
        if available:
            ans = QMessageBox.question(
                self, "Update available",
                f"Version {tag} is available (you have {__version__}).\nDownload and install now?")
            if ans == QMessageBox.Yes:
                self._download_and_run(asset_url)
        elif manual:
            QMessageBox.information(self, "Up to date", f"You're on the latest version ({__version__}).")

    def _download_and_run(self, asset_url):
        self.bridge.activity.emit("Downloading update...")
        def work():
            try:
                path = updater.download_installer(asset_url)
                self.bridge.activity.emit("Launching installer...")
                updater.launch_installer(path)
                QApplication.quit()
            except Exception as exc:
                self.bridge.activity.emit(f"Update failed: {exc}")
        threading.Thread(target=work, daemon=True).start()


def main():
    acquire_mutex()
    # WebEngine must be configured before QApplication. The dashboard is served
    # over http on a Tailscale IP, so getUserMedia (the in-page mic) would be
    # blocked as an insecure origin — tell Chromium to treat it as secure.
    from urllib.parse import urlsplit
    try:
        _cfg = load_config()
        web_url = (_cfg["web"].get("url", "") or "http://127.0.0.1:8080/").strip()
    except Exception:
        web_url = "http://127.0.0.1:8080/"
    parts = urlsplit(web_url)
    flags = ["--no-sandbox"]
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1"):
        origin = f"http://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        flags.append(f"--unsafely-treat-insecure-origin-as-secure={origin}")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
    try:
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(persona_logo_icon())
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.apply_theme(win.cfg["ui"].get("theme", "dark"))
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
