"""Screen capture + (optional) mouse/keyboard control for the JAY PC agent.

Everything here happens in memory. A capture is grabbed, scaled, JPEG-encoded
into a BytesIO, base64'd straight into the MQTT reply and then dropped — no
file is ever written on the PC, and the reply goes out QoS 0 / not retained so
the broker never stores it either.

pc/command actions handled here:
  {"action":"screen_capture","req_id":"..","reply_topic":"pc/screen/frame/<id>",
   "max_side":1920,"quality":80,"monitor":"active"|"primary"|"all"}
  {"action":"input","input":{"type":"click","x":0.5,"y":0.3,"button":"left","double":false}}
      types: move | click | scroll (dy, dx) | type_text (text) | key (keys "ctrl+shift+t")
      x/y are 0..1 over the monitor from the LAST capture, so a model can point at
      what it saw. Input is OFF unless control_enabled = true in config.ini.

Author: sugaredhat
"""
import base64
import ctypes
import io
import sys
import threading
import time

_last_monitor = {"left": 0, "top": 0, "width": 0, "height": 0}
_lock = threading.Lock()


# ── foreground window info (Windows; harmless no-op elsewhere) ───────────────
def foreground_info():
    """(title, exe name, (l, t, r, b)) of the active window, or Nones."""
    if sys.platform != "win32":
        return None, None, None
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None, None, None
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value or None
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = None
        h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            try:
                size = wintypes.DWORD(1024)
                pbuf = ctypes.create_unicode_buffer(1024)
                if k32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                    exe = pbuf.value.replace("/", "\\").split("\\")[-1]
            finally:
                k32.CloseHandle(h)
        return title, exe, (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        return None, None, None


def _pick_monitor(sct, which, rect):
    mons = sct.monitors  # [0] = all monitors combined, [1..] = each monitor
    if which == "all" or len(mons) < 2:
        return mons[0]
    if which == "active" and rect:
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        for m in mons[1:]:
            if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                return m
    return mons[1]  # primary


def capture(max_side=1920, quality=80, monitor="active"):
    """Grab one frame in RAM. Returns a dict ready to json.dumps (jpeg_b64 inside)."""
    import mss
    from PIL import Image

    title, exe, rect = foreground_info()
    buf = io.BytesIO()
    img = None
    try:
        with mss.mss() as sct:
            mon = _pick_monitor(sct, monitor, rect)
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            del shot
        w, h = img.size
        s = min(1.0, float(max_side) / float(max(w, h)))
        if s < 1.0:
            img = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        img.save(buf, "JPEG", quality=int(quality), optimize=True)
        jpeg = buf.getvalue()
        mon_info = {k: int(mon[k]) for k in ("left", "top", "width", "height")}
        with _lock:
            _last_monitor.update(mon_info)
        return {
            "ok": True,
            "title": title or "",
            "app": exe or "",
            "width": img.size[0],
            "height": img.size[1],
            "monitor": mon_info,
            "bytes": len(jpeg),
            "jpeg_b64": base64.b64encode(jpeg).decode("ascii"),
        }
    finally:
        buf.close()
        img = None


# ── input control (gated by the caller on control_enabled) ───────────────────
_KEYMAP = {
    "ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "win": "cmd",
    "windows": "cmd", "cmd": "cmd", "super": "cmd", "enter": "enter", "return": "enter",
    "esc": "esc", "escape": "esc", "tab": "tab", "space": "space", "backspace": "backspace",
    "delete": "delete", "del": "delete", "up": "up", "down": "down", "left": "left",
    "right": "right", "home": "home", "end": "end", "pageup": "page_up", "pagedown": "page_down",
}


def _to_px(x, y):
    with _lock:
        m = dict(_last_monitor)
    if not m["width"]:  # no capture yet: treat as primary-monitor fractions
        try:
            import mss
            with mss.mss() as sct:
                p = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                m = {k: p[k] for k in ("left", "top", "width", "height")}
        except Exception:
            m = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    fx = min(max(float(x), 0.0), 1.0)
    fy = min(max(float(y), 0.0), 1.0)
    return int(m["left"] + fx * (m["width"] - 1)), int(m["top"] + fy * (m["height"] - 1))


def _key(name):
    from pynput.keyboard import Key, KeyCode
    n = _KEYMAP.get(name.lower().strip(), name.lower().strip())
    if len(n) == 1:
        return KeyCode.from_char(n)
    if n.startswith("f") and n[1:].isdigit():
        return getattr(Key, n)
    return getattr(Key, n)


def perform_input(spec):
    """Run one input action. Returns a short status string."""
    from pynput.mouse import Controller as Mouse, Button
    from pynput.keyboard import Controller as Keyboard

    t = (spec.get("type") or "").lower()
    if t in ("move", "click"):
        mouse = Mouse()
        if "x" in spec and "y" in spec:
            mouse.position = _to_px(spec["x"], spec["y"])
            time.sleep(0.05)
        if t == "move":
            return f"input: moved to {mouse.position}"
        btn = {"right": Button.right, "middle": Button.middle}.get(
            (spec.get("button") or "left").lower(), Button.left)
        mouse.click(btn, 2 if spec.get("double") else 1)
        return f"input: {'double-' if spec.get('double') else ''}clicked {btn.name} at {mouse.position}"
    if t == "scroll":
        Mouse().scroll(int(spec.get("dx", 0)), int(spec.get("dy", -3)))
        return "input: scrolled"
    if t == "type_text":
        text = str(spec.get("text") or "")[:2000]
        Keyboard().type(text)
        return f"input: typed {len(text)} chars"
    if t == "key":
        kb = Keyboard()
        keys = [_key(k) for k in str(spec.get("keys") or "").split("+") if k.strip()]
        if not keys:
            return "error: key missing keys"
        for k in keys:
            kb.press(k)
        for k in reversed(keys):
            kb.release(k)
        return f"input: pressed {spec.get('keys')}"
    return f"error: unknown input type '{t}'"
