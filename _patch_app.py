#!/usr/bin/env python3
"""jay-pc-agent app.py fixes:
1. resolve_object: strip a trailing "on <device>" the server didn't (e.g. "steam on laptop"
   -> "steam"), so it matches your mapping instead of falling back to steamonlaptop.com.
2. MappingsPage: add a read-only "Global (shared) mappings" table (fed from the agent's
   live skill/globalmap/state), so website-managed globals are visible in the app.
Idempotent + transactional. Run from the repo root. Author: Alex (SugaredHat)."""
import ast, os, sys

APP = sys.argv[1] if len(sys.argv) > 1 else "app.py"


def repl(src, old, new, where):
    n = src.count(old)
    assert n == 1, f"[{where}] anchor not unique (found {n}): {old[:50]!r}"
    return src.replace(old, new)


# ---- 1. target-suffix strip ----
NORM_OLD = (
    'def norm(s: str) -> str:\n'
    '    return " ".join((s or "").lower().split())\n'
)
NORM_NEW = NORM_OLD + (
    '\n'
    '_TARGET_SUFFIXES = (\n'
    '    " on my computer", " on the computer", " on this computer", " on computer",\n'
    '    " on my laptop", " on the laptop", " on this laptop", " on laptop",\n'
    '    " on my pc", " on the pc", " on this pc", " on pc",\n'
    '    " on my desktop", " on the desktop", " on desktop",\n'
    '    " on my machine", " on the machine", " on machine",\n'
    ')\n'
    '\n'
    'def strip_target(key: str) -> str:\n'
    '    """Drop a trailing \\"on <device>\\" phrase the server may not have removed."""\n'
    '    k = key\n'
    '    for suf in _TARGET_SUFFIXES:\n'
    '        if k.endswith(suf):\n'
    '            return k[: -len(suf)].strip()\n'
    '    return k\n'
)

KEY_OLD = "    key = norm(obj)\n"
KEY_NEW = "    key = strip_target(norm(obj))\n"

# ---- 2. global mappings view ----
SIG_OLD = (
    "    def __init__(self, get_mappings, set_mappings):\n"
    "        super().__init__()\n"
    "        self.get_mappings = get_mappings\n"
    "        self.set_mappings = set_mappings\n"
)
SIG_NEW = (
    "    def __init__(self, get_mappings, set_mappings, get_globals=None):\n"
    "        super().__init__()\n"
    "        self.get_mappings = get_mappings\n"
    "        self.set_mappings = set_mappings\n"
    "        self.get_globals = get_globals or (lambda: [])\n"
)

GTABLE_OLD = "        lay.addLayout(btns)\n        self.reload()\n"
GTABLE_NEW = (
    "        lay.addLayout(btns)\n"
    "\n"
    "        gtitle = QLabel(\"Global (shared) mappings\"); gtitle.setObjectName(\"PageTitle\")\n"
    "        lay.addWidget(gtitle)\n"
    "        lay.addWidget(QLabel(\"Read-only \\u2014 managed from the JAY website. Local entries above override these.\"))\n"
    "        self.gtable = QTableWidget(0, 3)\n"
    "        self.gtable.setHorizontalHeaderLabels([\"Phrase\", \"Type\", \"Value\"])\n"
    "        self.gtable.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)\n"
    "        self.gtable.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)\n"
    "        self.gtable.setColumnWidth(1, 140)\n"
    "        self.gtable.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)\n"
    "        self.gtable.verticalHeader().setVisible(False)\n"
    "        self.gtable.verticalHeader().setDefaultSectionSize(40)\n"
    "        self.gtable.setEditTriggers(QAbstractItemView.NoEditTriggers)\n"
    "        self.gtable.setSelectionMode(QAbstractItemView.NoSelection)\n"
    "        lay.addWidget(self.gtable)\n"
    "        _gm = list(self.get_globals() or [])\n"
    "        for m in _gm:\n"
    "            r = self.gtable.rowCount(); self.gtable.insertRow(r)\n"
    "            self.gtable.setItem(r, 0, QTableWidgetItem(m.get(\"phrase\", \"\")))\n"
    "            self.gtable.setItem(r, 1, QTableWidgetItem((m.get(\"kind\") or \"url\")))\n"
    "            self.gtable.setItem(r, 2, QTableWidgetItem(m.get(\"value\", \"\")))\n"
    "        if not _gm:\n"
    "            self.gtable.insertRow(0)\n"
    "            self.gtable.setItem(0, 0, QTableWidgetItem(\"(none yet)\"))\n"
    "        self.reload()\n"
)

OPEN_OLD = "        page = MappingsPage(self.get_mappings, self.set_mappings)\n"
OPEN_NEW = "        page = MappingsPage(self.get_mappings, self.set_mappings, self.get_global_mappings)\n"

GETTER_OLD = (
    "    def get_mappings(self):\n"
    "        with self._lock:\n"
    "            return list(self._mappings)\n"
)
GETTER_NEW = GETTER_OLD + (
    "\n"
    "    def get_global_mappings(self):\n"
    "        try:\n"
    "            with self.agent._global_lock:\n"
    "                return list(self.agent._global)\n"
    "        except Exception:\n"
    "            return []\n"
)


def main():
    src = open(APP, encoding="utf-8").read()
    changed = False

    if "strip_target(" not in src:
        src = repl(src, NORM_OLD, NORM_NEW, "norm.helper")
        src = repl(src, KEY_OLD, KEY_NEW, "resolve.key")
        changed = True
    else:
        print("skip: target-strip already present")

    if "self.gtable" not in src:
        src = repl(src, SIG_OLD, SIG_NEW, "page.sig")
        src = repl(src, GTABLE_OLD, GTABLE_NEW, "page.gtable")
        src = repl(src, OPEN_OLD, OPEN_NEW, "open_mappings")
        src = repl(src, GETTER_OLD, GETTER_NEW, "get_globals")
        changed = True
    else:
        print("skip: global view already present")

    if changed:
        ast.parse(src)
        open(APP, "w", encoding="utf-8").write(src)
        print(f"patched: {APP}")
    else:
        print("nothing to do (already patched)")


if __name__ == "__main__":
    main()
