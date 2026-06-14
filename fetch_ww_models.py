"""CI helper: fetch openWakeWord ONNX wake models into ./ww_models so PyInstaller
can bundle them. download_models() often lands wake models in a user cache dir
that collect_data_files() never sees, so we gather from every plausible location
and fail loudly if the default wake model is missing."""
import glob
import os
import shutil
import sys

os.makedirs("ww_models", exist_ok=True)

from openwakeword.utils import download_models
try:
    download_models(target_directory="ww_models")   # newer signature
except TypeError:
    download_models()                                # older: defaults to cache/package

import openwakeword
pkg = os.path.dirname(openwakeword.__file__)
home = os.path.expanduser("~")

candidates = set()
for base in (
    "ww_models",
    os.path.join(pkg, "resources", "models"),
    os.path.join(home, ".openwakeword"),
    os.path.join(home, ".cache", "openwakeword"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "openwakeword"),
):
    if base:
        candidates |= set(glob.glob(os.path.join(base, "**", "*.onnx"), recursive=True))

for src in candidates:
    dst = os.path.join("ww_models", os.path.basename(src))
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy(src, dst)

have = sorted(f for f in os.listdir("ww_models") if f.endswith(".onnx"))
print("ww_models contains:", have)

required = "hey_jarvis_v0.1.onnx"
if required not in have:
    print(f"ERROR: {required} not found after download", file=sys.stderr)
    sys.exit(1)
print("OK: wake models bundled")
