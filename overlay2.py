"""
PoE2 ES Overlay
Version: 2.0

A desktop overlay for Path of Exile 2 that reads Energy Shield values from the
game window with OCR and renders a live ES fill overlay on the orb.

Features:
- Tracks the PoE2 window and positions the overlay automatically
- Uses OCR to read current/total ES from the HUD
- Draws a live ES fill effect over the orb
- Optional globe text and edge outline
- Built-in tuning mode for OCR and orb regions
- Saves tuning values to config.ini

Status:
- Version 1.0 baseline saved before packaging/portability work
- Current implementation targets Linux/X11

Runtime dependencies:
- Python 3
- PyQt5
- Pillow
- pytesseract
- python-xlib
- tesseract OCR engine
"""

import sys
import re
import configparser
from Xlib import X, display, error
from pathlib import Path
from collections import deque

import pytesseract
from PIL import Image, ImageOps, ImageFilter

from PyQt5.QtCore import Qt, QTimer, QRectF, QEvent
from PyQt5.QtGui import (
    QPainter,
    QColor,
    QFont,
    QPainterPath,
    QLinearGradient,
    QBrush,
    QPen,
    QIcon,
    QPixmap,
    QImage,
)
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QDialog,
    QSystemTrayIcon,
    QMenu,
    QAction,
)


GAME_WINDOW_NAME = "Path of Exile 2"
GAME_WINDOW_ID = None
CONFIG_PATH = Path.home() / ".config" / "poe2-es-overlay" / "config.ini"

X_DISPLAY = display.Display()
X_ROOT = X_DISPLAY.screen().root
NET_ACTIVE_WINDOW = X_DISPLAY.intern_atom("_NET_ACTIVE_WINDOW")
NET_WM_NAME = X_DISPLAY.intern_atom("_NET_WM_NAME")
WM_NAME = X_DISPLAY.intern_atom("WM_NAME")

DEFAULT_OCR_BOX = (149, 1115, 129, 32)
DEFAULT_ORB_BOX = (52, 1183, 229, 235)
DEFAULT_REFERENCE_WIDTH = 2560
DEFAULT_REFERENCE_HEIGHT = 1440

DEFAULT_CONFIG = {
    "overlay": {
        "reference_width": str(DEFAULT_REFERENCE_WIDTH),
        "reference_height": str(DEFAULT_REFERENCE_HEIGHT),
        "ocr_x": str(DEFAULT_OCR_BOX[0]),
        "ocr_y": str(DEFAULT_OCR_BOX[1]),
        "ocr_w": str(DEFAULT_OCR_BOX[2]),
        "ocr_h": str(DEFAULT_OCR_BOX[3]),
        "orb_x": str(DEFAULT_ORB_BOX[0]),
        "orb_y": str(DEFAULT_ORB_BOX[1]),
        "orb_w": str(DEFAULT_ORB_BOX[2]),
        "orb_h": str(DEFAULT_ORB_BOX[3]),
        "show_text": "false",
        "show_edge": "false",
    }
}


def load_config():
    parser = configparser.ConfigParser()

    if CONFIG_PATH.exists():
        parser.read(CONFIG_PATH)

    changed = False
    for section, values in DEFAULT_CONFIG.items():
        if section not in parser:
            parser[section] = {}
            changed = True
        for key, value in values.items():
            if key not in parser[section]:
                parser[section][key] = value
                changed = True

    if changed or not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open("w") as f:
            parser.write(f)

    return parser


def save_config(parser):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w") as f:
        parser.write(f)


CONFIG = load_config()

REFERENCE_WIDTH = CONFIG.getint("overlay", "reference_width")
REFERENCE_HEIGHT = CONFIG.getint("overlay", "reference_height")

OCR_BOX = (
    CONFIG.getint("overlay", "ocr_x"),
    CONFIG.getint("overlay", "ocr_y"),
    CONFIG.getint("overlay", "ocr_w"),
    CONFIG.getint("overlay", "ocr_h"),
)

ORB_BOX = (
    CONFIG.getint("overlay", "orb_x"),
    CONFIG.getint("overlay", "orb_y"),
    CONFIG.getint("overlay", "orb_w"),
    CONFIG.getint("overlay", "orb_h"),
)


def get_window_name(win):
    try:
        prop = win.get_full_property(NET_WM_NAME, X.AnyPropertyType)
        if prop and prop.value:
            value = prop.value
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="ignore")
            return str(value)

        prop = win.get_full_property(WM_NAME, X.AnyPropertyType)
        if prop and prop.value:
            value = prop.value
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="ignore")
            return str(value)
    except error.XError:
        return None

    return None


def walk_windows(win):
    yield win
    try:
        children = win.query_tree().children
    except error.XError:
        return

    for child in children:
        yield from walk_windows(child)


def find_game_window_id():
    try:
        for win in walk_windows(X_ROOT):
            name = get_window_name(win)
            if name and GAME_WINDOW_NAME in name:
                return win.id
    except error.XError:
        return None

    return None


def ensure_game_window_id():
    global GAME_WINDOW_ID
    if GAME_WINDOW_ID is None:
        GAME_WINDOW_ID = find_game_window_id()
    return GAME_WINDOW_ID


def invalidate_game_window_id():
    global GAME_WINDOW_ID
    GAME_WINDOW_ID = None


def is_game_window_active():
    wid = ensure_game_window_id()
    if not wid:
        return False

    try:
        prop = X_ROOT.get_full_property(NET_ACTIVE_WINDOW, X.AnyPropertyType)
        if not prop or not prop.value:
            return False
        active = int(prop.value[0])
        return active == int(wid)
    except error.XError:
        return False


def get_absolute_geometry(win):
    geom = win.get_geometry()
    x = geom.x
    y = geom.y

    while True:
        parent = win.query_tree().parent
        if parent.id == X_ROOT.id:
            break
        pgeom = parent.get_geometry()
        x += pgeom.x
        y += pgeom.y
        win = parent

    return x, y, geom.width, geom.height


def get_window_geometry():
    for _ in range(2):
        wid = ensure_game_window_id()
        if not wid:
            return None

        try:
            win = X_DISPLAY.create_resource_object("window", int(wid))
            return get_absolute_geometry(win)
        except error.XError:
            invalidate_game_window_id()

    return None


def scale_box(box, current_w, current_h):
    x, y, w, h = box
    scale_x = current_w / REFERENCE_WIDTH
    scale_y = current_h / REFERENCE_HEIGHT
    return (
        int(round(x * scale_x)),
        int(round(y * scale_y)),
        int(round(w * scale_x)),
        int(round(h * scale_y)),
    )


def parse_es(text):
    cleaned = text.strip()
    cleaned = cleaned.replace("]", "/").replace("|", "/").replace("\\", "/")
    cleaned = re.sub(r"[^0-9,./ ]", "", cleaned)

    parts = cleaned.split("/")
    if len(parts) != 2:
        return None

    def norm(v):
        digits = re.sub(r"[^0-9]", "", v)
        return int(digits) if digits else None

    cur = norm(parts[0])
    total = norm(parts[1])

    if not cur or not total or total <= 0:
        return None

    return cur, total


def capture_ocr_region(win_id, win_w, win_h):
    ocr_x, ocr_y, ocr_w, ocr_h = scale_box(OCR_BOX, win_w, win_h)

    screen = QApplication.primaryScreen()
    if screen is None:
        raise RuntimeError("No primary screen available")

    pixmap = screen.grabWindow(int(win_id), ocr_x, ocr_y, ocr_w, ocr_h)
    qimage = pixmap.toImage().convertToFormat(QImage.Format_RGBA8888)

    width = qimage.width()
    height = qimage.height()
    ptr = qimage.bits()
    ptr.setsize(qimage.byteCount())

    img = Image.frombuffer(
        "RGBA",
        (width, height),
        bytes(ptr),
        "raw",
        "RGBA",
        0,
        1,
    ).convert("L")

    img = ImageOps.autocontrast(img)
    img = img.resize((img.width * 4, img.height * 4))
    img = img.filter(ImageFilter.SHARPEN)
    img = img.point(lambda p: 255 if p >= 235 else 0)

    return img


class Overlay(QWidget):
    def __init__(self):
        super().__init__()

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.X11BypassWindowManagerHint
        )

        self.current = 1
        self.total = 1
        self.display_ratio = 1.0
        self.target_ratio = 1.0
        self.samples = deque(maxlen=5)

        self.overlay_enabled = True
        self.show_text = CONFIG.getboolean("overlay", "show_text", fallback=False)
        self.show_edge = CONFIG.getboolean("overlay", "show_edge", fallback=False)

        self.orb_rect = QRectF()
        self.tuning_window = None

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(75)

        self.show()

    def tick(self):
        try:
            if not self.overlay_enabled:
                if self.isVisible():
                    self.hide()
                return

            geom = get_window_geometry()
            if not geom:
                if self.isVisible():
                    self.hide()
                return

            tuning_active = self.tuning_window is not None and self.tuning_window.isVisible()

            if not is_game_window_active() and not tuning_active:
                if self.isVisible():
                    self.hide()
                return
            elif not self.isVisible():
                self.show()

            win_x, win_y, win_w, win_h = geom
            self.setGeometry(win_x, win_y, win_w, win_h)

            orb_x, orb_y, orb_w, orb_h = scale_box(ORB_BOX, win_w, win_h)
            self.orb_rect = QRectF(orb_x, orb_y, orb_w, orb_h)

            wid = ensure_game_window_id()
            if not wid:
                return

            img = capture_ocr_region(wid, win_w, win_h)

            data = pytesseract.image_to_data(
                img,
                config="--psm 7 -c tessedit_char_whitelist=0123456789,/",
                output_type=pytesseract.Output.DICT,
            )

            parts = []
            confs = []

            for txt, conf in zip(data["text"], data["conf"]):
                txt = txt.strip()
                if not txt:
                    continue

                parts.append(txt)

                try:
                    c = float(conf)
                    if c >= 0:
                        confs.append(c)
                except ValueError:
                    pass

            text = "".join(parts)
            avg_conf = sum(confs) / len(confs) if confs else 0

            parsed = None
            if avg_conf >= 80:
                parsed = parse_es(text)

            if parsed:
                cur, total = parsed
                if total > 0 and 0 <= cur <= total:
                    self.samples.append((cur, total))

            if self.samples:
                self.current, self.total = self.samples[-1]
                self.target_ratio = self.current / self.total if self.total else 0.0
            elif self.current <= 0 or self.total <= 0:
                return

            self.display_ratio += (self.target_ratio - self.display_ratio) * 0.65
            self.update()

        except Exception as e:
            print("tick error:", e)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        rect = self.orb_rect.adjusted(5, 5, -5, -5)

        orb_path = QPainterPath()
        orb_path.addEllipse(rect)

        p.setClipPath(orb_path)
        p.fillPath(orb_path, QColor(10, 10, 10, 40))

        ratio = max(0.0, min(1.0, self.display_ratio))
        fill_height = rect.height() * ratio
        fill_rect = QRectF(
            rect.left(),
            rect.bottom() - fill_height,
            rect.width(),
            fill_height,
        )

        fill_grad = QLinearGradient(fill_rect.left(), 0, fill_rect.right(), 0)
        fill_grad.setColorAt(0.00, QColor(30, 140, 255, 12))
        fill_grad.setColorAt(0.10, QColor(30, 140, 255, 22))
        fill_grad.setColorAt(0.22, QColor(30, 140, 255, 45))
        fill_grad.setColorAt(0.35, QColor(30, 140, 255, 85))
        fill_grad.setColorAt(0.55, QColor(30, 140, 255, 145))
        fill_grad.setColorAt(1.00, QColor(30, 140, 255, 190))
        p.fillRect(fill_rect, QBrush(fill_grad))

        p.setClipping(False)

        if self.show_edge:
            p.setPen(QPen(QColor(120, 220, 255, 220), 3))
            p.drawEllipse(rect)

        if self.show_text:
            p.setPen(QColor(220, 245, 255, 230))
            p.setFont(QFont("Sans", 15, QFont.Bold))

            text_rect = self.orb_rect.adjusted(0, 0, 0, 0)
            p.drawText(
                text_rect,
                Qt.AlignCenter | Qt.TextSingleLine,
                f"{self.current:,}",
            )

        if self.tuning_window is not None and self.tuning_window.isVisible():
            ocr_x, ocr_y, ocr_w, ocr_h = scale_box(
                tuple(self.tuning_window.ocr_rect), self.width(), self.height()
            )
            orb_x, orb_y, orb_w, orb_h = scale_box(
                tuple(self.tuning_window.orb_rect), self.width(), self.height()
            )

            p.setBrush(Qt.NoBrush)

            active = self.tuning_window.tuning_target

            ocr_color = QColor(255, 90, 90) if active == "ocr" else QColor(180, 100, 100)
            orb_color = QColor(90, 180, 255) if active == "orb" else QColor(100, 140, 180)

            p.setPen(QPen(ocr_color, 2, Qt.DashLine))
            p.drawRect(ocr_x, ocr_y, ocr_w, ocr_h)
            p.drawText(ocr_x + 4, max(14, ocr_y - 6), "OCR")

            p.setPen(QPen(orb_color, 2, Qt.DashLine))
            p.drawRect(orb_x, orb_y, orb_w, orb_h)
            p.drawText(orb_x + 4, max(14, orb_y - 6), "ORB")


class TuningWindow(QDialog):
    def __init__(self, overlay, on_close):
        super().__init__(None)

        self.overlay = overlay
        self.on_close = on_close

        self.setWindowTitle("PoE2 Overlay Tuning")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setFocusPolicy(Qt.StrongFocus)
        self.installEventFilter(self)
        self.resize(560, 260)

        self.tuning_target = "ocr"
        self.tuning_step = 2
        self.ocr_rect = list(OCR_BOX)
        self.orb_rect = list(ORB_BOX)
        self.tuning_rect = list(self.ocr_rect)

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def closeEvent(self, event):
        self.overlay.update()
        self.on_close()
        super().closeEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        p.fillRect(self.rect(), QColor(24, 24, 28))

        title_font = QFont("Sans", 12, QFont.Bold)
        body_font = QFont("Sans", 10)

        p.setPen(QColor(240, 240, 240))
        p.setFont(title_font)
        p.drawText(20, 30, "PoE2 Overlay Tuning")

        p.setFont(body_font)

        x, y, w, h = self.tuning_rect
        help_text = (
            f"Target: {self.tuning_target.upper()}\n"
            f"x={x}  y={y}  w={w}  h={h}  step={self.tuning_step}\n\n"
            "Arrows: move\n"
            "Shift+Arrows: resize\n"
            "Tab: switch OCR / ORB\n"
            "[ / ]: step down / up\n"
            "Ctrl+S: save\n"
            "Esc: close"
        )

        p.drawText(
            QRectF(20, 45, 240, 180),
            Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
            help_text,
        )

        p.setPen(QColor(180, 180, 190))
        p.drawText(
            QRectF(290, 30, 230, 170),
            Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
            "The OCR and ORB boxes are shown on the live game overlay.\n\n"
            "Use the arrow keys to move the active box in its real on-screen position."
        )

    def handle_key_event(self, event):
        self.keyPressEvent(event)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            self.handle_key_event(event)
            return True
        return False

    def keyPressEvent(self, event):
        global OCR_BOX, ORB_BOX

        mods = event.modifiers()
        key = event.key()

        if key == Qt.Key_Escape:
            self.close()
            event.accept()
            return

        if key == Qt.Key_Tab:
            if self.tuning_target == "ocr":
                self.ocr_rect = list(self.tuning_rect)
                self.tuning_target = "orb"
                self.tuning_rect = list(self.orb_rect)
            else:
                self.orb_rect = list(self.tuning_rect)
                self.tuning_target = "ocr"
                self.tuning_rect = list(self.ocr_rect)

            self.overlay.update()
            self.update()
            event.accept()
            return

        if key == Qt.Key_BracketLeft:
            self.tuning_step = max(1, self.tuning_step - 1)
            self.overlay.update()
            self.update()
            event.accept()
            return

        if key == Qt.Key_BracketRight:
            self.tuning_step = min(50, self.tuning_step + 1)
            self.overlay.update()
            self.update()
            event.accept()
            return

        if (mods & Qt.ControlModifier) and key == Qt.Key_S:
            x, y, w, h = self.tuning_rect

            if self.tuning_target == "ocr":
                self.ocr_rect = [x, y, w, h]
                OCR_BOX = tuple(self.ocr_rect)
                CONFIG["overlay"]["ocr_x"] = str(x)
                CONFIG["overlay"]["ocr_y"] = str(y)
                CONFIG["overlay"]["ocr_w"] = str(w)
                CONFIG["overlay"]["ocr_h"] = str(h)
            else:
                self.orb_rect = [x, y, w, h]
                ORB_BOX = tuple(self.orb_rect)
                CONFIG["overlay"]["orb_x"] = str(x)
                CONFIG["overlay"]["orb_y"] = str(y)
                CONFIG["overlay"]["orb_w"] = str(w)
                CONFIG["overlay"]["orb_h"] = str(h)

            save_config(CONFIG)
            self.overlay.update()
            self.update()
            event.accept()
            return

        x, y, w, h = self.tuning_rect
        step = self.tuning_step
        resize_mode = bool(mods & Qt.ShiftModifier)

        if key == Qt.Key_Left:
            if resize_mode:
                w = max(10, w - step)
            else:
                x -= step
        elif key == Qt.Key_Right:
            if resize_mode:
                w += step
            else:
                x += step
        elif key == Qt.Key_Up:
            if resize_mode:
                h = max(10, h - step)
            else:
                y -= step
        elif key == Qt.Key_Down:
            if resize_mode:
                h += step
            else:
                y += step
        else:
            super().keyPressEvent(event)
            return

        self.tuning_rect = [x, y, w, h]

        if self.tuning_target == "ocr":
            self.ocr_rect = list(self.tuning_rect)
        else:
            self.orb_rect = list(self.tuning_rect)

        self.overlay.update()
        self.update()
        event.accept()


class TrayController:
    def __init__(self, overlay, app):
        self.overlay = overlay
        self.app = app
        self.tuning_window = None

        if not QSystemTrayIcon.isSystemTrayAvailable():
            raise RuntimeError("System tray is not available")

        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(30, 140, 255, 220))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, 12, 12)
        painter.end()

        self.tray = QSystemTrayIcon(QIcon(pixmap))
        self.tray.setToolTip("PoE2 ES Overlay")

        self.menu = QMenu()

        self.toggle_overlay_action = QAction("Overlay enabled", self.menu)
        self.toggle_overlay_action.setCheckable(True)
        self.toggle_overlay_action.setChecked(True)
        self.toggle_overlay_action.toggled.connect(self.on_toggle_overlay)
        self.menu.addAction(self.toggle_overlay_action)

        self.toggle_text_action = QAction("Show globe text", self.menu)
        self.toggle_text_action.setCheckable(True)
        self.toggle_text_action.setChecked(self.overlay.show_text)
        self.toggle_text_action.toggled.connect(self.on_toggle_text)
        self.menu.addAction(self.toggle_text_action)

        self.toggle_edge_action = QAction("Show blue edge", self.menu)
        self.toggle_edge_action.setCheckable(True)
        self.toggle_edge_action.setChecked(self.overlay.show_edge)
        self.toggle_edge_action.toggled.connect(self.on_toggle_edge)
        self.menu.addAction(self.toggle_edge_action)

        self.tuning_action = QAction("Enter tuning mode", self.menu)
        self.tuning_action.triggered.connect(self.enter_tuning_mode)
        self.menu.addAction(self.tuning_action)

        self.menu.addSeparator()

        self.quit_action = QAction("Quit", self.menu)
        self.quit_action.triggered.connect(self.app.quit)
        self.menu.addAction(self.quit_action)

        self.tray.setContextMenu(self.menu)
        self.tray.show()

    def on_toggle_overlay(self, checked):
        self.overlay.overlay_enabled = checked
        self.overlay.update()

    def on_toggle_text(self, checked):
        self.overlay.show_text = checked
        CONFIG["overlay"]["show_text"] = "true" if checked else "false"
        save_config(CONFIG)
        self.overlay.update()

    def on_toggle_edge(self, checked):
        self.overlay.show_edge = checked
        CONFIG["overlay"]["show_edge"] = "true" if checked else "false"
        save_config(CONFIG)
        self.overlay.update()

    def clear_tuning_window(self):
        if self.tuning_window is not None:
            try:
                self.app.removeEventFilter(self.tuning_window)
            except Exception:
                pass
        self.tuning_window = None
        self.overlay.tuning_window = None
        self.overlay.update()

    def enter_tuning_mode(self):
        if self.tuning_window is None:
            self.tuning_window = TuningWindow(self.overlay, self.clear_tuning_window)

        self.overlay.tuning_window = self.tuning_window
        self.app.installEventFilter(self.tuning_window)
        self.tuning_window.show()
        self.tuning_window.raise_()
        self.tuning_window.activateWindow()
        self.tuning_window.setFocus()
        self.overlay.update()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    overlay = Overlay()
    tray = TrayController(overlay, app)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
