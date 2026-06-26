#!/usr/bin/env python3
"""jay-pc-agent: dual-persona app icon. Paints a rounded tile split orange|blue
with 'J' on the orange (Jay) half and 'N' on the blue (Nova) half, and sets it as
the application/taskbar icon. No binary asset — drawn at runtime with QPainter.
Idempotent + transactional. Run from repo root. Author: Alex (SugaredHat)."""
import ast, sys

APP = sys.argv[1] if len(sys.argv) > 1 else "app.py"
JAY_ORANGE = "#ff7a1a"
NOVA_BLUE = "#2f6bff"


def repl(src, old, new, where):
    n = src.count(old)
    assert n == 1, f"[{where}] anchor not unique (found {n}): {old[:50]!r}"
    return src.replace(old, new)


DOT_TAIL = (
    "    p.drawEllipse(3, 3, 10, 10)\n"
    "    p.end()\n"
    "    return QIcon(pm)\n"
)

LOGO_FN = (
    "\n\n"
    "def persona_logo_icon(size: int = 64) -> QIcon:\n"
    "    \"\"\"Dual-persona mark: rounded tile, left half orange 'J' (Jay),\n"
    "    right half blue 'N' (Nova). Used as the app / taskbar icon.\"\"\"\n"
    "    pm = QPixmap(size, size)\n"
    "    pm.fill(Qt.transparent)\n"
    "    p = QPainter(pm)\n"
    "    p.setRenderHint(QPainter.Antialiasing)\n"
    "    p.setPen(Qt.NoPen)\n"
    "    radius = size * 0.22\n"
    "    # whole tile orange, then paint the right half blue (clipped)\n"
    "    p.setBrush(QColor(\"" + JAY_ORANGE + "\"))\n"
    "    p.drawRoundedRect(0, 0, size, size, radius, radius)\n"
    "    p.setClipRect(size // 2, 0, size - size // 2, size)\n"
    "    p.setBrush(QColor(\"" + NOVA_BLUE + "\"))\n"
    "    p.drawRoundedRect(0, 0, size, size, radius, radius)\n"
    "    p.setClipping(False)\n"
    "    # letters: J on the orange half, N on the blue half\n"
    "    f = QFont(\"Segoe UI\", int(size * 0.40))\n"
    "    f.setBold(True)\n"
    "    p.setFont(f)\n"
    "    p.setPen(QColor(\"#ffffff\"))\n"
    "    p.drawText(QRectF(0, 0, size / 2, size), Qt.AlignCenter, \"J\")\n"
    "    p.drawText(QRectF(size / 2, 0, size / 2, size), Qt.AlignCenter, \"N\")\n"
    "    p.end()\n"
    "    return QIcon(pm)\n"
)

ICON_OLD = "    app.setStyleSheet(QSS)\n"
ICON_NEW = "    app.setWindowIcon(persona_logo_icon())\n    app.setStyleSheet(QSS)\n"


def main():
    src = open(APP, encoding="utf-8").read()
    if "def persona_logo_icon" in src:
        print("nothing to do (logo already present)")
        return
    src = repl(src, DOT_TAIL, DOT_TAIL + LOGO_FN, "logo.fn")
    src = repl(src, ICON_OLD, ICON_NEW, "logo.seticon")
    ast.parse(src)
    open(APP, "w", encoding="utf-8").write(src)
    print(f"patched: {APP}")


if __name__ == "__main__":
    main()
