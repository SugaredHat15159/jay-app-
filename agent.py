"""JAY PC Agent — tray app that executes commands from the JAY MQTT broker.

Subscribes to pc/command and executes:
  {"action": "open_url",  "url": "https://youtube.com"}
  {"action": "open_app",  "app": "notepad"}
  {"action": "ping"}   -> publishes pc/status pong

Tray icon has Enable/Disable toggle and Quit.
Config read from config.ini (never committed).
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

# ── pystray / PIL are optional at import time so we can test the core
#    logic without a display.  The tray is initialised in main().
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("jay-pc-agent")

# ── Config ──────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.ini"

def load_config():
    if not CONFIG_PATH.exists():
        log.error("config.ini not found. Copy config.ini.example -> config.ini and fill in your password.")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    return cfg

# ── Command execution ────────────────────────────────────────────────────────
def execute_command(payload: dict) -> str:
    """Execute a pc/command payload. Returns a short status string."""
    action = (payload.get("action") or "").strip().lower()

    if action == "open_url":
        url = (payload.get("url") or "").strip()
        if not url:
            return "error: open_url missing url"
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        log.info("Opening URL: %s", url)
        webbrowser.open(url)
        return f"opened url: {url}"

    if action == "open_app":
        app = (payload.get("app") or "").strip()
        if not app:
            return "error: open_app missing app"
        log.info("Opening app: %s", app)
        try:
            if sys.platform == "win32":
                os.startfile(app)  # works for app names, file paths, URIs
            else:
                subprocess.Popen(["xdg-open", app])
            return f"opened app: {app}"
        except Exception as exc:
            log.warning("open_app failed, trying subprocess: %s", exc)
            try:
                subprocess.Popen(app, shell=True)
                return f"opened app (shell): {app}"
            except Exception as exc2:
                return f"error: {exc2}"

    if action == "ping":
        return "pong"

    return f"error: unknown action '{action}'"


# ── MQTT client ──────────────────────────────────────────────────────────────
class AgentMQTT:
    def __init__(self, cfg: configparser.ConfigParser):
        self.host = cfg["broker"]["host"]
        self.port = int(cfg["broker"]["port"])
        self.username = cfg["broker"]["username"]
        self.password = cfg["broker"]["password"]
        self.client_id = cfg.get("agent", "client_id", fallback="jay-pc-agent")
        self.enabled = True
        self._connected = False

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
        )
        self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("Connected to JAY broker at %s:%s", self.host, self.port)
            self._connected = True
            client.subscribe("pc/command", qos=1)
            log.info("Subscribed to pc/command")
            self._publish_status("online")
        else:
            log.warning("Connect failed: reason_code=%s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected = False
        log.info("Disconnected (reason=%s) — will reconnect", reason_code)

    def _on_message(self, client, userdata, msg):
        if not self.enabled:
            log.debug("Agent disabled — ignoring message on %s", msg.topic)
            return
        if msg.topic != "pc/command":
            return
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            log.warning("Bad JSON on pc/command: %r", msg.payload)
            return
        log.info("pc/command: %s", payload)
        result = execute_command(payload)
        log.info("Result: %s", result)
        self._publish_status(result)

    def _publish_status(self, status: str):
        try:
            self.client.publish(
                "pc/status",
                json.dumps({"client_id": self.client_id, "status": status}),
                qos=1,
            )
        except Exception as exc:
            log.debug("publish status failed: %s", exc)

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        self._publish_status("enabled" if enabled else "disabled")
        log.info("Agent %s", "enabled" if enabled else "disabled")

    def start(self):
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()

    def stop(self):
        self._publish_status("offline")
        self.client.loop_stop()
        self.client.disconnect()


# ── Tray icon ────────────────────────────────────────────────────────────────
def make_icon(enabled: bool) -> "Image.Image":
    """Draw a simple coloured circle as the tray icon."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colour = (80, 200, 120) if enabled else (160, 160, 160)
    draw.ellipse([4, 4, size - 4, size - 4], fill=colour)
    return img


def run_tray(agent: AgentMQTT):
    icon_ref = [None]  # mutable container so callbacks can access it

    def toggle(icon, item):
        agent.enabled = not agent.enabled
        agent._publish_status("enabled" if agent.enabled else "disabled")
        icon.icon = make_icon(agent.enabled)
        icon.menu = build_menu()
        log.info("Toggled: agent %s", "enabled" if agent.enabled else "disabled")

    def quit_app(icon, item):
        log.info("Quitting JAY PC Agent")
        agent.stop()
        icon.stop()

    def build_menu():
        label = "✓ Enabled" if agent.enabled else "  Disabled"
        return pystray.Menu(
            pystray.MenuItem(label, toggle),
            pystray.MenuItem("Quit", quit_app),
        )

    icon = pystray.Icon(
        "jay-pc-agent",
        make_icon(agent.enabled),
        "JAY PC Agent",
        menu=build_menu(),
    )
    icon_ref[0] = icon
    icon.run()  # blocks until quit_app calls icon.stop()


# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    cfg = load_config()
    agent = AgentMQTT(cfg)
    agent.start()

    if HAS_TRAY:
        run_tray(agent)   # blocks until user quits from tray
    else:
        log.warning("pystray/Pillow not installed — running headless (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    agent.stop()
    log.info("Bye.")


if __name__ == "__main__":
    main()
