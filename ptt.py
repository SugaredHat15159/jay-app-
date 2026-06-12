"""Push-to-talk: hold a hotkey, capture mic, send to server STT, return text.

Heavy/native imports (pynput, sounddevice) are deferred into methods so this
module imports cleanly in any environment. Pure logic (hotkey parsing, key
normalization, WAV encoding) lives at module scope and is unit-testable.
"""
import io
import logging
import threading
import wave

import numpy as np

log = logging.getLogger("jay-pc-agent.ptt")

SAMPLE_RATE = 16000


# ── pure, testable helpers ───────────────────────────────────────────────────
def normalize_key_name(name):
    """Collapse left/right modifier variants to a canonical token."""
    name = (name or "").lower()
    if name.startswith("ctrl"):
        return "ctrl"
    if name.startswith("alt"):
        return "alt"
    if name.startswith("shift"):
        return "shift"
    if name.startswith("cmd") or name.startswith("win"):
        return "cmd"
    return name


def normalize_keycode(char, vk):
    """Normalize a pynput KeyCode to a token.

    With Ctrl/Alt held, Windows often delivers char=None or a control char, so
    fall back to the virtual-key code for letters (A-Z=65-90) and digits (0-9=48-57).
    """
    if char and char.isalnum():
        return char.lower()
    if vk is not None and (65 <= vk <= 90 or 48 <= vk <= 57):
        return chr(vk).lower()
    if char:
        return char.lower()
    return ("vk%s" % vk) if vk is not None else "?"


def parse_hotkey(spec):
    """'ctrl+alt+j' -> {'ctrl','alt','j'}."""
    return {normalize_key_name(p.strip()) for p in (spec or "").split("+") if p.strip()}


def combo_satisfied(pressed, target):
    """True when every key in target is currently pressed."""
    return bool(target) and target.issubset(pressed)


def frames_to_wav(frames, sample_rate=SAMPLE_RATE):
    """frames: list of int16 numpy arrays (mono) -> WAV bytes (or None)."""
    if not frames:
        return None
    audio = np.concatenate(frames).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


# ── the controller ───────────────────────────────────────────────────────────
class PushToTalk:
    def __init__(self, stt_url, on_text, on_status, hotkey="ctrl+alt+j"):
        self.stt_url = stt_url
        self.on_text = on_text          # called with transcribed text
        self.on_status = on_status      # called with status strings (for Activity log)
        self.hotkey_spec = hotkey
        self.hotkey = parse_hotkey(hotkey)
        self._pressed = set()
        self._recording = False
        self._frames = []
        self._stream = None
        self._listener = None
        self._lock = threading.Lock()
        self.running = False

    # key normalization for a pynput key object
    def _norm(self, key):
        from pynput import keyboard
        if isinstance(key, keyboard.Key):
            return normalize_key_name(key.name)
        # KeyCode: prefer char, fall back to virtual-key code (modifiers held -> char often None)
        return normalize_keycode(getattr(key, "char", None), getattr(key, "vk", None))

    def start(self):
        if self.running:
            return
        try:
            from pynput import keyboard
        except Exception as exc:
            self.on_status(f"Push-to-talk unavailable: {exc}")
            return
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()
        self.running = True
        self.on_status(f"Push-to-talk ready (hold {self.hotkey_spec})")

    def stop(self):
        self.running = False
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        self._stop_recording(discard=True)

    def set_hotkey(self, spec):
        self.hotkey_spec = spec
        self.hotkey = parse_hotkey(spec)

    def _on_press(self, key):
        self._pressed.add(self._norm(key))
        if not self._recording and combo_satisfied(self._pressed, self.hotkey):
            self._start_recording()

    def _on_release(self, key):
        self._pressed.discard(self._norm(key))
        if self._recording and not combo_satisfied(self._pressed, self.hotkey):
            self._stop_recording()

    def _start_recording(self):
        try:
            import sounddevice as sd
        except Exception as exc:
            self.on_status(f"Mic unavailable: {exc}")
            return
        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._recording = True

        def cb(indata, frames, time_info, status):
            self._frames.append(indata.copy().reshape(-1))

        try:
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                          dtype="int16", callback=cb)
            self._stream.start()
            self.on_status("Listening\u2026 (push-to-talk)")
        except Exception as exc:
            self._recording = False
            self.on_status(f"Mic error: {exc}")

    def _stop_recording(self, discard=False):
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            frames = self._frames
            self._frames = []
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if discard:
            return
        threading.Thread(target=self._process, args=(frames,), daemon=True).start()

    def _process(self, frames):
        wav = frames_to_wav(frames)
        if not wav or len(wav) < 4000:   # ~0.1s of 16k mono guard
            self.on_status("(too short \u2014 hold a moment longer)")
            return
        self.on_status("Transcribing\u2026")
        try:
            import requests
            resp = requests.post(self.stt_url,
                                 files={"audio": ("clip.wav", wav, "audio/wav")},
                                 timeout=30)
            data = resp.json()
            text = (data.get("text") or "").strip()
        except Exception as exc:
            self.on_status(f"STT failed: {exc}")
            return
        if text:
            self.on_status(f'Heard: "{text}"')
            self.on_text(text)
        else:
            self.on_status("(didn't catch that)")