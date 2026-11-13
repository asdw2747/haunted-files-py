#!/usr/bin/env python3
"""Haunted Files - a file browser that is also an abandoned room.

Read-only by design. It never creates, moves, or deletes anything in the
folders you browse. It only remembers what it has seen so that missing
files can linger as ghosts.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QFileDialog, QWidget

APP_NAME = "haunted-files"
MAX_FILES = 54
MAX_GHOSTS = 40
MAX_DOORS = 6
FRAME_MS = 33

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    t = clamp(t, 0.0, 1.0)
    return QColor(
        int(lerp(a.red(), b.red(), t)),
        int(lerp(a.green(), b.green(), t)),
        int(lerp(a.blue(), b.blue(), t)),
        int(lerp(a.alpha(), b.alpha(), t)),
    )


def with_alpha(c: QColor, alpha: float) -> QColor:
    out = QColor(c)
    out.setAlpha(int(clamp(alpha, 0, 255)))
    return out


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_age(seconds: float) -> str:
    days = seconds / 86400.0
    if days < 1:
        hours = seconds / 3600.0
        return f"{hours:.0f} hour{'s' if hours != 1 else ''} ago"
    if days < 60:
        return f"{days:.0f} day{'s' if days != 1 else ''} ago"
    if days < 730:
        return f"{days / 30.4:.0f} month{'s' if days / 30.4 != 1 else ''} ago"
    return f"{days / 365.25:.0f} year{'s' if days / 365.25 != 1 else ''} ago"


CATEGORY_COLORS = {
    "image": QColor(150, 170, 138),
    "audio": QColor(196, 162, 98),
    "video": QColor(112, 140, 168),
    "text": QColor(186, 174, 150),
    "code": QColor(132, 164, 146),
    "archive": QColor(158, 132, 110),
    "other": QColor(138, 138, 138),
}

CATEGORY_EXTS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff",
              ".webp", ".avif", ".heic", ".jxl", ".raw", ".dng"},
    "audio": {".flac", ".wav", ".mp3", ".ogg", ".opus", ".m4a", ".aac",
              ".aiff", ".aif", ".wv", ".dsf"},
    "video": {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".mpg",
              ".mpeg", ".ts", ".vob"},
    "text": {".txt", ".md", ".pdf", ".doc", ".docx", ".rtf", ".epub",
             ".csv", ".log"},
    "code": {".py", ".rs", ".js", ".ts", ".c", ".h", ".cpp", ".go", ".sh",
             ".toml", ".yaml", ".yml", ".json", ".html", ".css", ".sql"},
    "archive": {".zip", ".7z", ".rar", ".tar", ".gz", ".xz", ".zst",
                ".bz2", ".iso"},
}


def category_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    for cat, exts in CATEGORY_EXTS.items():
        if ext in exts:
            return cat
    return "other"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    d = root / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# the memory: what this room has seen before
# --------------------------------------------------------------------------


class Index:
    """Persistent record of files seen, keyed by absolute path."""

    def __init__(self) -> None:
        self.path = data_dir() / "index.json"
        self.entries: dict[str, dict] = {}
        self.dirty = False
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                self.entries = data["entries"]
        except (OSError, ValueError):
            self.entries = {}

    def save(self) -> None:
        if not self.dirty:
            return
        payload = {"version": 1, "entries": self.entries}
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass

    def touch(self, path: Path, size: int, mtime: float) -> None:
        key = str(path)
        now = time.time()
        rec = self.entries.get(key)
        if rec is None:
            self.entries[key] = {
                "first_seen": now,
                "last_seen": now,
                "size": size,
                "mtime": mtime,
            }
        else:
            rec["last_seen"] = now
            rec["size"] = size
            rec["mtime"] = mtime
        self.dirty = True

    def ghosts_in(self, directory: Path, present: set[str]) -> list[dict]:
        out = []
        parent = str(directory)
        for key, rec in self.entries.items():
            p = Path(key)
            if str(p.parent) != parent or p.name in present:
                continue
            if p.name.startswith("."):
                continue
            out.append({"path": key, **rec})
        return out


# --------------------------------------------------------------------------
# items
# --------------------------------------------------------------------------


@dataclass
class Item:
    path: Path
    name: str
    size: int
    mtime: float
    is_dir: bool = False
    ghost: bool = False
    last_seen: float = 0.0
    rect: QRectF = field(default_factory=QRectF)
    base_y: float = 0.0
    depth: float = 0.0
    scale: float = 1.0
    lift: float = 0.0
    phase: float = 0.0

    @property
    def category(self) -> str:
        return category_for(self.name)

    @property
    def color(self) -> QColor:
        return CATEGORY_COLORS[self.category]

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.mtime)

    def decay_color(self) -> QColor:
        """Fresh things glow pale; old things yellow, grey, then green."""
        fresh = QColor(196, 214, 202)
        months = QColor(178, 156, 112)
        years = QColor(112, 120, 104)
        days = self.age / 86400.0
        if days < 14:
            c = lerp_color(fresh, months, days / 14.0)
        elif days < 365:
            c = lerp_color(months, years, (days - 14) / 351.0)
        else:
            c = lerp_color(years, QColor(84, 92, 84), clamp((days - 365) / 1500.0, 0, 1))
        return c


# --------------------------------------------------------------------------
# the room
# --------------------------------------------------------------------------


class HauntedFiles(QWidget):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root.expanduser().resolve()
        self.current = self.root
        self.index = Index()
        self.doors: list[Item] = []
        self.files: list[Item] = []
        self.ghosts: list[Item] = []
        self.selected: Item | None = None
        self.hover: Item | None = None
        self.show_ghosts = True
        self._hits: list[tuple[QRectF, str, object]] = []
        self.mouse = QPointF(-1, -1)
        self.time = 0.0
        self.flicker = 1.0
        self.error_text = ""
        self.error_until = 0.0
        self._save_at = 0.0
        self.dust: list[dict] = []
        self._rng = random.Random(1337)

        self.serif = "DejaVu Serif"
        self.mono = "DejaVu Sans Mono"

        self.setWindowTitle("Haunted Files")
        self.resize(1180, 760)
        self.setMinimumSize(760, 520)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(False)

        self.enter_room(self.root)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(FRAME_MS)

    # ---------------------------------------------------------------- state

    def enter_room(self, path: Path) -> None:
        path = path.expanduser().resolve()
        if not path.is_dir():
            self.flash(f"'{path.name}' will not open.")
            return
        try:
            entries = list(os.scandir(path))
        except OSError as exc:
            self.flash(f"The door is stuck: {exc.strerror or 'permission denied'}.")
            return

        files: list[Item] = []
        dirs: list[Item] = []
        present: set[str] = set()
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            present.add(entry.name)
            if entry.is_dir(follow_symlinks=False):
                dirs.append(Item(Path(entry.path), entry.name, 0, st.st_mtime, is_dir=True))
            elif entry.is_file(follow_symlinks=False):
                files.append(Item(Path(entry.path), entry.name, st.st_size, st.st_mtime))
                self.index.touch(Path(entry.path), st.st_size, st.st_mtime)

        dirs.sort(key=lambda i: i.name.lower())
        files.sort(key=lambda i: i.name.lower())

        ghost_items: list[Item] = []
        if self.show_ghosts:
            for rec in self.index.ghosts_in(path, present):
                p = Path(rec["path"])
                ghost_items.append(
                    Item(
                        p,
                        p.name,
                        int(rec.get("size", 0)),
                        float(rec.get("mtime", 0.0)),
                        ghost=True,
                        last_seen=float(rec.get("last_seen", 0.0)),
                        phase=self._rng.random() * math.tau,
                    )
                )
            ghost_items.sort(key=lambda i: i.name.lower())

        self.current = path
        self.doors = dirs[:MAX_DOORS]
        self._extra_doors = max(0, len(dirs) - MAX_DOORS)
        self.files = files[:MAX_FILES]
        self._extra_files = max(0, len(files) - MAX_FILES)
        self.ghosts = ghost_items[:MAX_GHOSTS]
        self.selected = None
        self.hover = None
        self.index.save()
        self.setWindowTitle(f"Haunted Files - {path.name or str(path)}")

    def up(self) -> None:
        if self.current == self.root:
            self.flash("The way back is a wall now.")
            return
        self.enter_room(self.current.parent)

    def flash(self, message: str) -> None:
        self.error_text = message
        self.error_until = time.time() + 4.0

    def _pretty_path(self, p: Path) -> str:
        s = str(p)
        home = str(Path.home())
        if s == home:
            return "~"
        if s.startswith(home + os.sep):
            return "~" + s[len(home):]
        return s

    def choose_root(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose a room", str(self.root))
        if chosen:
            self.root = Path(chosen).resolve()
            self.enter_room(self.root)

    # ---------------------------------------------------------------- frame

    def tick(self) -> None:
        self.time += FRAME_MS / 1000.0
        # light: slow breathing plus rare stutter
        base = 0.86 + 0.07 * math.sin(self.time * 1.7) + 0.03 * math.sin(self.time * 9.3)
        if self._rng.random() < 0.018:
            base *= 0.55
        self.flicker = lerp(self.flicker, base, 0.35)

        for d in self.dust:
            d["x"] = (d["x"] + d["vx"] * 0.0006) % 1.0
            d["y"] = (d["y"] + d["vy"] * 0.0006) % 1.0

        if time.time() >= self._save_at:
            self.index.save()
            self._save_at = time.time() + 15.0
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        need = 140
        while len(self.dust) < need:
            self.dust.append(
                {
                    "x": self._rng.random(),
                    "y": self._rng.random(),
                    "vx": self._rng.uniform(-1, 1),
                    "vy": self._rng.uniform(-0.4, 0.4),
                    "r": self._rng.uniform(0.6, 2.1),
                    "a": self._rng.uniform(8, 40),
                }
            )
        super().resizeEvent(event)

    # ------------------------------------------------------------- geometry

    def _geo(self) -> dict:
        w, h = float(self.width()), float(self.height())
        cx = w * 0.5
        hy = h * 0.47
        bw = w * 0.54
        bh = h * 0.40
        back = QRectF(cx - bw / 2.0, hy - bh / 2.0, bw, bh)
        return {"w": w, "h": h, "cx": cx, "hy": hy, "back": back, "bw": bw, "bh": bh}

    def _fs(self, factor: float = 1.0) -> int:
        return max(9, int(self.height() * 0.0165 * factor))

    def _layout_items(self, geo: dict) -> None:
        items = list(self.files)
        if self.show_ghosts:
            items.extend(self.ghosts)
        if not items:
            return
        w, h = geo["w"], geo["h"]
        back: QRectF = geo["back"]
        cx = geo["cx"]

        n = len(items)
        rows = max(3, min(7, int(round(math.sqrt(n / 2.4)))))
        rows = min(rows, n)
        base = n // rows
        rem = n % rows
        counts = [base + (1 if r < rem else 0) for r in range(rows)]

        idx = 0
        for row, count in enumerate(counts):
            t = (row + 1) / rows
            depth = t
            y = lerp(back.bottom() + 24, h - 112, t ** 1.15)
            half = w * 0.47 * lerp(0.34, 0.96, t)
            spacing = (2.0 * half) / max(count, 1)
            for col in range(count):
                u = (col + 0.5) / count
                x = cx + (u - 0.5) * 2.0 * half
                item = items[idx]
                idx += 1
                scale = lerp(0.50, 1.06, t)
                size_f = 0.84 + 0.26 * clamp(math.log10(max(item.size, 1)) / 7.0, 0, 1)
                pw = min(80.0 * scale * size_f, spacing * 0.90)
                ph = pw * 0.66
                item.scale = scale
                item.depth = depth
                item.base_y = y
                item.rect = QRectF(x - pw / 2.0, y - ph, pw, ph)
        items.sort(key=lambda i: i.base_y)

    # --------------------------------------------------------------- paint

    def paintEvent(self, event) -> None:  # noqa: N802
        if self.width() < 40 or self.height() < 40:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        geo = self._geo()
        self._hits = []
        self._layout_items(geo)

        self._paint_room(painter, geo)
        self._paint_doors(painter, geo)
        self._paint_items(painter, geo)
        kind, obj = self._hit(self.mouse)
        self.hover = obj if kind == "item" else None
        self._paint_dust(painter, geo)
        self._paint_frame(painter, geo)
        self._paint_ui(painter, geo)

        painter.end()

    def _paint_room(self, p: QPainter, geo: dict) -> None:
        w, h = geo["w"], geo["h"]
        back: QRectF = geo["back"]
        cx, hy = geo["cx"], geo["hy"]
        flick = self.flicker

        bg = QLinearGradient(0, 0, 0, h)
        bg.setColorAt(0.0, QColor(7, 9, 10))
        bg.setColorAt(1.0, QColor(2, 3, 3))
        p.fillRect(QRectF(0, 0, w, h), QBrush(bg))

        # ceiling
        ceil_poly = QPolygonF(
            [QPointF(0, 0), QPointF(back.left(), back.top()),
             QPointF(back.right(), back.top()), QPointF(w, 0)]
        )
        g = QLinearGradient(0, 0, 0, back.top())
        g.setColorAt(0.0, QColor(10, 12, 13))
        g.setColorAt(1.0, QColor(4, 5, 6))
        p.setPen(QPen(QColor(24, 28, 29, 120), 1.0))
        p.setBrush(QBrush(g))
        p.drawPolygon(ceil_poly)

        # floor
        floor_poly = QPolygonF(
            [QPointF(0, h), QPointF(back.left(), back.bottom()),
             QPointF(back.right(), back.bottom()), QPointF(w, h)]
        )
        g = QLinearGradient(0, back.bottom(), 0, h)
        g.setColorAt(0.0, QColor(13, 16, 17))
        g.setColorAt(1.0, QColor(4, 5, 6))
        p.setBrush(QBrush(g))
        p.drawPolygon(floor_poly)

        # side walls
        left_poly = QPolygonF(
            [QPointF(0, 0), QPointF(back.left(), back.top()),
             QPointF(back.left(), back.bottom()), QPointF(0, h)]
        )
        right_poly = QPolygonF(
            [QPointF(w, 0), QPointF(back.right(), back.top()),
             QPointF(back.right(), back.bottom()), QPointF(w, h)]
        )
        for poly, x1, x2 in ((left_poly, 0.0, back.left()), (right_poly, w, back.right())):
            g = QLinearGradient(x1, 0, x2, 0)
            g.setColorAt(0.0, QColor(8, 10, 11))
            g.setColorAt(1.0, QColor(14, 17, 18))
            p.setBrush(QBrush(g))
            p.setPen(QPen(QColor(22, 26, 27, 90), 1.0))
            p.drawPolygon(poly)

        # back wall
        g = QLinearGradient(back.left(), back.top(), back.right(), back.bottom())
        g.setColorAt(0.0, QColor(24, 28, 29))
        g.setColorAt(0.55, QColor(16, 19, 20))
        g.setColorAt(1.0, QColor(9, 11, 12))
        p.setBrush(QBrush(g))
        p.setPen(QPen(QColor(40, 46, 47, 150), 1.5))
        p.drawRect(back)

        # faint wall panels
        p.setPen(QPen(QColor(60, 66, 66, 28), 1.0))
        for i in range(1, 9):
            x = back.left() + back.width() * i / 9.0
            p.drawLine(QPointF(x, back.top()), QPointF(x, back.bottom()))
        p.drawLine(
            QPointF(back.left(), back.top() + back.height() * 0.34),
            QPointF(back.right(), back.top() + back.height() * 0.34),
        )

        # floor perspective grid
        p.setPen(QPen(QColor(70, 84, 80, 16), 1.0))
        for i in range(1, 8):
            t = i / 8.0
            y = lerp(back.bottom(), h, t)
            x1 = lerp(back.left(), 0, t)
            x2 = lerp(back.right(), w, t)
            p.drawLine(QPointF(x1, y), QPointF(x2, y))
        for i in range(9):
            x_bottom = w * i / 8.0
            p.drawLine(QPointF(cx, back.bottom()), QPointF(x_bottom, h))

        # light
        glow = QRadialGradient(QPointF(cx, hy - geo["bh"] * 0.22), w * 0.58)
        glow.setColorAt(0.0, QColor(255, 226, 170, int(34 * flick)))
        glow.setColorAt(0.35, QColor(180, 150, 110, int(14 * flick)))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(QPen(QColor(0, 0, 0, 0)))
        p.setBrush(QBrush(glow))
        p.drawRect(QRectF(0, 0, w, h))

        bulb = QRadialGradient(QPointF(cx, hy - geo["bh"] * 0.46), 46)
        bulb.setColorAt(0.0, QColor(255, 244, 210, int(120 * flick)))
        bulb.setColorAt(0.25, QColor(255, 220, 160, int(40 * flick)))
        bulb.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(bulb))
        p.drawEllipse(QPointF(cx, hy - geo["bh"] * 0.46), 46, 46)

        # the lamp itself
        lamp_y = hy - geo["bh"] * 0.46
        p.setPen(QPen(QColor(74, 68, 56, 150), 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, 0), QPointF(cx, lamp_y - 13))
        shade = QPolygonF(
            [
                QPointF(cx - 20, lamp_y - 13),
                QPointF(cx + 20, lamp_y - 13),
                QPointF(cx + 10, lamp_y - 2),
                QPointF(cx - 10, lamp_y - 2),
            ]
        )
        p.setBrush(QBrush(QColor(40, 36, 29, 235)))
        p.setPen(QPen(QColor(96, 84, 64, 170), 1.0))
        p.drawPolygon(shade)
        p.setBrush(QBrush(QColor(255, 242, 204, int(215 * flick))))
        p.setPen(QPen(QColor(255, 232, 184, 120), 1.0))
        p.drawEllipse(QPointF(cx, lamp_y), 3.4, 4.4)

    def _paint_doors(self, p: QPainter, geo: dict) -> None:
        if not self.doors:
            return
        back: QRectF = geo["back"]
        n = len(self.doors)
        spacing = back.width() / (n + 1.0)
        door_w = min(76.0, spacing * 0.62)
        door_h = back.height() * 0.63
        bottom = back.bottom() - back.height() * 0.05
        top = bottom - door_h
        fm = QFontMetricsF(QFont(self.serif, self._fs(0.78)))

        for i, door in enumerate(self.doors):
            cx = back.left() + spacing * (i + 1)
            x = cx - door_w / 2.0
            rect = QRectF(x, top, door_w, door_h)
            hovered = rect.contains(self.mouse)

            path = QPainterPath()
            path.moveTo(x, bottom)
            path.lineTo(x, top + door_w / 2.0)
            path.arcTo(QRectF(x, top, door_w, door_w), 180, -180)
            path.lineTo(x + door_w, bottom)
            path.closeSubpath()

            g = QLinearGradient(0, top, 0, bottom)
            if hovered:
                g.setColorAt(0.0, QColor(52, 44, 34))
                g.setColorAt(1.0, QColor(24, 20, 16))
            else:
                g.setColorAt(0.0, QColor(34, 30, 25))
                g.setColorAt(1.0, QColor(15, 14, 13))
            p.setBrush(QBrush(g))
            p.setPen(QPen(QColor(92, 78, 58, 200 if hovered else 130), 1.6))
            p.drawPath(path)

            # panels
            p.setPen(QPen(QColor(12, 11, 10, 180), 1.0))
            inset = door_w * 0.16
            for frac in (0.20, 0.55):
                pr = QRectF(
                    x + inset,
                    top + door_w * 0.55 + door_h * frac,
                    door_w - inset * 2,
                    door_h * 0.24,
                )
                p.drawRoundedRect(pr, 2, 2)

            # handle
            p.setBrush(QBrush(QColor(190, 164, 108, 210 if hovered else 150)))
            p.setPen(QPen(QColor(0, 0, 0, 120), 1.0))
            p.drawEllipse(QPointF(x + door_w * 0.82, top + door_h * 0.58), 2.6, 2.6)

            # light under the door
            spill = QLinearGradient(0, bottom - 4, 0, bottom + 22)
            spill.setColorAt(0.0, QColor(255, 214, 150, int((70 if hovered else 34) * self.flicker)))
            spill.setColorAt(1.0, QColor(255, 214, 150, 0))
            p.setPen(QPen(QColor(0, 0, 0, 0)))
            p.setBrush(QBrush(spill))
            p.drawRect(QRectF(x - door_w * 0.12, bottom - 4, door_w * 1.24, 26))

            # name plate above the arch, on the wall
            label = fm.elidedText(door.name, Qt.TextElideMode.ElideRight, spacing * 0.88)
            f = QFont(self.serif, self._fs(0.70))
            p.setFont(f)
            p.setPen(QPen(QColor(212, 198, 168, 235 if hovered else 175)))
            p.drawText(
                QRectF(cx - spacing * 0.44, top - 25, spacing * 0.88, 20),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                label,
            )

            self._hits.append((rect.adjusted(-6, -6, 6, 6), "door", door))

        if self._extra_doors:
            p.setFont(QFont(self.serif, self._fs(0.7)))
            p.setPen(QPen(QColor(150, 140, 120, 150)))
            p.drawText(
                QRectF(back.right() - 90, back.bottom() + 6, 84, 20),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop),
                f"+{self._extra_doors} shut",
            )

    def _paint_items(self, p: QPainter, geo: dict) -> None:
        now = time.time()
        for item in sorted(self.files + (self.ghosts if self.show_ghosts else []),
                           key=lambda i: i.base_y):
            if item.rect.isNull():
                continue
            hovered = self.hover is item
            if hovered:
                self.hover = item
            lift = 5.0 if hovered else 0.0
            item.lift = lift
            rect = item.rect.translated(0, -lift)
            depth_a = lerp(0.52, 1.0, item.depth)
            cat = item.color

            # shadow
            p.setPen(QPen(QColor(0, 0, 0, 0)))
            p.setBrush(QBrush(QColor(0, 0, 0, int(95 * depth_a))))
            sh_w = rect.width() * 0.86
            p.drawEllipse(
                QPointF(rect.center().x(), item.base_y + 3 - lift * 0.4),
                sh_w / 2.0,
                max(3.0, rect.height() * 0.11),
            )

            if item.ghost:
                last = item.last_seen or item.mtime
                fade = clamp(math.exp(-max(0.0, now - last) / (86400.0 * 140.0)), 0.0, 1.0)
                alpha = clamp(0.10 + 0.42 * fade, 0.06, 0.5)
                bob = math.sin(self.time * 0.55 + item.phase) * 2.2
                rect = rect.translated(0, bob)
                pen = QPen(QColor(176, 188, 192, int(170 * alpha)), 1.2)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(QBrush(QColor(160, 174, 178, int(26 * alpha))))
                p.drawRoundedRect(rect, 4, 4)
                f = QFont(self.serif, max(8, int(self._fs(0.72) * item.scale)))
                p.setFont(f)
                p.setPen(QPen(QColor(190, 200, 202, int(210 * alpha))))
                label = QFontMetricsF(f).elidedText(
                    item.name, Qt.TextElideMode.ElideRight, rect.width() - 8
                )
                p.drawText(
                    QRectF(rect.left() + 4, rect.bottom() - 17, rect.width() - 8, 14),
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                    label,
                )
                self._hits.append((rect.adjusted(-3, -3, 3, 3), "item", item))
                continue

            # body
            base = item.decay_color()
            body_top = QColor(31, 33, 32)
            body_bot = QColor(14, 15, 15)
            g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            g.setColorAt(0.0, body_top)
            g.setColorAt(1.0, body_bot)
            p.setBrush(QBrush(g))
            edge = with_alpha(base, (168 if hovered else 110) * depth_a)
            p.setPen(QPen(edge, 1.6 if hovered else 1.2))
            p.drawRoundedRect(rect, 4, 4)

            # inner sheen
            p.setPen(QPen(with_alpha(base, 28 * depth_a), 1.0))
            p.drawLine(
                QPointF(rect.left() + 5, rect.top() + 5),
                QPointF(rect.right() - 5, rect.top() + 5),
            )

            # fresh things have a faint pulse
            if item.age < 86400 * 3:
                pulse = 0.5 + 0.5 * math.sin(self.time * 1.4 + item.phase)
                p.setPen(QPen(with_alpha(base, (26 + 20 * pulse) * depth_a), 1.0))
                p.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 6, 6)

            if item is self.selected:
                pulse = 0.55 + 0.45 * math.sin(self.time * 2.2)
                p.setPen(QPen(with_alpha(base, 120 + 100 * pulse), 2.2))
                p.drawRoundedRect(rect.adjusted(-4, -4, 4, 4), 7, 7)

            self._draw_glyph(p, rect, item.category, with_alpha(base, 210 * depth_a))

            # name
            if rect.width() >= 46:
                f = QFont(self.serif, max(8, int(self._fs(0.68) * item.scale)))
                p.setFont(f)
                p.setPen(QPen(with_alpha(base, (228 if hovered else 195) * depth_a)))
                label = QFontMetricsF(f).elidedText(
                    item.name, Qt.TextElideMode.ElideRight, rect.width() - 8
                )
                p.drawText(
                    QRectF(rect.left() + 4, rect.bottom() - 16, rect.width() - 8, 13),
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                    label,
                )

            # a thin line whose length hints at file size
            meter = clamp(math.log10(max(item.size, 1)) / 10.0, 0.06, 1.0)
            p.setPen(QPen(with_alpha(base, 70 * depth_a), 1.0))
            p.drawLine(
                QPointF(rect.left() + 6, rect.bottom() - 3),
                QPointF(rect.left() + 6 + (rect.width() - 12) * meter, rect.bottom() - 3),
            )

            self._hits.append((rect.adjusted(-3, -3, 3, 3), "item", item))

        if self._extra_files:
            p.setFont(QFont(self.serif, self._fs(0.7)))
            p.setPen(QPen(QColor(150, 140, 120, 150)))
            p.drawText(
                QRectF(24, self.height() - 58, 260, 18),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                f"+{self._extra_files} unlit things",
            )

    def _draw_glyph(self, p: QPainter, rect: QRectF, cat: str, color: QColor) -> None:
        s = min(rect.width(), rect.height()) * 0.30
        c = rect.center()
        top = rect.top() + rect.height() * 0.30
        p.setPen(QPen(color, 1.3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if cat == "image":
            p.drawEllipse(QPointF(c.x() - s * 0.18, top - s * 0.10), s * 0.16, s * 0.16)
            path = QPainterPath()
            path.moveTo(c.x() - s * 0.55, top + s * 0.38)
            path.lineTo(c.x() - s * 0.12, top - s * 0.05)
            path.lineTo(c.x() + s * 0.12, top + s * 0.18)
            path.lineTo(c.x() + s * 0.55, top - s * 0.12)
            p.drawPath(path)
        elif cat == "audio":
            for i in range(5):
                hgt = (0.18 + 0.16 * [1, 3, 2, 4, 1][i]) * s
                x = c.x() - s * 0.55 + i * s * 0.28
                p.drawLine(QPointF(x, top - hgt), QPointF(x, top + hgt))
        elif cat == "video":
            path = QPainterPath()
            path.moveTo(c.x() - s * 0.28, top - s * 0.38)
            path.lineTo(c.x() + s * 0.40, top)
            path.lineTo(c.x() - s * 0.28, top + s * 0.38)
            path.closeSubpath()
            p.drawPath(path)
        elif cat in ("text", "code"):
            for i in range(3):
                y = top - s * 0.26 + i * s * 0.26
                x2 = c.x() + s * (0.48 if i != 1 else 0.16)
                p.drawLine(QPointF(c.x() - s * 0.5, y), QPointF(x2, y))
        elif cat == "archive":
            p.drawRect(QRectF(c.x() - s * 0.44, top - s * 0.30, s * 0.88, s * 0.66))
            p.drawLine(QPointF(c.x() - s * 0.44, top), QPointF(c.x() + s * 0.44, top))
        else:
            p.drawEllipse(QPointF(c.x(), top), s * 0.18, s * 0.18)

    def _paint_dust(self, p: QPainter, geo: dict) -> None:
        w, h = geo["w"], geo["h"]
        p.setPen(QPen(QColor(0, 0, 0, 0)))
        for d in self.dust:
            x = d["x"] * w
            y = d["y"] * h
            p.setBrush(QBrush(QColor(220, 214, 190, int(d["a"] * self.flicker))))
            p.drawEllipse(QPointF(x, y), d["r"], d["r"])

    def _paint_frame(self, p: QPainter, geo: dict) -> None:
        w, h = geo["w"], geo["h"]
        # fog at the floor line
        fog = QLinearGradient(0, h * 0.62, 0, h)
        fog.setColorAt(0.0, QColor(10, 14, 14, 0))
        fog.setColorAt(1.0, QColor(10, 14, 14, 150))
        p.setPen(QPen(QColor(0, 0, 0, 0)))
        p.setBrush(QBrush(fog))
        p.drawRect(QRectF(0, h * 0.62, w, h * 0.38))

        # vignette
        vig = QRadialGradient(QPointF(w * 0.5, h * 0.48), max(w, h) * 0.72)
        vig.setColorAt(0.0, QColor(0, 0, 0, 0))
        vig.setColorAt(0.58, QColor(0, 0, 0, 0))
        vig.setColorAt(1.0, QColor(0, 0, 0, 232))
        p.setBrush(QBrush(vig))
        p.drawRect(QRectF(0, 0, w, h))

        # grain
        rng = random.Random(int(self.time * 24))
        for _ in range(260):
            x = rng.random() * w
            y = rng.random() * h
            a = rng.randint(4, 16)
            p.setPen(QPen(QColor(255, 255, 255, a), 1.0))
            p.drawPoint(QPointF(x, y))

    def _paint_ui(self, p: QPainter, geo: dict) -> None:
        w, h = geo["w"], geo["h"]
        now = time.time()

        # breadcrumbs
        segs: list[tuple[str, Path]] = []
        cur = self.current
        while True:
            label = "~" if cur == Path.home() else (cur.name or str(cur))
            segs.append((label, cur))
            if cur == self.root or cur.parent == cur:
                break
            cur = cur.parent
        segs.reverse()

        f = QFont(self.serif, self._fs(1.02))
        fm = QFontMetricsF(f)
        p.setFont(f)
        sep = "  /  "
        sep_w = fm.horizontalAdvance(sep)
        max_w = w * 0.56
        shown: list[tuple[str, Path]] = []
        total = 0.0
        for label, path in reversed(segs):
            seg_w = fm.horizontalAdvance(label)
            add = seg_w + (sep_w if shown else 0.0)
            if shown and total + add > max_w:
                break
            shown.append((label, path))
            total += add
        shown.reverse()
        x = 24.0
        y = 16.0
        row_h = fm.height() + 4
        if len(shown) < len(segs):
            p.setPen(QPen(QColor(140, 132, 116, 160)))
            p.drawText(QRectF(x, y, 20, row_h),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "…")
            x += 20
        for i, (label, path) in enumerate(shown):
            if i:
                p.setPen(QPen(QColor(120, 112, 98, 150)))
                p.drawText(QRectF(x, y, sep_w, row_h),
                           int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), sep)
                x += sep_w
            seg_w = fm.horizontalAdvance(label)
            hovered = QRectF(x, y, seg_w, row_h).contains(self.mouse)
            p.setPen(QPen(QColor(232, 220, 194, 240) if hovered
                          else QColor(196, 186, 164, 205)))
            p.drawText(QRectF(x, y, seg_w + 2, row_h),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), label)
            self._hits.append((QRectF(x, y, seg_w, row_h), "crumb", path))
            x += seg_w

        # hints
        hint = "esc  up     ctrl+o  open     g  ghosts     f5  look again"
        p.setFont(QFont(self.mono, max(8, self._fs(0.72))))
        p.setPen(QPen(QColor(128, 124, 112, 150)))
        p.drawText(QRectF(w - 470, 16, 446, 20),
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), hint)

        # summary
        files = len(self.files)
        ghosts = len(self.ghosts) if self.show_ghosts else 0
        doors = len(self.doors)
        summary = f"{files} things   ·   {ghosts} remembered   ·   {doors} doors"
        if self._extra_files:
            summary += f"   ·   {self._extra_files} unlit"
        p.setFont(QFont(self.serif, max(9, self._fs(0.82))))
        p.setPen(QPen(QColor(172, 164, 146, 185)))
        p.drawText(QRectF(24, h - 32, w - 48, 20),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), summary)

        if not self.files and not self.ghosts:
            p.setFont(QFont(self.serif, max(10, self._fs(1.0))))
            p.setPen(QPen(QColor(150, 144, 128, 90)))
            p.drawText(
                QRectF(w * 0.2, h * 0.68, w * 0.6, 26),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                "nothing here but doors",
            )

        # transient error / flavour
        if self.error_text and now < self.error_until:
            p.setFont(QFont(self.serif, max(9, self._fs(0.85))))
            p.setPen(QPen(QColor(196, 128, 108, 230)))
            p.drawText(QRectF(24, h - 54, w - 48, 20),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       self.error_text)

        if self.selected is not None:
            self._paint_selected(p, geo)

        if self.hover is not None and self.hover is not self.selected:
            self._paint_tooltip(p, geo, self.hover)

    def _paint_selected(self, p: QPainter, geo: dict) -> None:
        item = self.selected
        assert item is not None
        w, h = geo["w"], geo["h"]
        pw = 306.0
        px = w - pw - 24.0
        py = 64.0
        ph = 214.0
        rect = QRectF(px, py, pw, ph)

        p.setPen(QPen(QColor(150, 138, 112, 90), 1.0))
        p.setBrush(QBrush(QColor(10, 12, 12, 214)))
        p.drawRoundedRect(rect, 6, 6)

        f = QFont(self.serif, self._fs(1.02))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(QColor(222, 208, 178, 235)))
        fm = QFontMetricsF(f)
        title = fm.elidedText(item.name, Qt.TextElideMode.ElideRight, pw - 28)
        p.drawText(QRectF(px + 14, py + 12, pw - 28, 22),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)

        p.setPen(QPen(QColor(90, 84, 72, 120), 1.0))
        p.drawLine(QPointF(px + 14, py + 40), QPointF(px + pw - 14, py + 40))

        lines: list[tuple[str, str]] = []
        if item.ghost:
            last = item.last_seen or item.mtime
            lines.append(("state", "a memory"))
            lines.append(("last seen", human_age(max(0.0, time.time() - last))))
        else:
            lines.append(("kind", item.category))
            lines.append(("size", human_size(item.size)))
            lines.append(("changed", datetime.fromtimestamp(item.mtime).strftime("%Y-%m-%d %H:%M")))
            lines.append(("age", human_age(item.age)))
        lines.append(("in", self._pretty_path(item.path.parent)))

        p.setFont(QFont(self.mono, max(8, self._fs(0.72))))
        fm2 = QFontMetricsF(QFont(self.mono, max(8, self._fs(0.72))))
        yy = py + 50
        for key, value in lines:
            p.setPen(QPen(QColor(132, 126, 112, 190)))
            p.drawText(QRectF(px + 14, yy, 78, 18),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), key)
            p.setPen(QPen(QColor(202, 194, 176, 220)))
            val = fm2.elidedText(value, Qt.TextElideMode.ElideLeft, pw - 108)
            p.drawText(QRectF(px + 94, yy, pw - 108, 18),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), val)
            yy += 22

        p.setFont(QFont(self.serif, max(8, self._fs(0.72))))
        p.setPen(QPen(QColor(120, 114, 102, 150)))
        p.drawText(QRectF(px + 14, py + ph - 24, pw - 28, 16),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "read-only · nothing here can be lost")

    def _paint_tooltip(self, p: QPainter, geo: dict, item: Item) -> None:
        w, h = geo["w"], geo["h"]
        lines = [item.name]
        if item.ghost:
            last = item.last_seen or item.mtime
            lines.append("a memory")
            lines.append(human_age(max(0.0, time.time() - last)))
        else:
            lines.append(f"{item.category} · {human_size(item.size)}")
            lines.append(datetime.fromtimestamp(item.mtime).strftime("%Y-%m-%d %H:%M"))
            lines.append(human_age(item.age))
        if item.is_dir:
            lines = [item.name, "a door"]

        f = QFont(self.serif, max(8, self._fs(0.76)))
        fm = QFontMetricsF(f)
        width = max(fm.horizontalAdvance(line) for line in lines) + 20
        width = min(width, 360)
        height = fm.height() * len(lines) + 14
        x = clamp(self.mouse.x() + 16, 8, w - width - 8)
        y = clamp(self.mouse.y() + 16, 8, h - height - 8)

        p.setPen(QPen(QColor(140, 128, 104, 110), 1.0))
        p.setBrush(QBrush(QColor(8, 10, 10, 232)))
        p.drawRoundedRect(QRectF(x, y, width, height), 4, 4)
        p.setFont(f)
        yy = y + 7
        for i, line in enumerate(lines):
            p.setPen(QPen(QColor(220, 208, 182, 235) if i == 0
                          else QColor(158, 150, 134, 200)))
            txt = fm.elidedText(line, Qt.TextElideMode.ElideMiddle, width - 16)
            p.drawText(QRectF(x + 9, yy, width - 16, fm.height()),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), txt)
            yy += fm.height()

    # --------------------------------------------------------------- input

    def _hit(self, pos: QPointF):
        for rect, kind, obj in reversed(self._hits):
            if rect.contains(pos):
                return kind, obj
        return None, None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self.mouse = event.position()
        kind, _obj = self._hit(self.mouse)
        if kind in ("item", "door", "crumb"):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.mouse = QPointF(-1, -1)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        kind, obj = self._hit(event.position())
        if kind == "door" or (kind == "item" and isinstance(obj, Item) and obj.is_dir):
            self.enter_room(obj.path)
        elif kind == "crumb":
            self.enter_room(obj)
        elif kind == "item":
            self.selected = obj
        else:
            self.selected = None
        self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self.up()
        elif key == Qt.Key.Key_Home:
            self.enter_room(self.root)
        elif key == Qt.Key.Key_G:
            self.show_ghosts = not self.show_ghosts
            self.enter_room(self.current)
        elif key == Qt.Key.Key_F5:
            self.enter_room(self.current)
        elif key == Qt.Key.Key_O and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.choose_root()
        elif key == Qt.Key.Key_Q and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.index.save()
        event.accept()


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Browse a folder as a haunted room.")
    parser.add_argument("root", nargs="?", default=str(Path.home()),
                        help="folder to treat as the outermost room (default: home)")
    args = parser.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2

    app = QApplication(sys.argv)
    app.setApplicationName("Haunted Files")
    window = HauntedFiles(root)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
