"""Always-on wake word for the JAY PC agent.

Listens continuously via openWakeWord; on detection, records until you stop
talking, sends the clip to the server /api/stt, and emits the transcribed text
(which app.py publishes to stt/text source=pc -- the same path as push-to-talk).

Design contract: when stop() is called the background thread exits, the mic
stream is closed, and the model object is dropped. Nothing keeps running, so a
disabled wake word costs zero CPU. Heavy imports (openwakeword, sounddevice) are
deferred into start()/_run so this module imports clean in any environment.
"""
import logging
import os
import sys
import threading
import time

import numpy as np

from ptt import frames_to_wav  # reuse the 16k mono WAV encoder

log = logging.getLogger("jay-pc-agent.ww")

SAMPLE_RATE = 16000
CHUNK = 1280  # 80 ms; openWakeWord's native frame size

# Map a friendly wake-word key to its bundled model filename.
MODEL_FILES = {
    "hey_jarvis": "hey_jarvis_v0.1.onnx",
    "alexa": "alexa_v0.1.onnx",
    "hey_mycroft": "hey_mycroft_v0.1.onnx",
    "hey_rhasspy": "hey_rhasspy_v0.1.onnx",
}


def _bundle_dir():
    """Directory where bundled resources live (PyInstaller _MEIPASS or script dir)."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def resolve_model_path(name_or_path):
    """Resolve a wake-word key or path to an actual .onnx model file.

    Order: an existing absolute path -> bundled models dir -> bare name (lets
    openWakeWord download it if the machine is online)."""
    if not name_or_path:
        name_or_path = "hey_jarvis"
    if os.path.isfile(name_or_path):
        return name_or_path
    key = name_or_path.strip().lower().replace(" ", "_")
    fname = MODEL_FILES.get(key, key if key.endswith(".onnx") else key + "_v0.1.onnx")
    bundled = os.path.join(_bundle_dir(), "ww_models", fname)
    if os.path.isfile(bundled):
        return bundled
    # openWakeWord's own resources/models dir (present via collect_data_files)
    try:
        import openwakeword
        owwdir = os.path.join(os.path.dirname(openwakeword.__file__),
                              "resources", "models", fname)
        if os.path.isfile(owwdir):
            return owwdir
    except Exception:
        pass
    return key  # last resort: openWakeWord resolves/downloads by name


def rms_int16(frame):
    """Root-mean-square of an int16 mono frame (0..~32767)."""
    if frame is None or len(frame) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame.astype(np.float32)))))


class WakeWord:
    def __init__(self, model, stt_url, on_wake_text, on_status,
                 threshold=0.5, cooldown=2.5, silence_rms=350.0,
                 quiet_ms=1200, max_capture_s=10.0):
        self.model = model                      # key or path
        self.stt_url = stt_url
        self.on_wake_text = on_wake_text         # called with transcribed text
        self.on_status = on_status               # called with status strings
        self.threshold = float(threshold)
        self.cooldown = float(cooldown)
        self.silence_rms = float(silence_rms)
        self.quiet_ms = int(quiet_ms)
        self.max_capture_s = float(max_capture_s)
        self._stop = threading.Event()
        self._thread = None
        self.running = False

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.running = True

    def stop(self):
        # Signal the loop to exit; it closes the stream and drops the model.
        self._stop.set()
        self._thread = None
        self.running = False

    def _run(self):
        try:
            import sounddevice as sd
            from openwakeword.model import Model
        except Exception as exc:
            self.on_status(f"Wake word unavailable: {exc}")
            self.running = False
            return

        model_path = resolve_model_path(self.model)
        try:
            oww = Model(wakeword_models=[model_path], inference_framework="onnx")
        except Exception as exc:
            self.on_status(f"Wake model load failed: {exc}")
            self.running = False
            return

        # warm up (first inferences are slow / noisy)
        try:
            for _ in range(8):
                oww.predict(np.zeros(CHUNK, dtype=np.int16))
        except Exception:
            pass

        self.on_status("Wake word listening")
        last_fire = 0.0
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                dtype="int16", blocksize=CHUNK) as stream:
                while not self._stop.is_set():
                    data, _ = stream.read(CHUNK)
                    frame = data.reshape(-1)
                    try:
                        preds = oww.predict(frame)
                        conf = max(preds.values()) if preds else 0.0
                    except Exception:
                        conf = 0.0
                    now = time.time()
                    if conf >= self.threshold and (now - last_fire) > self.cooldown:
                        last_fire = now
                        self.on_status("Wake word detected")
                        self._capture(stream)
                        # reset detector state so the just-spoken tail can't re-fire
                        try:
                            oww.reset()
                        except Exception:
                            pass
        except Exception as exc:
            if not self._stop.is_set():
                self.on_status(f"Wake word stopped: {exc}")
        finally:
            oww = None  # drop the model -> no residual memory/CPU
            self.running = False

    def _capture(self, stream):
        """Record from the live stream until silence, then STT-post the clip."""
        import requests
        frames = []
        start = time.time()
        quiet_since = None
        while not self._stop.is_set():
            data, _ = stream.read(CHUNK)
            frame = data.reshape(-1)
            frames.append(frame.copy())
            now = time.time()
            if rms_int16(frame) < self.silence_rms:
                if quiet_since is None:
                    quiet_since = now
                elif (now - quiet_since) * 1000.0 > self.quiet_ms:
                    break
            else:
                quiet_since = None
            if now - start > self.max_capture_s:
                break

        wav = frames_to_wav(frames)
        if not wav or len(wav) < 6000:  # ~0.2s guard
            self.on_status("(didn't catch that)")
            return
        self.on_status("Transcribing\u2026")
        try:
            resp = requests.post(self.stt_url,
                                 files={"audio": ("clip.wav", wav, "audio/wav")},
                                 timeout=30)
            text = (resp.json().get("text") or "").strip()
        except Exception as exc:
            self.on_status(f"STT failed: {exc}")
            return
        if text:
            self.on_status(f'Heard: "{text}"')
            self.on_wake_text(text)
        else:
            self.on_status("(didn't catch that)")
