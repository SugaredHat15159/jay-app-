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

from PySide6.QtCore import Qt, QObject, Signal, Slot, QTimer
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QListWidget,
    QListWidgetItem, QStackedWidget, QLabel, QTableWidget, QTableWidgetItem,
    QPushButton, QComboBox, QLineEdit, QHeaderView, QFormLayout, QPlainTextEdit,
    QMessageBox, QAbstractItemView, QCheckBox,
)

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


def resolve_object(obj: str, mappings: list):
    """Local mappings first; else treat as a domain. Returns (payload, friendly)."""
    key = norm(obj)
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
        self.mappings_ref = mappings_ref          # callable returning current list
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
        result = execute_command(payload, self.mappings_ref())
        self.bridge.activity.emit(f"{payload} -> {result}")
        self._publish_status(result)

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


# ── GUI ──────────────────────────────────────────────────────────────────────
QSS = """
QMainWindow, QWidget { background: #14110d; color: #f2ebdc;
    font-family: 'Segoe UI', sans-serif; font-size: 14px; }
#Sidebar { background: #1a1610; border-right: 1px solid #2a2319; }
#Sidebar QListWidget { background: transparent; border: none; outline: none; }
#Sidebar QListWidget::item { padding: 12px 16px; border-radius: 8px; margin: 2px 8px; color: #b3a892; }
#Sidebar QListWidget::item:selected { background: #2a2319; color: #e8a04c; }
#Sidebar QListWidget::item:hover { background: #221c15; }
#Brand { color: #e8a04c; font-size: 20px; font-weight: 700; padding: 18px 18px 8px; }
#PageTitle { font-size: 22px; font-weight: 700; }
#Muted { color: #8a8070; }
QTableWidget { background: #1a1610; border: 1px solid #2a2319; border-radius: 8px;
    gridline-color: #2a2319; selection-background-color: #2a2319; }
QHeaderView::section { background: #221c15; color: #b3a892; padding: 8px;
    border: none; border-bottom: 1px solid #2a2319; }
QLineEdit, QComboBox { background: #221c15; border: 1px solid #2a2319; border-radius: 6px;
    padding: 8px; color: #f2ebdc; }
QLineEdit:focus, QComboBox:focus { border-color: #e8a04c; }
QPushButton { background: #e8a04c; color: #1c130a; border: none; border-radius: 6px;
    padding: 9px 16px; font-weight: 600; }
QPushButton:hover { background: #f0b25e; }
QPushButton#Ghost { background: transparent; color: #b3a892; border: 1px solid #2a2319; }
QPushButton#Ghost:hover { border-color: #e07a5f; color: #e07a5f; }
QPlainTextEdit { background: #1a1610; border: 1px solid #2a2319; border-radius: 8px;
    color: #b3a892; font-family: 'Consolas', monospace; font-size: 12px; }
#StatusDot { font-size: 13px; color: #7c715d; padding: 12px 18px; }
"""


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


class MappingsPage(QWidget):
    def __init__(self, get_mappings, set_mappings):
        super().__init__()
        self.get_mappings = get_mappings
        self.set_mappings = set_mappings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24); lay.setSpacing(14)
        title = QLabel("Local Mappings"); title.setObjectName("PageTitle")
        lay.addWidget(title)
        lay.addWidget(QLabel("Spoken phrase → what to open on this PC. Local entries override the server's global list."))

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Phrase", "Type", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        lay.addWidget(self.table)

        btns = QHBoxLayout()
        add = QPushButton("Add"); add.clicked.connect(self.add_row)
        rem = QPushButton("Delete Selected"); rem.setObjectName("Ghost"); rem.clicked.connect(self.del_row)
        save = QPushButton("Save"); save.clicked.connect(self.save)
        btns.addWidget(add); btns.addWidget(rem); btns.addStretch(1); btns.addWidget(save)
        lay.addLayout(btns)
        self.reload()

    def _type_combo(self, current="url"):
        c = QComboBox(); c.addItems(["url", "app", "script"])
        c.setCurrentText(current if current in ("url", "app", "script") else "url")
        return c

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
            combo = self.table.cellWidget(r, 1)
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
    def __init__(self, cfg, on_save):
        super().__init__()
        self.cfg = cfg; self.on_save = on_save
        lay = QVBoxLayout(self); lay.setContentsMargins(28, 24, 28, 24); lay.setSpacing(14)
        title = QLabel("Settings"); title.setObjectName("PageTitle"); lay.addWidget(title)
        form = QFormLayout(); form.setSpacing(12)
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
        self.ww_enabled = QCheckBox("Enable wake word (always-on listening)")
        self.ww_enabled.setChecked(agent.get("ww_enabled", "false").lower() == "true")
        self.ww_word = QComboBox()
        self.ww_word.addItems(["hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy"])
        _w = agent.get("ww_word", "hey_jarvis")
        if self.ww_word.findText(_w) >= 0:
            self.ww_word.setCurrentText(_w)
        form.addRow("Push-to-talk", self.ptt_enabled)
        form.addRow("Hold-to-talk hotkey", self.hotkey)
        form.addRow("Wake word", self.ww_enabled)
        form.addRow("Wake phrase", self.ww_word)
        form.addRow("STT server URL", self.stt_url)
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
        if "stt" not in self.cfg:
            self.cfg["stt"] = {}
        self.cfg["stt"]["url"] = self.stt_url.text().strip() or "http://100.119.255.57:8080/api/stt"
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


class TimersPage(QWidget):
    """Live view of active timers, fed by the retained skill/timer/state topic.

    Countdown ticks locally off a monotonic clock so it stays smooth between
    state messages and is immune to clock/timezone skew vs. the server.
    """
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self); lay.setContentsMargins(28, 24, 28, 24); lay.setSpacing(14)
        title = QLabel("Timers"); title.setObjectName("PageTitle"); lay.addWidget(title)
        self.empty = QLabel("No active timers"); self.empty.setObjectName("Muted")
        lay.addWidget(self.empty)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Remaining", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        lay.addWidget(self.table, 1)
        self._timers = []  # {name, status, base_remaining, base_mono}
        self._tick = QTimer(self); self._tick.setInterval(1000)
        self._tick.timeout.connect(self._render); self._tick.start()

    @Slot(str)
    def update_state(self, payload):
        try:
            data = json.loads(payload or "{}")
        except Exception:
            data = {}
        now = time.monotonic()
        self._timers = [{
            "name": t.get("name") or t.get("duration_text") or "Timer",
            "status": t.get("status") or "scheduled",
            "base_remaining": int(t.get("remaining_seconds") or 0),
            "base_mono": now,
        } for t in (data.get("timers") or [])]
        self._render()

    @staticmethod
    def _fmt(secs):
        secs = max(0, int(round(secs)))
        h, rem = divmod(secs, 3600); m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _render(self):
        now = time.monotonic()
        rows = self._timers
        self.empty.setVisible(not rows)
        self.table.setVisible(bool(rows))
        self.table.setRowCount(len(rows))
        for i, t in enumerate(rows):
            if t["status"] == "ringing":
                remaining = "ringing"
            else:
                remaining = self._fmt(t["base_remaining"] - (now - t["base_mono"]))
            self.table.setItem(i, 0, QTableWidgetItem(t["name"]))
            self.table.setItem(i, 1, QTableWidgetItem(remaining))
            self.table.setItem(i, 2, QTableWidgetItem(t["status"]))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(820, 560)
        self.cfg = load_config()
        self._mappings = load_mappings()
        self._lock = threading.Lock()

        self.bridge = Bridge()
        self.agent = AgentMQTT(self.cfg, self.bridge, self.get_mappings)
        self.ptt_ctl = None
        self.ww_ctl = None

        central = QWidget(); self.setCentralWidget(central)
        h = QHBoxLayout(central); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(0)

        # sidebar
        side = QWidget(); side.setObjectName("Sidebar"); side.setFixedWidth(200)
        sv = QVBoxLayout(side); sv.setContentsMargins(0, 0, 0, 0); sv.setSpacing(0)
        brand = QLabel("JAY"); brand.setObjectName("Brand"); sv.addWidget(brand)
        self.nav = QListWidget()
        for name in ["Mappings", "Timers", "Settings", "Activity"]:
            QListWidgetItem(name, self.nav)
        self.nav.setCurrentRow(0)
        sv.addWidget(self.nav, 1)
        self.status_lbl = QLabel("● connecting"); self.status_lbl.setObjectName("StatusDot")
        sv.addWidget(self.status_lbl)
        h.addWidget(side)

        # pages
        self.stack = QStackedWidget()
        self.mappings_page = MappingsPage(self.get_mappings, self.set_mappings)
        self.timers_page = TimersPage()
        self.settings_page = SettingsPage(self.cfg, self.apply_settings)
        self.activity_page = ActivityPage()
        self.stack.addWidget(self.mappings_page)
        self.stack.addWidget(self.timers_page)
        self.stack.addWidget(self.settings_page)
        self.stack.addWidget(self.activity_page)
        h.addWidget(self.stack, 1)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.bridge.status.connect(self.on_status)
        self.bridge.activity.connect(self.activity_page.append)
        self.bridge.update_result.connect(self.on_update_result)
        self.bridge.skill_state.connect(self._on_skill_state)
        self.settings_page.upd_btn.clicked.connect(lambda: self.check_updates(manual=True))

        self.agent.start()
        self.check_updates(manual=False)  # silent check on launch
        self._reconcile_ptt()
        self._reconcile_ww()

    # mappings shared between GUI and mqtt thread
    def get_mappings(self):
        with self._lock:
            return list(self._mappings)

    def set_mappings(self, rows):
        with self._lock:
            self._mappings = list(rows)

    def apply_settings(self, cfg):
        self.agent.apply_config(cfg)
        self._reconcile_ptt()
        self._reconcile_ww()

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
                                      on_status=self._on_ptt_status, hotkey=hotkey)
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
                                  on_status=self._on_ww_status)
        self.ww_ctl.start()

    def closeEvent(self, event):
        if self.ptt_ctl is not None:
            try:
                self.ptt_ctl.stop()
            except Exception:
                pass
        if self.ww_ctl is not None:
            try:
                self.ww_ctl.stop()
            except Exception:
                pass
        super().closeEvent(event)

    @Slot(bool, str)
    def on_status(self, connected, detail):
        color = "#8fb96b" if connected else "#e07a5f"
        word = "connected" if connected else "offline"
        self.status_lbl.setText(f"<span style='color:{color}'>●</span> {word} — {detail}")

    @Slot(str, str)
    def _on_skill_state(self, skill, payload):
        """Route a retained skill/<x>/state message to its dashboard page."""
        if skill == "timer":
            self.timers_page.update_state(payload)


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
        self.activity_page.append("Downloading update...")
        def work():
            try:
                path = updater.download_installer(asset_url)
                self.bridge.activity.emit("Launching installer...")
                updater.launch_installer(path)
                QApplication.quit()
            except Exception as exc:
                self.bridge.activity.emit(f"Update failed: {exc}")
        threading.Thread(target=work, daemon=True).start()

    def closeEvent(self, event):
        self.agent.stop()
        super().closeEvent(event)


def main():
    acquire_mutex()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
