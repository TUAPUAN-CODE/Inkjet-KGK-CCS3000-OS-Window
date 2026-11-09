"""
CCS3000 Scan-and-Send — Desktop App (Industrial UI, one-screen layout)
======================================================================
Single-window native app (no web server, no browser needed).

- Scan with a USB/Bluetooth barcode/QR scanner (acts as a keyboard)
  -> just scan, the app auto-submits.
- OR scan with the device camera (Surface front / rear camera).
  Needs:  pip install opencv-python pillow
  Optional (real camera names):  pip install pygrabber
  Optional (also reads 1D barcodes):  pip install pyzbar
- QR payloads shorter than required are padded with trailing spaces
  up to (fields x digits per field), e.g. 160 characters.
- Validates BEFORE sending anything to the printer.
- Splits the payload into N fields and sends them to N Text modules
  over ONE persistent TCP connection.

UI: iOS "liquid glass" look in the i-Tail logo colours (orange / amber /
teal / blue)  (needs Pillow:  pip install pillow)
- Soft gradient wallpaper, frosted rounded cards, pill buttons, segmented tabs.
- Status banner is always visible at the top (blue / amber / green / red).
- Tab "สแกน": scan box + result table. The camera is NOT shown all the
  time: press "สแกนด้วยกล้อง" and a camera window pops up in the middle of
  the screen; it closes by itself right after a code is read.
- Tabs "ตั้งค่า" and "Log" keep the rarely used parts out of the way.
- Settings -> "ทดสอบการเชื่อมต่อ": connects to the printer and, when that
  works, sends a short test text to EVERY configured Text module.
- Shortcuts: Esc = clear, F2 = focus scan box, F5 = resend last.

Run:
    python ccs3000_app.py
"""

import base64
import io
import ipaddress
import json
import os
import socket
import threading
import queue
import time
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox
from datetime import datetime

import ccs3000_comm as proto


# ---------------------------------------------------------------------------
# Paths / Config
# ---------------------------------------------------------------------------

BASE_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "printer_ip": "192.168.1.110",
    "printer_port": 1024,
    # list of known printers (name -> ip/port); the user picks a NAME instead
    # of typing an IP. printer_ip / printer_port always mirror the selected one.
    "printers": [],
    "printer_name": "",
    "use_checksum": True,
    "module_numbers": [497, 498, 499, 500],
    "message_no": 999,
    "digits_per_field": 40,
    "code_system": "UNICODE",
    "camera_index": 0,
    "camera_name": "",
    "camera_mode": 0,
}


def parse_module_numbers(text: str) -> list[int]:
    """Parses "497,498, 499" -> [497, 498, 499]. Raises ValueError (Thai)."""
    if text is None:
        raise ValueError("กรุณาระบุหมายเลข module อย่างน้อย 1 ตัว")

    parts = [p.strip() for p in text.split(",")]
    parts = [p for p in parts if p != ""]

    if not parts:
        raise ValueError("กรุณาระบุหมายเลข module อย่างน้อย 1 ตัว")

    modules = []
    for p in parts:
        if not p.isdigit():
            raise ValueError(
                f"หมายเลข module ต้องเป็นตัวเลขเท่านั้น (พบ: '{p}')"
            )
        n = int(p)
        if not (1 <= n <= 500):
            raise ValueError(
                f"หมายเลข module ต้องอยู่ระหว่าง 1-500 (พบ: {n})"
            )
        modules.append(n)

    return modules


def validate_printer(name, ip, port, others):
    """Validates one entry of the printer list. Returns (name, ip, port).
    `others` = names already in the list (excluding the entry being edited).
    Raises ValueError with a Thai message."""

    name = (name or "").strip()
    ip = (ip or "").strip()

    if not name:
        raise ValueError("กรุณาระบุชื่อเครื่อง (Name)")
    if len(name) > 40:
        raise ValueError("ชื่อเครื่องยาวเกินไป (ไม่เกิน 40 ตัวอักษร)")
    if name.lower() in [o.lower() for o in others]:
        raise ValueError(f"มีชื่อ '{name}' อยู่ในรายการแล้ว")

    try:
        ip = str(ipaddress.IPv4Address(ip))
    except ValueError:
        raise ValueError(
            f"IP ไม่ถูกต้อง: '{ip}' (ต้องเป็นรูปแบบ เช่น 192.168.1.110)")

    try:
        port = int(str(port).strip())
    except ValueError:
        raise ValueError("Port ต้องเป็นตัวเลข")
    if not (1 <= port <= 65535):
        raise ValueError("Port ต้องอยู่ระหว่าง 1-65535")

    return name, ip, port


def normalize_printers(cfg: dict) -> dict:
    """Makes sure cfg has a valid printer list and a selected printer.
    Old config files (only printer_ip / printer_port) become one entry."""

    good = []
    seen = set()
    for item in cfg.get("printers") or []:
        try:
            n, ip, port = validate_printer(
                item.get("name"), item.get("ip"), item.get("port"),
                list(seen))
        except (ValueError, AttributeError):
            continue
        seen.add(n)
        good.append({"name": n, "ip": ip, "port": port})

    if not good:
        try:
            _n, ip, port = validate_printer(
                "x", cfg.get("printer_ip"), cfg.get("printer_port"), [])
        except ValueError:
            ip, port = "192.168.1.110", 1024
        good = [{"name": "เครื่อง Ink 1", "ip": ip, "port": port}]

    cfg["printers"] = good
    chosen = next((p for p in good if p["name"] == cfg.get("printer_name")),
                  None)
    if chosen is None:
        # keep the previously used address if it is in the list, else first
        chosen = next((p for p in good
                       if p["ip"] == cfg.get("printer_ip")
                       and p["port"] == cfg.get("printer_port")), good[0])
    cfg["printer_name"] = chosen["name"]
    cfg["printer_ip"] = chosen["ip"]
    cfg["printer_port"] = chosen["port"]
    return cfg


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            cfg = DEFAULT_CONFIG.copy()
    cfg["printers"] = [dict(p) for p in cfg.get("printers") or []]
    return normalize_printers(cfg)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Payload handling
# ---------------------------------------------------------------------------

def clean_scanner_input(payload: str) -> str:
    """
    Remove only scanner line-ending characters (CR/LF).
    Do NOT strip spaces: trailing spaces are part of the payload.
    """
    if payload is None:
        return ""
    payload = payload.replace("\r", "")
    payload = payload.replace("\n", "")
    return payload


def pad_payload_to_length(payload: str, expected_len: int) -> tuple[str, int]:
    """Add trailing spaces until payload reaches expected_len."""
    payload = clean_scanner_input(payload)
    current_len = len(payload)

    if current_len == expected_len:
        return payload, 0

    if current_len > expected_len:
        raise ValueError(
            f"ข้อมูลยาวเกินกำหนด: ได้ {current_len} ตัว "
            f"แต่ต้องการไม่เกิน {expected_len} ตัว"
        )

    spaces_to_add = expected_len - current_len
    return payload + (" " * spaces_to_add), spaces_to_add


def split_payload(
    payload: str,
    field_count: int,
    digits_per_field: int
) -> tuple[list[str], int, int, int]:
    """
    Validate and split QR payload (auto-pads with trailing spaces).

    Returns: chunks, actual_data_length, trailing_space_count,
             added_space_count
    """
    payload = clean_scanner_input(payload)
    expected_len = field_count * digits_per_field

    payload, added_space_count = pad_payload_to_length(payload, expected_len)

    actual_len = len(payload)
    if actual_len != expected_len:
        raise ValueError(
            f"ความยาวข้อมูลไม่ถูกต้อง: ได้ {actual_len} ตัว "
            f"แต่ต้องการ {expected_len} ตัว "
            f"({field_count} field × {digits_per_field} ตัว)"
        )

    trailing_space_count = len(payload) - len(payload.rstrip(" "))
    actual_data_length = actual_len - trailing_space_count

    # Content is free-form text; only the total length matters.
    if not payload[:actual_data_length]:
        raise ValueError("ไม่พบข้อมูลจริงใน QR Code")

    chunks = [
        payload[i:i + digits_per_field]
        for i in range(0, expected_len, digits_per_field)
    ]

    return chunks, actual_data_length, trailing_space_count, added_space_count


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_fields(
    ip: str,
    port: int,
    use_checksum: bool,
    module_numbers: list[int],
    chunks: list[str],
    code_system: str,
    message_no: int = 0,
    timeout: float = 5.0
) -> list[dict]:
    """
    Opens ONE TCP connection and sends one STM command per chunk,
    chunk[i] -> module_numbers[i].
    """
    if len(module_numbers) != len(chunks):
        raise ValueError(
            f"จำนวน module ({len(module_numbers)}) ไม่ตรงกับจำนวน field "
            f"({len(chunks)})"
        )

    results = []

    with socket.create_connection((ip, port), timeout=timeout) as sock:

        sock.settimeout(timeout)

        for module_no, chunk in zip(module_numbers, chunks):

            # UNICODE modules expect each character as 4-digit hex codepoint.
            if code_system.upper() == "UNICODE":
                wire_text = proto.text_to_unicode_hex(chunk)
            else:
                wire_text = chunk

            params = proto.build_stm_content_only(
                module_no,
                wire_text,
                code_system,
                msg_no=message_no
            )

            try:
                r = proto.send_command(sock, "STM", params, use_checksum)

                results.append({
                    "module": module_no,
                    "ok": r["ok"],
                    "error_code": r.get("error_code")
                })

            except Exception as e:
                # Catch-all on purpose: an escaping exception would kill the
                # thread and leave the UI stuck on "sending" forever.
                results.append({
                    "module": module_no,
                    "ok": False,
                    "error_code": f"{type(e).__name__}: {e}"
                })
                break

    return results


# ---------------------------------------------------------------------------
# Camera scanning (optional: needs opencv-python + pillow)
# ---------------------------------------------------------------------------

os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

try:
    from PIL import (Image, ImageChops, ImageDraw, ImageFilter, ImageOps,
                     ImageTk)
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import cv2
    CAMERA_AVAILABLE = PIL_AVAILABLE
except ImportError:
    cv2 = None
    CAMERA_AVAILABLE = False

try:
    from pyzbar import pyzbar          # optional, adds 1D barcode support
except Exception:
    pyzbar = None

PREVIEW_W = 480
PREVIEW_H = 270
SCAN_SHOW_MS = 1200       # how long a camera-scanned value stays visible
DUPLICATE_GAP_SEC = 1.5   # same code must leave the view this long to re-scan
READY_RESET_MS = 5000     # success banner returns to "ready" after this


def _backends():
    """Capture backends to try, in order."""
    if cv2 is None:
        return []
    if sys.platform.startswith("win"):
        return [
            ("DSHOW", cv2.CAP_DSHOW),
            ("MSMF", cv2.CAP_MSMF),
            ("ANY", cv2.CAP_ANY),
        ]
    return [("ANY", cv2.CAP_ANY)]


def _wait_for_frame(cap, timeout, stop_event=None):
    """True once the camera really delivers a frame (not just 'opened')."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if stop_event is not None and stop_event.is_set():
            return False
        ok, frame = cap.read()
        if ok and frame is not None and frame.size > 0:
            return True
        time.sleep(0.05)
    return False


def capture_profiles():
    """Capture modes: (name, backend, (w, h) or None, fourcc or None)."""
    if cv2 is None:
        return []

    if sys.platform.startswith("win"):
        D, M = cv2.CAP_DSHOW, cv2.CAP_MSMF
        return [
            ("DSHOW 1280x720",       D, (1280, 720), None),
            ("DSHOW ค่าเริ่มต้น",     D, None,        None),
            ("MSMF 1280x720",        M, (1280, 720), None),
            ("MSMF ค่าเริ่มต้น",      M, None,        None),
            ("DSHOW MJPG 1280x720",  D, (1280, 720), "MJPG"),
        ]

    return [
        ("1280x720",       cv2.CAP_ANY, (1280, 720), None),
        ("ค่าเริ่มต้น",     cv2.CAP_ANY, None,        None),
    ]


def open_capture(index: int, stop_event=None, first_profile: int = 0):
    """
    Opens camera `index` with the chosen mode first, then the others.
    A mode only counts when a real frame arrives.
    Returns (cap, profile_index, tried_notes); cap is None on failure.
    """
    profiles = capture_profiles()
    tried = []

    if not profiles:
        return None, None, tried

    first_profile = max(0, min(first_profile, len(profiles) - 1))
    order = [first_profile] + [
        i for i in range(len(profiles)) if i != first_profile
    ]

    for pi in order:

        if stop_event is not None and stop_event.is_set():
            return None, None, tried

        name, backend, size, fourcc = profiles[pi]

        cap = cv2.VideoCapture(index, backend)

        if not cap.isOpened():
            cap.release()
            tried.append(f"{name}: เปิดไม่ได้")
            continue

        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))

        if size:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])

        if _wait_for_frame(cap, 4.0, stop_event):
            return cap, pi, tried

        cap.release()
        tried.append(f"{name}: ไม่มีภาพ")

    return None, None, tried


def camera_facing(name: str) -> str:
    """Guess front/rear from the device name."""
    n = (name or "").lower()
    if "front" in n or "หน้า" in n:
        return "กล้องหน้า"
    if "rear" in n or "back" in n or "หลัง" in n:
        return "กล้องหลัง"
    return ""


def list_cameras(max_index: int = 6) -> list[tuple[int, str]]:
    """Returns [(opencv_index, device_name), ...]."""
    if not CAMERA_AVAILABLE:
        return []

    try:
        from pygrabber.dshow_graph import FilterGraph
        names = FilterGraph().get_input_devices()
        if names:
            return [(i, n) for i, n in enumerate(names)]
    except Exception:
        pass

    cams = []
    for i in range(max_index):
        found = False
        for _name, backend in _backends():
            cap = cv2.VideoCapture(i, backend)
            found = cap.isOpened()
            cap.release()
            if found:
                break
        if found:
            cams.append((i, f"Camera {i}"))

    if not cams:
        cams = [(0, "Camera 0"), (1, "Camera 1")]

    return cams


def camera_label(index: int, name: str) -> str:
    facing = camera_facing(name)
    return f"{facing} • {name}" if facing else name


def decode_frame(detector, frame):
    """Returns decoded text (trailing spaces preserved) or None."""
    if pyzbar is not None:
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            for obj in pyzbar.decode(gray):
                raw = obj.data
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("latin-1")
        except Exception:
            pass

    try:
        text, _pts, _ = detector.detectAndDecode(frame)
        if text:
            return text
    except cv2.error:
        pass

    return None


class CameraScanner:
    """Grabs frames + decodes QR/barcodes in a background thread."""

    def __init__(self, index: int, profile: int = 0):
        self.index = index
        self.profile = profile
        self.error = None
        self.backend = None
        self.announced = False
        self.results = queue.Queue()
        self._frame = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)

    def get_frame(self):
        with self._lock:
            return self._frame

    def _run(self):

        cap, used, tried = open_capture(
            self.index, self._stop, self.profile
        )

        if cap is None:
            if not self._stop.is_set():
                self.error = (
                    f"เปิดกล้อง index {self.index} ไม่ได้\n"
                    f"ลองแล้ว: {'; '.join(tried)}\n\n"
                    "ตรวจสอบ:\n"
                    "• Settings > Privacy & security > Camera → เปิด "
                    "\"Let desktop apps access your camera\"\n"
                    "• ปิดโปรแกรมอื่นที่ใช้กล้องอยู่ (Camera, Teams, Zoom)\n"
                    "• ลองเลือกกล้องตัวอื่นในรายการ"
                )
            return

        self.profile = used
        self.backend = capture_profiles()[used][0]

        detector = cv2.QRCodeDetector()
        n = 0
        fails = 0

        try:
            while not self._stop.is_set():
                ok, frame = cap.read()

                if not ok or frame is None:
                    fails += 1
                    if fails > 100:
                        self.error = "กล้องหยุดส่งภาพ (อาจถูกโปรแกรมอื่นแย่งใช้)"
                        return
                    time.sleep(0.05)
                    continue

                fails = 0
                n += 1

                with self._lock:
                    self._frame = frame

                if n % 3 == 0:
                    text = decode_frame(detector, frame)
                    if text:
                        self.results.put(text)
        finally:
            cap.release()


# ---------------------------------------------------------------------------
# Theme — iOS "liquid glass"
# ---------------------------------------------------------------------------
# Tk cannot blur what is behind a widget, so the glass look is drawn with
# Pillow: a soft pastel gradient window background, and frosted, rounded,
# softly shadowed cards that are rendered on top of the exact piece of
# gradient behind them.

FONT = "Segoe UI"
MONO = "Consolas"

C_TEXT = "#14233b"
C_MUTED = "#5d6f86"
C_BLUE = "#0062c2"             # i-Tail wordmark blue
C_BLUE_DK = "#004a96"
C_TEAL = "#2e9bb8"             # logo swoosh
C_ORANGE = "#f4720a"           # logo wing
C_AMBER = "#fdb927"            # logo wing (light end)
C_LINE = "#c9dded"
C_GLASS = "#f4f8ff"            # inner colour of every glass card; must stay
                               # equal to the background of the logo PNG
C_FALLBACK = "#e4eaf0"         # shown for a split second before the gradient

GLASS_RGB = (244, 248, 255)
GLASS_ALPHA = 1.0
CARD_MARGIN = 10               # room around a card for its soft shadow
GLOW_DEFAULT = (70, 160, 205)

# banner states: (background, foreground, icon)
STATE_GLOW = {
    "ready": (70, 160, 205),
    "busy":  (253, 185, 39),
    "ok":    (70, 205, 140),
    "error": (255, 110, 100),
}

STATES = {
    "ready": ("#e3f1fb", "#004a96", "●"),
    "busy":  ("#fff3d9", "#8a4b00", "◌"),
    "ok":    ("#e0f6ea", "#12713a", "✔"),
    "error": ("#ffe4e0", "#a8201a", "✖"),
}

BTN_STYLES = {
    "primary": dict(fill=(244, 114, 10), hover=(224, 98, 0),
                    press=(190, 80, 0), fg="#ffffff", outline=None),
    "blue":    dict(fill=(0, 98, 194), hover=(0, 80, 162),
                    press=(0, 64, 132), fg="#ffffff", outline=None),
    "teal":    dict(fill=(46, 155, 184), hover=(36, 134, 160),
                    press=(28, 112, 136), fg="#ffffff", outline=None),
    "light":   dict(fill=(255, 255, 255), hover=(232, 244, 252),
                    press=(212, 232, 246), fg=C_BLUE,
                    outline=(196, 220, 238)),
    "danger":  dict(fill=(255, 236, 234), hover=(255, 222, 219),
                    press=(255, 204, 199), fg="#d92d20", outline=None),
}
BTN_DISABLED = dict(fill=(228, 233, 241), fg="#a0a8b8", outline=None)


def _rgb(hexcolor: str) -> tuple:
    h = hexcolor.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _round_mask(w, h, r, ss=3):
    """Anti-aliased rounded-rectangle alpha mask (mode L)."""
    w, h = max(1, int(w)), max(1, int(h))
    m = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1), radius=int(r * ss), fill=255
    )
    return m.resize((w, h), Image.LANCZOS)


def _ring_mask(w, h, r, width=1.5, ss=3):
    """Thin anti-aliased rounded outline (mode L)."""
    w, h = max(1, int(w)), max(1, int(h))
    m = Image.new("L", (w * ss, h * ss), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1),
                        radius=int(r * ss), fill=255)
    iw = int(width * ss)
    d.rounded_rectangle((iw, iw, w * ss - 1 - iw, h * ss - 1 - iw),
                        radius=max(0, int(r * ss) - iw), fill=0)
    return m.resize((w, h), Image.LANCZOS)


def make_brand_bar(w, h=4):
    """Thin orange -> amber -> teal -> blue bar (the colours of the logo)."""
    stops = [(244, 114, 10), (253, 185, 39), (46, 155, 184), (0, 98, 194)]
    bar = Image.new("RGB", (w, h))
    px = bar.load()
    for x in range(w):
        t = x / max(1, w - 1) * (len(stops) - 1)
        i = min(int(t), len(stops) - 2)
        f = t - i
        c = tuple(int(stops[i][k] + (stops[i + 1][k] - stops[i][k]) * f)
                  for k in range(3))
        for y in range(h):
            px[x, y] = c
    out = Image.new("RGB", (w, h), _rgb(C_GLASS))
    out.paste(bar, (0, 0), _round_mask(w, h, h // 2))
    return out


def _roundbox_image(w, h, r, fill, outline=None, ow=1, ss=3):
    """RGBA rounded box with transparent corners (for pills / fields)."""
    w, h = max(2, int(w)), max(2, int(h))
    r = min(r, h // 2, w // 2)
    base = Image.new("RGBA", (w * ss, h * ss),
                     tuple(outline or fill) + (0,))
    d = ImageDraw.Draw(base)
    if outline:
        d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1),
                            radius=r * ss, fill=tuple(outline) + (255,))
        d.rounded_rectangle(
            (ow * ss, ow * ss, w * ss - 1 - ow * ss, h * ss - 1 - ow * ss),
            radius=max(0, (r - ow) * ss), fill=tuple(fill) + (255,))
    else:
        d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1),
                            radius=r * ss, fill=tuple(fill) + (255,))
    return base.resize((w, h), Image.LANCZOS)


def render_bg(w, h):
    """Soft pastel 'iOS wallpaper': diagonal gradient + blurred colour blobs."""
    w, h = max(2, int(w)), max(2, int(h))
    sw, sh = max(8, w // 5), max(8, h // 5)       # small = fast, smooth anyway

    gv = Image.linear_gradient("L").resize((sw, sh), Image.BILINEAR)
    gh = (Image.linear_gradient("L").rotate(90, expand=True)
          .resize((sw, sh), Image.BILINEAR))
    diag = ImageChops.add(gv, gh, scale=2.0)

    img = ImageOps.colorize(diag, black=(222, 231, 240),
                            mid=(232, 238, 244), white=(238, 240, 243))

    blobs = (
        (0.10, 0.12, 0.30, (205, 222, 236)),     # soft blue-grey
        (0.90, 0.30, 0.28, (212, 224, 240)),     # soft blue
        (0.30, 1.00, 0.30, (236, 232, 226)),     # warm grey
    )
    big = max(sw, sh)

    for fx, fy, fr, color in blobs:
        m = Image.new("L", (sw, sh), 0)
        cx, cy, r = fx * sw, fy * sh, fr * big
        ImageDraw.Draw(m).ellipse((cx - r, cy - r, cx + r, cy + r), fill=150)
        m = m.filter(ImageFilter.GaussianBlur(big * 0.07))
        img.paste(color, (0, 0), m)

    return img.resize((w, h), Image.BICUBIC)


def _soft_blob(img, box, radius, fill, blur, color):
    """Blurred rounded blob, computed at half size (much faster)."""
    w, h = img.size
    sw, sh = max(2, w // 2), max(2, h // 2)
    m = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        tuple(v / 2 for v in box), radius=radius / 2, fill=fill)
    m = m.filter(ImageFilter.GaussianBlur(blur / 2)).resize(
        (w, h), Image.BILINEAR)
    img.paste(tuple(color), (0, 0), m)


def compose_card(crop, w, h, m, radius, fill, alpha, shadow=True,
                 border_alpha=0.9, glow=GLOW_DEFAULT):
    """Glass card drawn over `crop` (the piece of background behind it)."""
    img = crop.convert("RGB")

    if shadow:
        if glow:
            _soft_blob(img, (m - 2, m - 2, w - m + 1, h - m + 1),
                       radius + 2, 120, max(4, m * 0.55), glow)
        _soft_blob(img, (m, m + 4, w - m - 1, h - m + 3),
                   radius, 50, 8, (40, 60, 110))

    bw, bh = w - 2 * m, h - 2 * m
    mask = _round_mask(bw, bh, radius, ss=2)
    if alpha < 1.0:
        mask = mask.point(lambda v: int(v * alpha))
    img.paste(tuple(fill), (m, m), mask)

    ring = _ring_mask(bw, bh, radius, 1.6, ss=2)
    img.paste((255, 255, 255), (m, m),
              ring.point(lambda v: int(v * border_alpha)))
    return img


# ---------------------------------------------------------------------------
# Glass widgets
# ---------------------------------------------------------------------------

class GlassHost:
    """Mixin for Tk / Toplevel: owns the gradient background image."""

    def _glass_init(self):
        self.glass_bg = None
        self._glass = []
        self._bg_size = (0, 0)
        self._bg_photo = None
        self._bg_job = None

        self._bg_label = tk.Label(self, bd=0, bg=C_FALLBACK)
        self._bg_label.place(x=0, y=0, relwidth=1, relheight=1)
        self._bg_label.lower()

        self.bind("<Configure>", self._on_host_configure, add="+")

    def register_glass(self, widget):
        self._glass.append(widget)

    def on_resize(self):
        """Hook: lay children out immediately on every resize."""

    def _on_host_configure(self, event):
        if event.widget is not self:
            return
        self.on_resize()
        if self._bg_job is None:
            self._bg_job = self.after(60, self._rebuild_bg)

    def _rebuild_bg(self):
        self._bg_job = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50 or h < 50 or (w, h) == self._bg_size:
            return
        self._bg_size = (w, h)
        self.glass_bg = render_bg(w, h)
        self._bg_photo = ImageTk.PhotoImage(self.glass_bg)
        self._bg_label.configure(image=self._bg_photo)
        for g in list(self._glass):
            try:
                g.schedule_redraw()
            except tk.TclError:
                pass


class _OnGlass(tk.Canvas):
    """Canvas that paints itself over the matching piece of the gradient."""

    def __init__(self, master, **kw):
        super().__init__(master, bd=0, highlightthickness=0,
                         bg=C_FALLBACK, **kw)
        self._photo = None
        self._cache_key = None
        self._redraw_job = None
        self._geo = None        # (x, y, w, h) inside a scrolling page
        self.active = True      # hidden pages do not redraw
        self._img_id = self.create_image(0, 0, anchor="nw")
        self.winfo_toplevel().register_glass(self)
        self.bind("<Configure>", lambda _e: self.schedule_redraw())

    def set_geometry(self, x, y, w, h):
        self._geo = (int(x), int(y), int(w), int(h))
        self.schedule_redraw()

    def schedule_redraw(self):
        if not self.active:
            return
        if self._redraw_job is None:
            # short delay: dragging the window edge must not redraw on
            # every single pixel
            self._redraw_job = self.after(45, self._do_redraw)

    def _do_redraw(self):
        self._redraw_job = None
        host = self.winfo_toplevel()
        bg = getattr(host, "glass_bg", None)
        if self._geo is not None:
            x, y, w, h = self._geo
            if w < 8 or h < 8 or bg is None:
                return
        else:
            w, h = self.winfo_width(), self.winfo_height()
            if w < 8 or h < 8 or bg is None:
                return
            x = self.winfo_rootx() - host.winfo_rootx()
            y = self.winfo_rooty() - host.winfo_rooty()

        # Same place / size / look as last time -> nothing to redraw.
        # (this is what makes switching tabs instant and flicker-free)
        key = (x, y, w, h, id(bg), self.cache_extra())
        if key == self._cache_key and self._photo is not None:
            self.after_compose(w, h)
            return

        crop = bg.crop((x, y, x + w, y + h))
        self._photo = ImageTk.PhotoImage(self.compose(crop, w, h))
        self._cache_key = key
        self.itemconfigure(self._img_id, image=self._photo)
        self.after_compose(w, h)

    def force_redraw(self):
        """Draw right now (used before a page becomes visible)."""
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except tk.TclError:
                pass
            self._redraw_job = None
        self._do_redraw()

    def cache_extra(self):
        return None

    def compose(self, crop, w, h):
        return crop

    def after_compose(self, w, h):
        pass


class GlassCard(_OnGlass):
    """Frosted rounded card. Put widgets into `.inner`."""

    def __init__(self, master, radius=26, pad=16, fill=GLASS_RGB,
                 alpha=GLASS_ALPHA, inner_bg=C_GLASS):
        super().__init__(master)
        self.radius = radius
        self.pad = pad
        self.m = CARD_MARGIN
        self.fill_rgb = tuple(fill)
        self.glass_alpha = alpha
        self.glow_rgb = GLOW_DEFAULT
        self.inner = tk.Frame(self, bg=inner_bg)
        self._win = self.create_window(self.m + pad, self.m + pad,
                                       window=self.inner, anchor="nw")

    def set_fill(self, hexcolor, glow=None):
        new_fill = _rgb(hexcolor)
        new_glow = tuple(glow) if glow else self.glow_rgb
        if new_fill == self.fill_rgb and new_glow == self.glow_rgb:
            return
        self.fill_rgb = new_fill
        self.glow_rgb = new_glow
        self.inner.configure(bg=hexcolor)
        self.schedule_redraw()

    def cache_extra(self):
        return (self.fill_rgb, self.glow_rgb, self.glass_alpha)

    def compose(self, crop, w, h):
        return compose_card(crop, w, h, self.m, self.radius,
                            self.fill_rgb, self.glass_alpha,
                            glow=self.glow_rgb)

    def after_compose(self, w, h):
        off = self.m + self.pad
        self.itemconfigure(self._win, width=max(1, w - 2 * off),
                           height=max(1, h - 2 * off))


class SegTabs(_OnGlass):
    """iOS segmented control."""

    def __init__(self, master, items, command):
        super().__init__(master, cursor="hand2")
        self.items = items
        self.command = command
        self.current = items[0][0]
        self.bind("<ButtonRelease-1>", self._click)

    def cache_extra(self):
        return self.current

    def set_current(self, key):
        self.current = key
        self.schedule_redraw()

    def _seg_width(self, w):
        return (w - 12) / len(self.items)

    def _click(self, event):
        w = self.winfo_width()
        idx = int((event.x - 6) / self._seg_width(w))
        idx = max(0, min(idx, len(self.items) - 1))
        self.command(self.items[idx][0])

    def compose(self, crop, w, h):
        img = crop.convert("RGB")
        r = h // 2

        track = _round_mask(w - 4, h - 4, r - 2)
        img.paste((120, 138, 175), (2, 2), track.point(lambda v: int(v * .22)))
        img.paste((255, 255, 255), (2, 2),
                  _ring_mask(w - 4, h - 4, r - 2, 1.5)
                  .point(lambda v: int(v * .75)))

        keys = [k for k, _ in self.items]
        idx = keys.index(self.current)
        seg = self._seg_width(w)
        x0, x1 = int(6 + idx * seg), int(6 + (idx + 1) * seg)
        tw, th = x1 - x0, h - 12

        sh = Image.new("L", (w, h), 0)
        ImageDraw.Draw(sh).rounded_rectangle(
            (x0, 8, x1 - 1, h - 5), radius=th // 2, fill=70)
        sh = sh.filter(ImageFilter.GaussianBlur(4))
        img.paste((40, 60, 110), (0, 0), sh)

        img.paste((255, 255, 255), (x0, 6), _round_mask(tw, th, th // 2))
        return img

    def after_compose(self, w, h):
        self.delete("lbl")
        seg = self._seg_width(w)
        for i, (key, text) in enumerate(self.items):
            active = key == self.current
            self.create_text(
                6 + seg * (i + 0.5), h / 2, text=text, tags="lbl",
                font=(FONT, 13, "bold"),
                fill=C_BLUE if active else C_MUTED
            )


class RoundBox(tk.Canvas):
    """Rounded box (field / pill) sitting on a flat glass card."""

    def __init__(self, master, radius=14, fill="#ffffff", outline=None,
                 parent_bg=C_GLASS, pad=10, width=None, height=None, ow=1):
        super().__init__(master, bd=0, highlightthickness=0, bg=parent_bg,
                         width=width or 1, height=height or 1)
        self.radius = radius
        self.pad = pad
        self.fill_hex = fill
        self.outline_hex = outline
        self.ow = ow
        self._photo = None
        self._img_id = self.create_image(0, 0, anchor="nw")
        self.inner = tk.Frame(self, bg=fill)
        self._win = self.create_window(pad, pad, window=self.inner,
                                       anchor="nw")
        self.bind("<Configure>", lambda _e: self._draw())

    def set_style(self, outline=None, ow=None):
        self.outline_hex = outline
        if ow is not None:
            self.ow = ow
        self._draw()

    def _draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        img = _roundbox_image(
            w, h, self.radius, _rgb(self.fill_hex),
            _rgb(self.outline_hex) if self.outline_hex else None, self.ow)
        self._photo = ImageTk.PhotoImage(img)
        self.itemconfigure(self._img_id, image=self._photo)
        self.itemconfigure(self._win, width=max(1, w - 2 * self.pad),
                           height=max(1, h - 2 * self.pad))


_BTN_CACHE = {}


class GlassButton(tk.Label):
    """Pill button (iOS style) with hover / pressed / disabled states."""

    def __init__(self, master, text, style="light", command=None,
                 height=44, padx=24, min_width=0, size=12, bg=C_GLASS):
        self._style = style
        self._text = text
        self._command = command
        self._h = height
        self._state = "normal"
        self._hover = False
        self._pressed = False
        self._font = (FONT, size, "bold")
        self._padx = padx
        self._min_w = min_width

        super().__init__(master, bd=0, highlightthickness=0, bg=bg,
                         activebackground=bg, compound="center",
                         font=self._font, padx=0, pady=0)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._refresh()

    def _width(self):
        f = tkfont.Font(font=self._font)
        return max(self._min_w, f.measure(self._text) + 2 * self._padx)

    def _refresh(self):
        w, h = self._width(), self._h
        st = BTN_STYLES[self._style]

        if self._state == "disabled":
            key, fill, outline, fg = "dis", BTN_DISABLED["fill"], None, \
                BTN_DISABLED["fg"]
        else:
            if self._pressed:
                key, fill = "press", st["press"]
            elif self._hover:
                key, fill = "hover", st["hover"]
            else:
                key, fill = "norm", st["fill"]
            outline, fg = st["outline"], st["fg"]

        ck = (self._style, key, w, h)
        if ck not in _BTN_CACHE:
            _BTN_CACHE[ck] = ImageTk.PhotoImage(
                _roundbox_image(w, h, h // 2, fill, outline, 1))

        super().configure(
            image=_BTN_CACHE[ck], text=self._text, fg=fg,
            cursor="arrow" if self._state == "disabled" else "hand2"
        )

    def configure(self, cnf=None, **kw):
        kw = {**(cnf or {}), **kw}
        changed = False

        if "state" in kw:
            self._state = kw.pop("state")
            changed = True
        if "text" in kw:
            self._text = kw.pop("text")
            changed = True
        if "style" in kw:
            self._style = kw.pop("style")
            changed = True
        if "command" in kw:
            self._command = kw.pop("command")
        if kw:
            super().configure(**kw)
        if changed:
            self._refresh()

    def _on_enter(self, _e):
        self._hover = True
        self._refresh()

    def _on_leave(self, _e):
        self._hover = False
        self._pressed = False
        self._refresh()

    def _on_press(self, _e):
        if self._state == "disabled":
            return
        self._pressed = True
        self._refresh()

    def _on_release(self, e):
        was = self._pressed
        self._pressed = False
        self._refresh()
        inside = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
        if was and inside and self._state != "disabled" and self._command:
            self._command()


class ProgressPill(tk.Canvas):
    """Thin rounded progress bar."""

    def __init__(self, master, height=12, bg=C_GLASS):
        super().__init__(master, height=height, bd=0, highlightthickness=0,
                         bg=bg)
        self._ph = height
        self.maximum = 1
        self.value = 0
        self.bind("<Configure>", lambda _e: self._draw())

    def configure(self, cnf=None, **kw):
        kw = {**(cnf or {}), **kw}
        if "maximum" in kw:
            self.maximum = max(1, kw.pop("maximum"))
        if "value" in kw:
            self.value = kw.pop("value")
        if kw:
            super().configure(**kw)
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self._ph
        if w < h * 2:
            return
        r = h / 2
        self.create_line(r, h / 2, w - r, h / 2, width=h, capstyle="round",
                         fill="#d6e6f2")
        frac = min(1.0, self.value / self.maximum)
        if frac > 0:
            x = r + (w - 2 * r) * frac
            self.create_line(r, h / 2, max(x, r + 0.1), h / 2, width=h,
                             capstyle="round", fill=C_BLUE)


# i-Tail logo (PNG, pre-blended onto the header card colour)
LOGO_B64 = (
    "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"
)


class CameraDialog(GlassHost, tk.Toplevel):
    """Camera popup window with its own glass background."""

    def __init__(self, master):
        tk.Toplevel.__init__(self, master)
        self.configure(bg=C_FALLBACK)
        self._glass_init()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(GlassHost, tk.Tk):

    def __init__(self):

        tk.Tk.__init__(self)

        self.title("INKJET KGK CCS3000 • Scan & Send")
        self.geometry("1180x780")
        self.minsize(900, 740)
        self.configure(bg=C_FALLBACK)

        # Fill the screen (Surface-friendly); the layout adapts to the size.
        if sys.platform.startswith("win"):
            try:
                self.state("zoomed")
            except tk.TclError:
                pass

        self._glass_init()

        self.cfg = load_config()

        self._auto_send_job = None
        self._reset_job = None
        self._sending = False
        self._testing = False
        self._last_payload = ""

        # camera state
        self.camera = None
        self._camera_job = None
        self.cam_mode = int(self.cfg.get("camera_mode", 0))
        self._camera_list = []
        self._preview_img = None
        self._preview_mask = None
        self._cam_pending = False
        self.cam_dialog = None      # popup window, exists only while scanning
        self._cam_pos = -1          # index into self._camera_list

        self._current_tab = "scan"
        self._pages = {"scan": [], "settings": [], "log": []}
        self._items = {}
        self._content_h = 0

        self._build_style()
        self._build_page()
        self._build_header()
        self._build_banner()
        self._build_tabs()
        self._build_scan_panel()
        self._build_result_panel()
        self._build_settings_panel()
        self._build_log_panel()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.bind("<Escape>", self._on_escape)
        self.bind("<F2>", self._on_f2)
        self.bind("<F5>", self._on_f5)

        self._update_progress()
        self.set_status("ready", "พร้อมสแกน", self._ready_hint())

        self.update_idletasks()
        self._layout()
        self.scan_entry.focus_set()

        if CAMERA_AVAILABLE:
            self.after(200, self.refresh_cameras)

    # -----------------------------------------------------------------------
    # Style (ttk parts that remain: Entry, Combobox, Treeview, Label)
    # -----------------------------------------------------------------------

    def _build_style(self):

        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".", background=C_GLASS, foreground=C_TEXT,
                    font=(FONT, 11))
        s.configure("TFrame", background=C_GLASS)
        s.configure("Card.TFrame", background=C_GLASS)

        s.configure("TLabel", background=C_GLASS, foreground=C_TEXT,
                    font=(FONT, 11))
        s.configure("Muted.TLabel", background=C_GLASS, foreground=C_MUTED,
                    font=(FONT, 10))
        s.configure("Title.TLabel", background=C_GLASS, foreground=C_TEXT,
                    font=(FONT, 14, "bold"))

        s.configure("TEntry", fieldbackground="#ffffff", foreground=C_TEXT,
                    bordercolor="#cfd9ea", lightcolor="#cfd9ea",
                    darkcolor="#cfd9ea", padding=9, relief="flat")
        s.map("TEntry", bordercolor=[("focus", C_BLUE)],
              lightcolor=[("focus", C_BLUE)], darkcolor=[("focus", C_BLUE)])

        s.configure("TCombobox", fieldbackground="#ffffff",
                    foreground=C_TEXT, background="#ffffff",
                    bordercolor="#cfd9ea", lightcolor="#cfd9ea",
                    darkcolor="#cfd9ea", arrowcolor=C_BLUE, padding=9)
        s.map("TCombobox",
              fieldbackground=[("readonly", "#ffffff")],
              foreground=[("readonly", C_TEXT)],
              bordercolor=[("focus", C_BLUE)])

        s.configure("Glass.Vertical.TScrollbar", background="#c9d6ee",
                    troughcolor="#eef3fb", bordercolor="#eef3fb",
                    lightcolor="#c9d6ee", darkcolor="#c9d6ee",
                    arrowcolor="#c9d6ee", gripcount=0, width=12,
                    relief="flat")
        s.map("Glass.Vertical.TScrollbar",
              background=[("active", C_BLUE), ("pressed", C_BLUE_DK)])

        s.layout("Result.Treeview",
                 [("Treeview.treearea", {"sticky": "nswe"})])
        s.configure("Result.Treeview", font=(FONT, 12), rowheight=34,
                    background="#ffffff", fieldbackground="#ffffff",
                    foreground=C_TEXT, borderwidth=0)
        s.configure("Result.Treeview.Heading", font=(FONT, 10, "bold"),
                    background="#e4f0fa", foreground=C_MUTED, padding=6,
                    relief="flat")
        s.map("Result.Treeview.Heading",
              background=[("active", "#eef3fb"), ("pressed", "#eef3fb")])
        s.map("Result.Treeview", background=[("selected", "#dbe9ff")],
              foreground=[("selected", C_TEXT)])

    # -----------------------------------------------------------------------
    # Layout (everything is a glass card placed straight on the gradient)
    # -----------------------------------------------------------------------

    # -- scrollable page ---------------------------------------------------

    def _build_page(self):
        """The whole UI lives on one canvas that always fits the window
        (no scrolling)."""
        self.page = tk.Canvas(self, bd=0, highlightthickness=0,
                              bg=C_FALLBACK)
        self.page.pack(fill="both", expand=True)

        self._page_bg_item = self.page.create_image(0, 0, anchor="nw")

        self.page.bind("<Configure>", self._on_page_configure)

    def _on_page_configure(self, _e):
        self._layout()
        if self._bg_job is None:
            self._bg_job = self.after(120, self._rebuild_bg)

    def _rebuild_bg(self):
        self._bg_job = None
        w, ph = self.page.winfo_width(), self.page.winfo_height()
        if w < 50 or ph < 50:
            return
        h = ph
        if (w, h) == self._bg_size:
            return
        self._bg_size = (w, h)
        self.glass_bg = render_bg(w, h)
        self._bg_photo = ImageTk.PhotoImage(self.glass_bg)
        self.page.itemconfigure(self._page_bg_item, image=self._bg_photo)
        self.page.tag_lower(self._page_bg_item)
        for g in list(self._glass):
            try:
                g.schedule_redraw()
            except tk.TclError:
                pass

    def _put(self, card, x, y, w, h):
        iid = self._items.get(card)
        if iid is None:
            self._items[card] = self.page.create_window(
                x, y, window=card, anchor="nw", width=w, height=h)
        else:
            self.page.coords(iid, x, y)
            self.page.itemconfigure(iid, width=w, height=h, state="normal")
        card.active = True
        card.set_geometry(x, y, w, h)

    def _hide(self, card):
        iid = self._items.get(card)
        if iid is not None:
            self.page.itemconfigure(iid, state="hidden")
        card.active = False

    def on_resize(self):
        pass

    def _layout(self):

        if not hasattr(self, "log_card"):
            return                      # still building

        W, H = self.page.winfo_width(), self.page.winfo_height()
        if W < 200 or H < 200:
            return

        # Everything is sized to fit the window height (no scrolling).
        # Compact enough for a Surface (about 1280x850 and up).
        cw = W - 16
        self._put(self.header, 8, 0, cw, 104)
        self._put(self.banner, 8, 100, cw, 104)
        self._put(self.tabs, 24, 206, W - 48, 44)

        top = 244
        bottom_gap = 10
        avail = H - top - bottom_gap
        cur = self._current_tab

        if cur == "scan":
            scan_h = 216
            row_h = 34
            chrome = 130                      # card + title + table header
            rows = max(1, len(self.cfg["module_numbers"]))

            self._put(self.scan_card, 8, top, cw, scan_h)
            ry = top + scan_h - 4
            res_avail = H - ry - bottom_gap

            fit = max(1, (res_avail - chrome) // row_h)
            shown = min(rows, fit)
            res_h = max(chrome + row_h * shown,
                        min(res_avail, chrome + row_h * max(rows, 4) + 40))

            self.tree.configure(height=shown)
            self._put(self.result_card, 8, ry, cw, res_h)

        elif cur == "settings":
            self._put(self.settings_card, 8, top, cw,
                      max(440, min(avail, 520)))

        else:
            self._put(self.log_card, 8, top, cw, max(260, avail))

        # Switching pages: paint the new cards BEFORE hiding the old ones,
        # so the user never sees an empty frame.
        if getattr(self, "_laid_tab", None) != cur:
            self.update_idletasks()
            for c in self._pages[cur]:
                try:
                    c.force_redraw()
                except tk.TclError:
                    pass
            self._laid_tab = cur

        for key, cards in self._pages.items():
            if key != cur:
                for c in cards:
                    self._hide(c)

        self.page.configure(scrollregion=(0, 0, W, H))

    # -----------------------------------------------------------------------
    # Header
    # -----------------------------------------------------------------------

    def _build_header(self):

        self.header = GlassCard(self.page, radius=26, pad=10)
        f = self.header.inner

        try:
            self._logo_img = ImageTk.PhotoImage(
                Image.open(io.BytesIO(base64.b64decode(LOGO_B64))))
            tk.Label(f, image=self._logo_img, bd=0, bg=C_GLASS).pack(
                side="left", padx=(2, 16))
        except Exception:
            self._logo_img = None

        left = tk.Frame(f, bg=C_GLASS)
        left.pack(side="left", fill="y")

        tk.Label(left, text="INKJET KGK CCS3000  Scan & Send", bg=C_GLASS,
                 fg=C_TEXT, font=(FONT, 19, "bold")).pack(anchor="w")

        self.printer_var = tk.StringVar()
        self._refresh_printer_label()

        tk.Label(left, textvariable=self.printer_var, bg=C_GLASS,
                 fg=C_MUTED, font=(FONT, 10)).pack(anchor="w")

        # thin bar in the logo colours
        self._brand_bar = ImageTk.PhotoImage(make_brand_bar(300, 4))
        tk.Label(left, image=self._brand_bar, bd=0, bg=C_GLASS).pack(
            anchor="w", pady=(3, 0))


    def _refresh_printer_label(self):
        self.printer_var.set(
            f"เครื่องพิมพ์ {self.cfg['printer_name']} "
            f"({self.cfg['printer_ip']}:{self.cfg['printer_port']})"
            f"  •  Module {','.join(str(n) for n in self.cfg['module_numbers'])}"
        )

    # -----------------------------------------------------------------------
    # Status banner
    # -----------------------------------------------------------------------

    def _build_banner(self):

        bg, fg, icon = STATES["ready"]

        self.banner = GlassCard(self.page, radius=26, pad=8, fill=_rgb(bg),
                                alpha=1.0, inner_bg=bg)
        f = self.banner.inner

        self.banner_icon = tk.Label(f, text=icon, font=(FONT, 26), bg=bg,
                                    fg=fg)
        self.banner_icon.pack(side="left", padx=(10, 14))

        text_box = tk.Frame(f, bg=bg)
        text_box.pack(side="left", fill="both", expand=True)
        self._banner_text_box = text_box

        self.banner_title = tk.Label(text_box, text="", anchor="w",
                                     font=(FONT, 19, "bold"), bg=bg, fg=fg)
        self.banner_title.pack(fill="x")

        self.banner_sub = tk.Label(text_box, text="", anchor="nw",
                                   justify="left", wraplength=800,
                                   font=(FONT, 10), bg=bg, fg=fg)
        self.banner_sub.pack(fill="x")

        f.bind(
            "<Configure>",
            lambda e: self.banner_sub.configure(
                wraplength=max(300, e.width - 110)
            )
        )

    def set_status(self, kind, title, sub=""):
        bg, fg, icon = STATES[kind]

        self.banner.set_fill(bg, glow=STATE_GLOW.get(kind))
        self._banner_text_box.configure(bg=bg)

        self.banner_icon.configure(text=icon, bg=bg, fg=fg)
        self.banner_title.configure(text=title, bg=bg, fg=fg)
        self.banner_sub.configure(text=sub, bg=bg, fg=fg)

    def _ready_hint(self):
        n = len(self.cfg["module_numbers"])
        d = int(self.cfg["digits_per_field"])
        return (
            f"สแกนบาร์โค้ด/QR ได้เลย • ต้องการ {self._expected()} ตัว "
            f"= {n} Module × {d} ตัว (ไม่ครบระบบเติม Space ให้อัตโนมัติ)"
        )

    def _schedule_ready_reset(self):
        if self._reset_job:
            self.after_cancel(self._reset_job)
        self._reset_job = self.after(READY_RESET_MS, self._reset_to_ready)

    def _reset_to_ready(self):
        self._reset_job = None
        if not self._sending:
            self.set_status("ready", "พร้อมสแกน", self._ready_hint())

    def _cancel_ready_reset(self):
        if self._reset_job:
            self.after_cancel(self._reset_job)
            self._reset_job = None

    # -----------------------------------------------------------------------
    # Tabs (iOS segmented control)
    # -----------------------------------------------------------------------

    def _build_tabs(self):

        self.tabs = SegTabs(
            self.page,
            (("scan", "สแกน"), ("settings", "ตั้งค่า"), ("log", "Log")),
            self.select_tab
        )

    def _is_scan_tab(self):
        return self._current_tab == "scan"

    def select_tab(self, key):
        if key == self._current_tab:
            return

        self._current_tab = key
        self.tabs.set_current(key)
        self._layout()
        self._on_tab_changed()

    def _on_tab_changed(self, _event=None):
        if self._is_scan_tab():
            self.scan_entry.focus_set()
        elif self.cam_dialog is not None:
            # never let the camera keep scanning/sending in the background
            self.close_camera_dialog()

    def _on_escape(self, _event=None):
        if self._is_scan_tab():
            self.clear_scan()

    def _on_f2(self, _event=None):
        self.select_tab("scan")
        self.scan_entry.focus_set()

    def _on_f5(self, _event=None):
        if self._is_scan_tab():
            self.resend_last()

    # -----------------------------------------------------------------------
    # Camera popup (opens only when the button is pressed, closes after scan)
    # -----------------------------------------------------------------------

    def _make_preview_image(self, frame_rgb=None, text=""):
        """Rounded PhotoImage of the preview area (dark, or camera frame)."""

        if self._preview_mask is None:
            self._preview_mask = _round_mask(PREVIEW_W, PREVIEW_H, 22)

        canvas = Image.new("RGB", (PREVIEW_W, PREVIEW_H), (11, 18, 32))

        if frame_rgb is not None:

            sw, sh = frame_rgb.size
            canvas.paste(frame_rgb, ((PREVIEW_W - sw) // 2,
                                     (PREVIEW_H - sh) // 2))

            # iOS-like scan guide: four white corner brackets
            d = ImageDraw.Draw(canvas)
            s = int(min(PREVIEW_W, PREVIEW_H) * 0.62)
            x0, y0 = (PREVIEW_W - s) // 2, (PREVIEW_H - s) // 2
            ln = int(s * 0.2)
            for cx, cy, dx, dy in (
                (x0, y0, 1, 1), (x0 + s, y0, -1, 1),
                (x0, y0 + s, 1, -1), (x0 + s, y0 + s, -1, -1),
            ):
                d.line((cx, cy, cx + dx * ln, cy), fill=(255, 255, 255),
                       width=4)
                d.line((cx, cy, cx, cy + dy * ln), fill=(255, 255, 255),
                       width=4)

        canvas = canvas.convert("RGBA")
        canvas.putalpha(self._preview_mask)
        return ImageTk.PhotoImage(canvas)

    def _show_preview(self, frame_rgb=None, text=""):
        self._preview_img = self._make_preview_image(frame_rgb)
        self.preview_label.configure(image=self._preview_img, text=text)

    def refresh_cameras(self):

        if not CAMERA_AVAILABLE:
            return

        was_running = self.camera is not None
        if was_running:
            self.stop_camera()

        self.set_status("busy", "กำลังค้นหากล้อง...", "")
        self.update_idletasks()

        self._camera_list = list_cameras()

        labels = [camera_label(i, n) for i, n in self._camera_list]

        if not self._camera_list:
            self._cam_pos = -1
            if self.cam_dialog is not None:
                self.cam_hint_var.set("ไม่พบกล้อง")
            self.set_status("error", "ไม่พบกล้อง",
                            "ตรวจสอบว่ากล้องเปิดใช้งานและไม่ถูกโปรแกรมอื่นใช้อยู่")
            self.log("[camera] ไม่พบกล้อง")
            return

        saved_name = self.cfg.get("camera_name", "")
        saved_index = self.cfg.get("camera_index", 0)

        pick = 0

        for pos, (i, n) in enumerate(self._camera_list):
            if saved_name and n == saved_name:
                pick = pos
                break
        else:
            for pos, (i, n) in enumerate(self._camera_list):
                if i == saved_index:
                    pick = pos
                    break

        self._cam_pos = pick

        if self.cam_dialog is not None:
            self._update_facing_buttons()

        self.log("[camera] พบกล้อง: " + ", ".join(labels))

        if not any(camera_facing(n) for _, n in self._camera_list):
            self.log(
                "[camera] ไม่ทราบว่าตัวไหนหน้า/หลัง — เลือกจากรายการ "
                "(ติดตั้ง pygrabber จะเห็นชื่อกล้องจริง)"
            )

        self.set_status("ready", "พร้อมสแกน", self._ready_hint())

        if was_running:
            self.start_camera()

    def _selected_camera(self):
        pos = self._cam_pos
        if pos < 0 or pos >= len(self._camera_list):
            return None
        return self._camera_list[pos]

    def _cam_dialog_parent(self):
        return self.cam_dialog if self.cam_dialog is not None else self

    def open_camera_dialog(self):
        """Pop the camera up in the middle of the screen and start scanning."""

        if not CAMERA_AVAILABLE:
            messagebox.showinfo(
                "ยังไม่ได้ติดตั้งไลบรารีกล้อง",
                "รันคำสั่ง: pip install opencv-python pillow\n\n"
                "(ยังใช้เครื่องสแกนเนอร์ได้ตามปกติ)"
            )
            return

        if (self._sending or self._testing or self._cam_pending
                or self.cam_dialog is not None):
            return

        self._cancel_ready_reset()

        if not self._camera_list:
            self.refresh_cameras()

        dlg = CameraDialog(self)
        self.cam_dialog = dlg

        dlg.title("สแกนด้วยกล้อง")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.protocol("WM_DELETE_WINDOW", self.close_camera_dialog)
        dlg.bind("<Escape>", lambda _e: self.close_camera_dialog())

        card = GlassCard(dlg, radius=30, pad=20)
        card.place(x=0, y=0, relwidth=1, relheight=1)
        f = card.inner

        top = tk.Frame(f, bg=C_GLASS)
        top.pack(fill="x", pady=(0, 12))

        tk.Label(top, text="สแกนด้วยกล้อง", bg=C_GLASS, fg=C_TEXT,
                 font=(FONT, 16, "bold")).pack(side="left")

        self.cam_hint_var = tk.StringVar(value="กำลังเปิดกล้อง...")
        tk.Label(top, textvariable=self.cam_hint_var, bg=C_GLASS,
                 fg=C_MUTED, font=(FONT, 10)).pack(side="right")

        self.preview_label = tk.Label(f, bd=0, bg=C_GLASS, compound="center",
                                      fg="#9ca3af", font=(FONT, 11))
        self.preview_label.pack(pady=(0, 14))
        self._show_preview(None, "กำลังเปิดกล้อง...")

        # only two choices: front camera / rear camera
        row = tk.Frame(f, bg=C_GLASS)
        row.pack(fill="x")

        self.front_btn = GlassButton(
            row, "กล้องหน้า", style="light", height=52, size=13,
            min_width=150,
            command=lambda: self.select_camera_by_facing("กล้องหน้า"))
        self.front_btn.pack(side="left")

        self.rear_btn = GlassButton(
            row, "กล้องหลัง", style="light", height=52, size=13,
            min_width=150,
            command=lambda: self.select_camera_by_facing("กล้องหลัง"))
        self.rear_btn.pack(side="left", padx=10)

        GlassButton(row, "ยกเลิก  (Esc)", style="danger", height=52,
                    size=12, command=self.close_camera_dialog
                    ).pack(side="right")

        self._update_facing_buttons()

        # size to content, then center on the screen
        dlg.update_idletasks()
        off = 2 * (card.m + card.pad)
        w = f.winfo_reqwidth() + off
        h = f.winfo_reqheight() + off
        x = max(0, (dlg.winfo_screenwidth() - w) // 2)
        y = max(0, (dlg.winfo_screenheight() - h) // 2)
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        dlg.lift()
        dlg.focus_force()

        try:
            dlg.wait_visibility()
            dlg.grab_set()          # modal: only the camera window is usable
        except tk.TclError:
            pass

        self.start_camera()

    def close_camera_dialog(self):
        """Stop the camera and remove the popup."""

        self.stop_camera()

        dlg = self.cam_dialog
        self.cam_dialog = None

        if dlg is not None:
            try:
                dlg.grab_release()
            except tk.TclError:
                pass
            dlg.destroy()

        if not self._sending and not self._cam_pending:
            self.set_status("ready", "พร้อมสแกน", self._ready_hint())

        self.scan_entry.focus_set()

    def _restart_camera(self):
        if self.cam_dialog is None:
            return
        self.stop_camera()
        self.start_camera()

    def _facing_of_pos(self, pos: int) -> str:
        """'กล้องหน้า' / 'กล้องหลัง' for a position in the camera list.

        Uses the device name when it says front/rear; when the names are
        generic (Camera 0, Camera 1 ...) the first camera counts as the
        front one and the second as the rear one.
        """
        if pos < 0 or pos >= len(self._camera_list):
            return ""
        name = self._camera_list[pos][1]
        facing = camera_facing(name)
        if facing:
            return facing
        if any(camera_facing(n) for _, n in self._camera_list):
            return ""
        return {0: "กล้องหน้า", 1: "กล้องหลัง"}.get(pos, "")

    def _pos_for_facing(self, facing: str):
        for pos in range(len(self._camera_list)):
            if self._facing_of_pos(pos) == facing:
                return pos
        return None

    def _update_facing_buttons(self):
        """Highlight the button of the camera that is currently selected."""
        if self.cam_dialog is None:
            return
        cur = self._facing_of_pos(self._cam_pos)
        self.front_btn.configure(
            style="blue" if cur == "กล้องหน้า" else "light")
        self.rear_btn.configure(
            style="blue" if cur == "กล้องหลัง" else "light")

    def select_camera_by_facing(self, facing: str):

        pos = self._pos_for_facing(facing)

        if pos is None:
            self.log(f"[camera] ไม่พบ{facing}")
            self.cam_hint_var.set(f"ไม่พบ{facing}")
            messagebox.showinfo(
                "ไม่พบกล้อง",
                f"ไม่พบ{facing}บนเครื่องนี้",
                parent=self._cam_dialog_parent()
            )
            return

        self._cam_pos = pos
        cam = self._camera_list[pos]
        self.cfg["camera_index"] = cam[0]
        self.cfg["camera_name"] = cam[1]
        save_config(self.cfg)

        self._update_facing_buttons()
        self._restart_camera()

    def start_camera(self):

        if self.camera is not None or self.cam_dialog is None:
            return

        cam = self._selected_camera()

        if cam is None:
            self.cam_hint_var.set("ไม่พบกล้อง")
            messagebox.showwarning("ไม่พบกล้อง",
                                   "ไม่พบกล้องบนเครื่องนี้",
                                   parent=self.cam_dialog)
            return

        # the capture mode that worked last time is tried first and the
        # others are tried automatically if it does not deliver a picture
        self.camera = CameraScanner(cam[0], self.cam_mode)
        self.camera.start()

        self._cam_pending = False

        self._show_preview(None, "กำลังเปิดกล้อง...")
        self.cam_hint_var.set("กำลังเปิดกล้อง...")

        self.set_status("busy", "กำลังเปิดกล้อง...", "")
        self.log(f"[camera] เปิดกล้อง: {camera_label(*cam)}")

        self._camera_tick()

    def stop_camera(self):

        if self._camera_job:
            self.after_cancel(self._camera_job)
            self._camera_job = None

        if self.camera is not None:
            self.camera.stop()
            self.camera = None

        self._preview_img = None

        if self.cam_dialog is not None:
            self._show_preview(None, "กล้องปิดอยู่")

    def _camera_tick(self):

        self._camera_job = None

        cam = self.camera
        if cam is None or self.cam_dialog is None:
            return

        if cam.error:

            msg = cam.error
            self.stop_camera()
            self.log(f"[camera] {msg}")
            self.cam_hint_var.set("กล้องมีปัญหา")
            self.set_status("error", "กล้องมีปัญหา",
                            msg.split("\n")[0])
            messagebox.showerror("กล้องมีปัญหา", msg,
                                 parent=self._cam_dialog_parent())
            return

        if cam.backend and not cam.announced:
            cam.announced = True
            self.log(f"[camera] เปิดสำเร็จ ({cam.backend})")

            if cam.profile is not None and cam.profile != self.cam_mode:
                self.cam_mode = cam.profile
                self.cfg["camera_mode"] = self.cam_mode
                save_config(self.cfg)

            self.cam_hint_var.set("ถือ QR ให้อยู่ในกรอบ")
            self.set_status("ready", "พร้อมสแกนด้วยกล้อง",
                            "ถือ QR ให้อยู่ในภาพ")

        frame = cam.get_frame()

        if frame is not None:

            h, w = frame.shape[:2]
            scale = min(PREVIEW_W / w, PREVIEW_H / h)

            small = cv2.resize(frame, (int(w * scale), int(h * scale)))
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)

            self._preview_img = self._make_preview_image(
                Image.fromarray(rgb))
            self.preview_label.configure(image=self._preview_img, text="")

        text = None

        try:
            while True:
                text = cam.results.get_nowait()
        except queue.Empty:
            pass

        if text and not self._sending and not self._cam_pending:
            # Got a code: close the camera right away (no camera left open).
            self._handle_camera_scan(text)
            return

        self._camera_job = self.after(40, self._camera_tick)

    def _handle_camera_scan(self, text: str):
        """Close the camera popup, show the value in the scan box, send it."""

        self.bell()
        self.log(f"[camera] สแกนได้ {len(text)} ตัว")

        if self._auto_send_job:
            self.after_cancel(self._auto_send_job)
            self._auto_send_job = None

        self._cancel_ready_reset()
        self._cam_pending = True

        self.close_camera_dialog()      # camera off + popup gone

        self.scan_var.set(text)
        self.scan_entry.icursor("end")
        self._update_progress()

        self.set_status("busy", "สแกนได้แล้ว",
                        f"{len(text)} ตัว • กำลังส่ง...")

        self.after(SCAN_SHOW_MS, self._camera_submit)

    def _camera_submit(self):
        self._cam_pending = False
        self.on_scan_submit()

    def _on_close(self):
        try:
            self.close_camera_dialog()
        except Exception:
            pass
        self.destroy()

    # -----------------------------------------------------------------------
    # Scan card
    # -----------------------------------------------------------------------

    def _expected(self):
        return len(self.cfg["module_numbers"]) * int(
            self.cfg["digits_per_field"]
        )

    def _build_scan_panel(self):

        self.scan_card = GlassCard(self.page, radius=28, pad=14)
        self._pages["scan"].append(self.scan_card)
        frame = self.scan_card.inner

        top = tk.Frame(frame, bg=C_GLASS)
        top.pack(fill="x")

        ttk.Label(top, text="สแกน QR / Barcode",
                  style="Title.TLabel").pack(side="left")

        self.count_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.count_var,
                  style="Muted.TLabel").pack(side="right")

        # pick the printer by NAME (no IP typing)
        self.scan_printer_var = tk.StringVar(value=self.cfg["printer_name"])
        self.scan_printer_combo = ttk.Combobox(
            top, textvariable=self.scan_printer_var, state="readonly",
            width=22, values=[p["name"] for p in self.cfg["printers"]])
        self.scan_printer_combo.pack(side="right", padx=(0, 16))
        ttk.Label(top, text="ส่งไปเครื่อง:",
                  style="Muted.TLabel").pack(side="right", padx=(0, 6))
        self.scan_printer_combo.bind("<<ComboboxSelected>>",
                                     self._on_scan_printer_selected)

        self.scan_var = tk.StringVar()

        self.entry_box = RoundBox(frame, radius=18, fill="#ffffff",
                                  outline=C_LINE, pad=10, height=54, ow=1)
        self.entry_box.pack(fill="x", pady=(8, 8))

        self.scan_entry = tk.Entry(
            self.entry_box.inner, textvariable=self.scan_var,
            font=(MONO, 20), bd=0, relief="flat", highlightthickness=0,
            bg="#ffffff", fg=C_TEXT, insertbackground=C_BLUE,
            selectbackground="#cfe3ff", selectforeground=C_TEXT
        )
        self.scan_entry.pack(fill="both", expand=True)

        self.scan_entry.bind("<Return>", self._on_scan_enter)
        self.scan_entry.bind("<KeyRelease>", self._on_scan_keyrelease)
        self.scan_entry.bind(
            "<FocusIn>",
            lambda _e: self.entry_box.set_style(outline=C_BLUE, ow=2))
        self.scan_entry.bind(
            "<FocusOut>",
            lambda _e: self.entry_box.set_style(outline=C_LINE, ow=1))

        self.progress = ProgressPill(frame, height=12)
        self.progress.pack(fill="x", pady=(0, 10))

        btn_row = tk.Frame(frame, bg=C_GLASS)
        btn_row.pack(fill="x")

        self.cam_btn = GlassButton(btn_row, "สแกนด้วยกล้อง", style="blue",
                                   height=44, size=12,
                                   command=self.open_camera_dialog)
        self.cam_btn.pack(side="left", padx=(0, 10))

        self.send_btn = GlassButton(btn_row, "ส่งทันที  (SEND)",
                                    style="primary", height=44, size=12,
                                    command=self.on_scan_submit)
        self.send_btn.pack(side="left", padx=(0, 10))

        GlassButton(btn_row, "ล้าง  (Esc)", style="light", height=44,
                    command=self.clear_scan).pack(side="left", padx=(0, 10))

        self.resend_btn = GlassButton(btn_row, "ส่งซ้ำล่าสุด  (F5)",
                                      style="light", height=44,
                                      command=self.resend_last)
        self.resend_btn.pack(side="left")
        self.resend_btn.configure(state="disabled")

    def _update_progress(self):
        expected = max(1, self._expected())
        value = clean_scanner_input(self.scan_var.get())
        n = len(value)

        self.progress.configure(maximum=expected, value=min(n, expected))

        if n == 0:
            self.count_var.set(f"รอสแกน • ต้องการ {expected} ตัว")
        else:
            self.count_var.set(f"{n} / {expected} ตัว")
            self._preview_split(value)

    def _preview_split(self, value: str):
        """Show how the text read so far will be split over the modules
        (nothing is sent). Field 1 = characters 1..N -> first module,
        field 2 = N+1..2N -> second module, and so on."""

        if self._sending or self._testing:
            return

        modules = list(self.cfg["module_numbers"])
        dpf = int(self.cfg["digits_per_field"])
        expected = len(modules) * dpf

        if not value or len(value) > expected:
            return

        padded = value + " " * (expected - len(value))
        chunks = [padded[i:i + dpf] for i in range(0, expected, dpf)]

        self._fill_tree(chunks, modules, "ตัวอย่าง", "preview")
        self.job_var.set("ตัวอย่างการแบ่งข้อมูล • ยังไม่ได้ส่ง")

    def _on_scan_enter(self, _event):
        self.on_scan_submit()
        return "break"

    def _on_scan_keyrelease(self, _event):

        if self._sending:
            return

        expected = self._expected()
        value = clean_scanner_input(self.scan_var.get())
        current_len = len(value)

        self._update_progress()

        if self._auto_send_job:
            self.after_cancel(self._auto_send_job)
            self._auto_send_job = None

        if current_len > expected:
            self.set_status(
                "error", "ข้อมูลยาวเกินกำหนด",
                f"ได้ {current_len} ตัว แต่ต้องการไม่เกิน {expected} ตัว "
                f"— กด Esc เพื่อล้างแล้วสแกนใหม่"
            )
            return

        if current_len == 0:
            return

        self._cancel_ready_reset()

        # Scanners type very fast; a short pause means the scan is done,
        # even if no trailing Enter arrives. A real Enter submits at once.
        debounce_ms = 80 if current_len == expected else 200

        self._auto_send_job = self.after(debounce_ms,
                                         self._auto_send_if_ready)

        if current_len == expected:
            self.set_status("busy", "อ่านข้อมูลครบแล้ว", "กำลังเตรียมส่ง...")
        else:
            trailing = len(value) - len(value.rstrip(" "))
            self.set_status(
                "busy", "กำลังอ่านข้อมูล...",
                f"อ่านแล้ว {current_len}/{expected} ตัว "
                f"(ข้อมูล {current_len - trailing} • Space {trailing}) "
                f"• จะเติม Space อีก {expected - current_len} ตัว"
            )

    def _auto_send_if_ready(self):
        self._auto_send_job = None
        if clean_scanner_input(self.scan_var.get()):
            self.on_scan_submit()

    def clear_scan(self):

        if self._auto_send_job:
            self.after_cancel(self._auto_send_job)
            self._auto_send_job = None

        self.scan_var.set("")
        self._update_progress()

        if not self._sending:
            self.set_status("ready", "พร้อมสแกน", self._ready_hint())

        self.scan_entry.focus_set()

    # -----------------------------------------------------------------------
    # Result card — per-field table of the last job
    # -----------------------------------------------------------------------

    def _build_result_panel(self):

        self.result_card = GlassCard(self.page, radius=28, pad=18)
        self._pages["scan"].append(self.result_card)
        frame = self.result_card.inner

        top = tk.Frame(frame, bg=C_GLASS)
        top.pack(fill="x")

        ttk.Label(top, text="ผลการส่งล่าสุด",
                  style="Title.TLabel").pack(side="left")

        self.job_var = tk.StringVar(value="ยังไม่มีการส่ง")
        ttk.Label(top, textvariable=self.job_var,
                  style="Muted.TLabel").pack(side="right")

        box = RoundBox(frame, radius=18, fill="#ffffff", outline=C_LINE,
                       pad=6)
        box.pack(fill="both", expand=True, pady=(10, 0))

        cols = ("field", "module", "range", "content", "result")

        self.tree = ttk.Treeview(box.inner, columns=cols, show="headings",
                                 height=5, style="Result.Treeview",
                                 selectmode="none")

        heads = (
            ("field", "Field", 64, "center", False),
            ("module", "Module", 84, "center", False),
            ("range", "ตัวอักษรที่", 110, "center", False),
            ("content", "ข้อมูลที่ส่ง", 260, "w", True),
            ("result", "ผลลัพธ์", 170, "center", False),
        )

        for key, text, width, anchor, stretch in heads:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=stretch, minwidth=50)

        self.tree.tag_configure("wait", background="#fff4d9")
        self.tree.tag_configure("ok", background="#e2f8e8")
        self.tree.tag_configure("ng", background="#ffe4e1")
        self.tree.tag_configure("skip", background="#f2f4f8",
                                foreground=C_MUTED)
        self.tree.tag_configure("preview", background="#e8f3fb",
                                foreground=C_BLUE)

        self.tree.pack(fill="both", expand=True)

    def _fill_tree(self, chunks, module_numbers,
                   result_text="กำลังส่ง...", tag="wait"):
        self.tree.delete(*self.tree.get_children())

        dpf = int(self.cfg["digits_per_field"])

        for i, chunk in enumerate(chunks):
            data = chunk.rstrip(" ")
            shown = data if data else "— (ว่าง) —"
            span = f"{i * dpf + 1}-{(i + 1) * dpf}"

            self.tree.insert(
                "", "end", iid=str(i),
                values=(i + 1, module_numbers[i], span, shown, result_text),
                tags=(tag,)
            )

    def _tree_set(self, i, text, tag):
        iid = str(i)
        if self.tree.exists(iid):
            self.tree.set(iid, "result", text)
            self.tree.item(iid, tags=(tag,))

    # -----------------------------------------------------------------------
    # Submit
    # -----------------------------------------------------------------------

    def on_scan_submit(self):

        if self._sending or self._testing:
            return

        payload = clean_scanner_input(self.scan_var.get())   # no .strip()

        self.scan_var.set("")
        self._update_progress()

        if not payload:
            return

        self._process_payload(payload)

    def resend_last(self):
        if self._sending or self._testing or not self._last_payload:
            return
        self.log("[resend] ส่งข้อมูลล่าสุดซ้ำ")
        self._process_payload(self._last_payload)

    def _process_payload(self, payload: str):

        self._cancel_ready_reset()

        module_numbers = list(self.cfg["module_numbers"])
        field_count = len(module_numbers)
        digits_per_field = int(self.cfg["digits_per_field"])
        expected_total = field_count * digits_per_field

        try:
            (
                chunks,
                actual_data_length,
                trailing_spaces,
                added_space_count
            ) = split_payload(payload, field_count, digits_per_field)

        except ValueError as e:

            self.log(f"[REJECTED] {e}")
            self.set_status("error", "ข้อมูลไม่ถูกต้อง — ยังไม่ได้ส่ง",
                            str(e))
            self.bell()
            self.scan_entry.focus_set()
            return

        self._last_payload = payload
        self.resend_btn.configure(state="normal")

        self._sending = True
        self.send_btn.configure(state="disabled")
        self.cam_btn.configure(state="disabled")
        self.resend_btn.configure(state="disabled")

        self.set_status(
            "busy", "กำลังส่งไปเครื่องพิมพ์...",
            f"{field_count} field • {self.cfg['printer_ip']}:"
            f"{self.cfg['printer_port']}"
        )

        self.job_var.set(
            f"{datetime.now().strftime('%H:%M:%S')} • "
            f"ข้อมูลจริง {actual_data_length} ตัว • "
            f"เติม Space {added_space_count} ตัว"
        )

        self._fill_tree(chunks, module_numbers)

        self.log(f"[scan] รับข้อมูล {len(payload)} ตัว")
        self.log(
            f"[padding] เติม Space ต่อท้าย {added_space_count} ตัว"
            if added_space_count > 0 else "[padding] ไม่ต้องเติม Space"
        )
        self.log(
            f"[validated] รวมทั้งหมด "
            f"{sum(len(c) for c in chunks)}/{expected_total} ตัว ✓"
        )
        self.log(f"       ข้อมูลจริง: {actual_data_length} ตัว")
        self.log(f"       Space padding รวม: {trailing_spaces} ตัว")
        self.log(f"       splitting into {field_count} fields × "
                 f"{digits_per_field} characters")

        for i, chunk in enumerate(chunks):
            sp = len(chunk) - len(chunk.rstrip(" "))
            self.log(
                f"       Field {i + 1} → Module {module_numbers[i]}: "
                f"{len(chunk)} chars (data={len(chunk) - sp}, space={sp})"
            )

        threading.Thread(
            target=self._send_worker,
            args=(chunks, module_numbers),
            daemon=True
        ).start()

    # -----------------------------------------------------------------------
    # Send worker / finish
    # -----------------------------------------------------------------------

    def _send_worker(self, chunks, module_numbers):

        results = []
        error = None

        try:
            results = send_fields(
                ip=self.cfg["printer_ip"],
                port=self.cfg["printer_port"],
                use_checksum=self.cfg["use_checksum"],
                module_numbers=module_numbers,
                chunks=chunks,
                code_system=self.cfg["code_system"],
                message_no=self.cfg["message_no"]
            )

        except (ConnectionRefusedError, TimeoutError, OSError) as e:
            error = f"เชื่อมต่อเครื่องพิมพ์ไม่ได้: {e}"

        except Exception as e:
            # Safety net: the UI must always unlock.
            error = f"เกิดข้อผิดพลาดไม่คาดคิด: {type(e).__name__}: {e}"

        self.after(0, self._send_finished, results, error, len(chunks))

    def _send_finished(self, results, error, total_fields):

        self._sending = False
        self.send_btn.configure(state="normal")
        self.cam_btn.configure(state="normal")
        self.resend_btn.configure(state="normal")

        for i in range(total_fields):
            if i < len(results):
                r = results[i]
                if r["ok"]:
                    self._tree_set(i, "✔ สำเร็จ", "ok")
                    self.log(f"  [OK] Module {r['module']}")
                else:
                    code = r["error_code"] or "NAK"
                    self._tree_set(i, f"✖ {code}", "ng")
                    self.log(f"  [NAK] Module {r['module']} "
                             f"— error code: {code}")
            else:
                self._tree_set(i, "ไม่ได้ส่ง", "skip")

        all_ok = (
            error is None
            and len(results) == total_fields
            and all(r["ok"] for r in results)
        )

        if all_ok:
            self.set_status(
                "ok", "ส่งสำเร็จ",
                f"ส่งครบ {total_fields} field • "
                f"{datetime.now().strftime('%H:%M:%S')} • พร้อมสแกนชิ้นต่อไป"
            )
            self.log("[done] ส่งข้อมูลครบทุก field แล้ว")
            self._schedule_ready_reset()
        else:
            if error:
                detail = error
            else:
                bad = [r for r in results if not r["ok"]]
                detail = "; ".join(
                    f"Module {r['module']}: {r['error_code'] or 'NAK'}" for r in bad
                ) or "มีบาง field ส่งไม่สำเร็จ"

            self.set_status(
                "error", "ส่งไม่สำเร็จ",
                detail + "  •  กด 'ส่งซ้ำล่าสุด' (F5) เพื่อลองใหม่"
            )
            self.log("[done] " + (error or "มีบาง field ส่งไม่สำเร็จ"))
            self.bell()

        self.scan_entry.focus_set()

    # -----------------------------------------------------------------------
    # Settings tab
    # -----------------------------------------------------------------------

    def _build_settings_panel(self):

        self.settings_card = GlassCard(self.page, radius=28, pad=20)
        self._pages["settings"].append(self.settings_card)
        frame = self.settings_card.inner

        ttk.Label(frame, text="ตั้งค่าเครื่องพิมพ์ / Field",
                  style="Title.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 10)
        )

        self.vars = {}

        def add_field(row, col, key, label, width=12):
            ttk.Label(frame, text=label).grid(
                row=row, column=col * 2, sticky="w", padx=(0, 8), pady=6
            )
            var = tk.StringVar(value=str(self.cfg[key]))
            ttk.Entry(frame, textvariable=var, width=width).grid(
                row=row, column=col * 2 + 1, sticky="ew",
                padx=(0, 20), pady=6
            )
            self.vars[key] = var

        # printer picked by NAME; IP / Port are shown read-only so they cannot
        # be mistyped. Names are managed in the "จัดการรายชื่อเครื่อง" dialog.
        ttk.Label(frame, text="เครื่องพิมพ์ (Name)").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=6)
        self.vars["printer_name"] = tk.StringVar(
            value=self.cfg["printer_name"])
        self.printer_combo = ttk.Combobox(
            frame, textvariable=self.vars["printer_name"], state="readonly",
            width=24, values=[p["name"] for p in self.cfg["printers"]])
        self.printer_combo.grid(row=1, column=1, sticky="ew", padx=(0, 20),
                                pady=6)
        self.printer_combo.bind("<<ComboboxSelected>>",
                                self._on_settings_printer_selected)
        GlassButton(frame, "จัดการรายชื่อเครื่อง", style="light", height=38,
                    size=11, command=self.open_printer_manager).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=6)

        def add_ro(row, col, key, label, width=12):
            ttk.Label(frame, text=label).grid(
                row=row, column=col * 2, sticky="w", padx=(0, 8), pady=6)
            var = tk.StringVar(value=str(self.cfg[key]))
            ttk.Entry(frame, textvariable=var, width=width,
                      state="readonly").grid(
                row=row, column=col * 2 + 1, sticky="ew", padx=(0, 20),
                pady=6)
            self.vars[key] = var

        add_ro(2, 0, "printer_ip", "Printer IP")
        add_ro(2, 1, "printer_port", "Port", 8)
        add_field(3, 0, "digits_per_field", "ตัวอักษร / field", 10)
        add_field(3, 1, "message_no", "Message no.", 8)

        ttk.Label(frame, text="Code system").grid(
            row=4, column=0, sticky="w", padx=(0, 8), pady=6
        )

        self.vars["code_system"] = tk.StringVar(value=self.cfg["code_system"])

        self.code_combo = ttk.Combobox(
            frame, textvariable=self.vars["code_system"],
            values=["UNICODE", "ASCII", "SHIFT_JIS"],
            width=14, state="readonly"
        )
        self.code_combo.grid(row=4, column=1, sticky="ew", padx=(0, 20),
                             pady=6)

        ttk.Label(frame, text="Module numbers").grid(
            row=5, column=0, sticky="w", padx=(0, 8), pady=6
        )

        self.vars["module_numbers"] = tk.StringVar(
            value=",".join(str(n) for n in self.cfg["module_numbers"])
        )

        self.module_entry = ttk.Entry(
            frame, textvariable=self.vars["module_numbers"], width=40
        )
        self.module_entry.grid(row=5, column=1, columnspan=3, sticky="ew",
                               padx=(0, 20), pady=6)

        ttk.Label(
            frame,
            text="เช่น 497,498,499,500 (เรียงตาม field 1,2,3,...)",
            style="Muted.TLabel"
        ).grid(row=6, column=1, columnspan=3, sticky="w")

        btns = tk.Frame(frame, bg=C_GLASS)
        btns.grid(row=7, column=0, columnspan=4, sticky="w", pady=(18, 0))

        GlassButton(btns, "บันทึกการตั้งค่า", style="blue", height=46,
                    command=self.on_save_settings).pack(side="left")

        self.test_btn = GlassButton(btns, "ทดสอบการเชื่อมต่อ", style="teal",
                                    height=46, command=self.test_connection)
        self.test_btn.pack(side="left", padx=10)

        self.test_result_var = tk.StringVar(
            value="กด 'ทดสอบการเชื่อมต่อ' เพื่อเชื่อมต่อเครื่องพิมพ์ แล้วส่ง"
                  "ข้อความทดสอบไปทุก Module")
        self.test_result_label = tk.Label(
            frame, textvariable=self.test_result_var, bg=C_GLASS,
            fg=C_MUTED, font=(FONT, 11), justify="left", anchor="w",
            wraplength=800)
        self.test_result_label.grid(row=8, column=0, columnspan=4,
                                    sticky="we", pady=(16, 0))

        frame.bind(
            "<Configure>",
            lambda e: self.test_result_label.configure(
                wraplength=max(300, e.width - 20)))

        for c in (1, 3):
            frame.columnconfigure(c, weight=1)

    # -- printer list (Name -> IP) ---------------------------------------------

    def _printer_by_name(self, name):
        return next((p for p in self.cfg["printers"] if p["name"] == name),
                    None)

    def _apply_printer(self, name):
        """Makes `name` the active printer (cfg + every selector + saved)."""

        p = self._printer_by_name(name)
        if p is None:
            return
        self.cfg["printer_name"] = p["name"]
        self.cfg["printer_ip"] = p["ip"]
        self.cfg["printer_port"] = p["port"]
        save_config(self.cfg)
        self._sync_printer_widgets()
        self._refresh_printer_label()

    def _sync_printer_widgets(self):
        names = [p["name"] for p in self.cfg["printers"]]
        self.scan_printer_combo.configure(values=names)
        self.printer_combo.configure(values=names)
        self.scan_printer_var.set(self.cfg["printer_name"])
        self.vars["printer_name"].set(self.cfg["printer_name"])
        self.vars["printer_ip"].set(self.cfg["printer_ip"])
        self.vars["printer_port"].set(str(self.cfg["printer_port"]))

    def _on_scan_printer_selected(self, _event=None):
        name = self.scan_printer_var.get()
        if self._sending or self._testing:
            # never switch the target while a send / test is running
            self.scan_printer_var.set(self.cfg["printer_name"])
            return
        self._apply_printer(name)
        p = self._printer_by_name(name)
        if p:
            self.log(f"[printer] เลือกเครื่อง {p['name']} "
                     f"({p['ip']}:{p['port']})")
        self.scan_entry.focus_set()

    def _on_settings_printer_selected(self, _event=None):
        # only fills IP / Port in the form; saved with "บันทึกการตั้งค่า"
        p = self._printer_by_name(self.vars["printer_name"].get())
        if p:
            self.vars["printer_ip"].set(p["ip"])
            self.vars["printer_port"].set(str(p["port"]))

    def open_printer_manager(self):
        """Dialog to add / edit / delete the Name -> IP list."""

        if getattr(self, "_pm_dialog", None) is not None:
            try:
                self._pm_dialog.lift()
                return
            except tk.TclError:
                self._pm_dialog = None

        dlg = tk.Toplevel(self)
        self._pm_dialog = dlg
        dlg.title("จัดการรายชื่อเครื่องพิมพ์ (Name → IP)")
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.configure(bg=C_GLASS)

        items = [dict(p) for p in self.cfg["printers"]]
        selected = {"i": None}

        tree = ttk.Treeview(dlg, columns=("name", "ip", "port"),
                            show="headings", height=8, selectmode="browse")
        for col, text, w in (("name", "ชื่อเครื่อง", 200),
                             ("ip", "IP", 140), ("port", "Port", 70)):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="w")
        tree.grid(row=0, column=0, columnspan=4, padx=14, pady=(14, 8))

        name_var, ip_var, port_var = (tk.StringVar(), tk.StringVar(),
                                      tk.StringVar(value="1024"))
        for r, (label, var, w) in enumerate((("ชื่อเครื่อง", name_var, 28),
                                             ("IP", ip_var, 28),
                                             ("Port", port_var, 10)), 1):
            ttk.Label(dlg, text=label).grid(row=r, column=0, sticky="w",
                                            padx=(14, 8), pady=3)
            ttk.Entry(dlg, textvariable=var, width=w).grid(
                row=r, column=1, columnspan=3, sticky="w", pady=3)

        def refresh():
            tree.delete(*tree.get_children())
            for i, p in enumerate(items):
                tree.insert("", "end", iid=str(i),
                            values=(p["name"], p["ip"], p["port"]))

        def on_pick(_e=None):
            sel = tree.selection()
            if not sel:
                return
            i = int(sel[0])
            selected["i"] = i
            name_var.set(items[i]["name"])
            ip_var.set(items[i]["ip"])
            port_var.set(str(items[i]["port"]))

        def others(skip=None):
            return [p["name"] for j, p in enumerate(items) if j != skip]

        def add():
            try:
                n, ip, port = validate_printer(
                    name_var.get(), ip_var.get(), port_var.get(), others())
            except ValueError as e:
                messagebox.showerror("ข้อมูลไม่ถูกต้อง", str(e), parent=dlg)
                return
            items.append({"name": n, "ip": ip, "port": port})
            selected["i"] = None
            name_var.set("")
            ip_var.set("")
            refresh()

        def update():
            i = selected["i"]
            if i is None or i >= len(items):
                messagebox.showinfo("แก้ไข", "เลือกเครื่องในรายการก่อน",
                                    parent=dlg)
                return
            try:
                n, ip, port = validate_printer(
                    name_var.get(), ip_var.get(), port_var.get(), others(i))
            except ValueError as e:
                messagebox.showerror("ข้อมูลไม่ถูกต้อง", str(e), parent=dlg)
                return
            items[i] = {"name": n, "ip": ip, "port": port}
            refresh()

        def delete():
            i = selected["i"]
            if i is None or i >= len(items):
                messagebox.showinfo("ลบ", "เลือกเครื่องในรายการก่อน",
                                    parent=dlg)
                return
            if len(items) <= 1:
                messagebox.showerror("ลบไม่ได้",
                                     "ต้องมีเครื่องอย่างน้อย 1 เครื่อง",
                                     parent=dlg)
                return
            if not messagebox.askyesno(
                    "ลบเครื่อง", f"ลบ '{items[i]['name']}' ออกจากรายการ?",
                    parent=dlg):
                return
            del items[i]
            selected["i"] = None
            refresh()

        def close():
            self._pm_dialog = None
            dlg.destroy()

        def save():
            if self._sending or self._testing:
                messagebox.showinfo("รอสักครู่",
                                    "กำลังส่ง/ทดสอบอยู่ ลองใหม่ภายหลัง",
                                    parent=dlg)
                return
            self.cfg["printers"] = [dict(p) for p in items]
            normalize_printers(self.cfg)
            save_config(self.cfg)
            self._sync_printer_widgets()
            self._refresh_printer_label()
            self.log(f"[printer] บันทึกรายชื่อเครื่อง {len(items)} เครื่อง "
                     f"• ใช้ {self.cfg['printer_name']}")
            close()

        tree.bind("<<TreeviewSelect>>", on_pick)
        refresh()

        row = tk.Frame(dlg, bg=C_GLASS)
        row.grid(row=4, column=0, columnspan=4, pady=(10, 4))
        ttk.Button(row, text="เพิ่ม", command=add).pack(side="left", padx=4)
        ttk.Button(row, text="แก้ไข", command=update).pack(side="left",
                                                           padx=4)
        ttk.Button(row, text="ลบ", command=delete).pack(side="left", padx=4)

        row2 = tk.Frame(dlg, bg=C_GLASS)
        row2.grid(row=5, column=0, columnspan=4, pady=(6, 14))
        ttk.Button(row2, text="บันทึกรายชื่อ", command=save).pack(
            side="left", padx=6)
        ttk.Button(row2, text="ยกเลิก", command=close).pack(side="left",
                                                           padx=6)

        dlg.protocol("WM_DELETE_WINDOW", close)
        dlg.grab_set()

    def _read_settings_form(self):
        """Validate the settings form (unsaved values included).
        Returns a dict, or None after showing an error message."""

        try:
            printer_ip = self.vars["printer_ip"].get().strip()
            printer_port = int(self.vars["printer_port"].get())
            digits_per_field = int(self.vars["digits_per_field"].get())
            message_no = int(self.vars["message_no"].get())
            code_system = self.vars["code_system"].get()
        except ValueError:
            messagebox.showerror(
                "ข้อมูลไม่ถูกต้อง",
                "Port / ตัวอักษรต่อ field / Message no. ต้องเป็นตัวเลข"
            )
            return None

        if not printer_ip:
            messagebox.showerror("ข้อมูลไม่ถูกต้อง", "กรุณาระบุ Printer IP")
            return None

        if not (1 <= printer_port <= 65535):
            messagebox.showerror("ข้อมูลไม่ถูกต้อง",
                                 "Port ต้องอยู่ระหว่าง 1-65535")
            return None

        if digits_per_field < 1:
            messagebox.showerror("ข้อมูลไม่ถูกต้อง",
                                 "ตัวอักษร / field ต้องมากกว่า 0")
            return None

        if not (0 <= message_no <= 999):
            messagebox.showerror(
                "หมายเลข message ไม่ถูกต้อง",
                "Message no. ต้องอยู่ระหว่าง 0-999 "
                "(คือเลขที่มุมซ้ายบนของหน้าจอเครื่องตอนสร้าง module เช่น 999)"
            )
            return None

        try:
            module_numbers = parse_module_numbers(
                self.vars["module_numbers"].get()
            )
        except ValueError as e:
            messagebox.showerror("หมายเลข module ไม่ถูกต้อง", str(e))
            return None

        return {
            "printer_name": self.vars["printer_name"].get(),
            "printer_ip": printer_ip,
            "printer_port": printer_port,
            "digits_per_field": digits_per_field,
            "message_no": message_no,
            "code_system": code_system,
            "module_numbers": module_numbers,
        }

    # -- connection test -----------------------------------------------------

    def _set_test_result(self, text, color):
        self.test_result_var.set(text)
        self.test_result_label.configure(fg=color)

    def test_connection(self):
        """Connect to the printer and, if that works, send a short test text
        to EVERY module in the form. Uses the values currently typed in the
        form (no need to save first)."""

        if self._sending or self._testing:
            return

        form = self._read_settings_form()
        if form is None:
            return

        modules = form["module_numbers"]
        modules_str = ", ".join(str(n) for n in modules)

        if not messagebox.askyesno(
            "ส่งข้อความทดสอบไปทุก Module",
            f"ระบบจะเชื่อมต่อ {form['printer_ip']}:{form['printer_port']} "
            f"และส่งข้อความทดสอบ (TEST M<เลข module>) ไปที่ Module\n"
            f"{modules_str}\n\n"
            "ข้อความใน Module เหล่านี้จะถูกแทนที่ด้วยข้อความทดสอบ "
            "(ข้อมูลจริงจะถูกเขียนทับเมื่อสแกนครั้งถัดไป)\n\n"
            "ต้องการดำเนินการต่อหรือไม่?"
        ):
            return

        self._testing = True
        self.test_btn.configure(state="disabled")
        self._cancel_ready_reset()

        self._set_test_result("กำลังเชื่อมต่อเครื่องพิมพ์...", C_MUTED)
        self.set_status("busy", "กำลังทดสอบการเชื่อมต่อ...",
                        f"{form['printer_ip']}:{form['printer_port']}")
        self.log(f"[test] เชื่อมต่อ {form['printer_ip']}:"
                 f"{form['printer_port']} • Module {modules_str}")

        threading.Thread(target=self._test_worker, args=(form,),
                         daemon=True).start()

    def _test_worker(self, form):

        digits = form["digits_per_field"]
        modules = form["module_numbers"]

        # one test text per module, padded to the same width as a real scan
        texts = [f"TEST M{n}".ljust(digits)[:digits] for n in modules]

        results, error = [], None

        try:
            results = send_fields(
                ip=form["printer_ip"],
                port=form["printer_port"],
                use_checksum=self.cfg["use_checksum"],
                module_numbers=modules,
                chunks=texts,
                code_system=form["code_system"],
                message_no=form["message_no"]
            )
        except (ConnectionRefusedError, TimeoutError, OSError) as e:
            # raised only while opening the connection
            error = f"เชื่อมต่อไม่ได้: {e}"
        except Exception as e:
            error = f"เกิดข้อผิดพลาดไม่คาดคิด: {type(e).__name__}: {e}"

        self.after(0, self._test_done, form, results, error)

    def _test_done(self, form, results, error):

        self._testing = False
        self.test_btn.configure(state="normal")

        modules = form["module_numbers"]
        target = f"{form['printer_ip']}:{form['printer_port']}"

        if error:
            self._set_test_result(f"✖ {error}\n{target}", "#a8201a")
            self.set_status("error", "เชื่อมต่อเครื่องพิมพ์ไม่ได้",
                            f"{target} — {error}")
            self.log(f"[test] {error}")
            self.bell()
            return

        by_module = {r["module"]: r for r in results}
        ok_n = sum(1 for r in results if r["ok"])
        parts = []

        for n in modules:
            r = by_module.get(n)
            if r is None:
                parts.append(f"{n} — ไม่ได้ส่ง")
            elif r["ok"]:
                parts.append(f"{n} ✔")
                self.log(f"  [test OK] Module {n}")
            else:
                code = r["error_code"] or "NAK"
                parts.append(f"{n} ✖ ({code})")
                self.log(f"  [test NAK] Module {n} — {code}")

        detail = "     ".join(parts)

        if ok_n == len(modules):
            self._set_test_result(
                f"✔ เชื่อมต่อได้ • ส่งข้อความทดสอบสำเร็จ {ok_n}/{len(modules)}"
                f" Module\nModule: {detail}", "#12713a")
            self.set_status("ok", "เชื่อมต่อและส่งข้อความทดสอบสำเร็จ",
                            f"{target} • ครบ {ok_n} Module")
            self.log(f"[test] สำเร็จ {ok_n}/{len(modules)} Module")
            self._schedule_ready_reset()
        else:
            self._set_test_result(
                f"✔ เชื่อมต่อได้ แต่ส่งข้อความทดสอบสำเร็จ "
                f"{ok_n}/{len(modules)} Module\nModule: {detail}", "#a8201a")
            self.set_status("error", "ส่งข้อความทดสอบไม่ครบทุก Module",
                            f"{target} • สำเร็จ {ok_n}/{len(modules)}")
            self.log(f"[test] สำเร็จ {ok_n}/{len(modules)} Module")
            self.bell()

    def on_save_settings(self):

        form = self._read_settings_form()
        if form is None:
            return

        self.cfg.update(form)
        normalize_printers(self.cfg)
        save_config(self.cfg)
        self._sync_printer_widgets()

        module_numbers = form["module_numbers"]
        digits_per_field = form["digits_per_field"]
        field_count = len(module_numbers)
        total = field_count * digits_per_field
        modules_str = ",".join(str(n) for n in module_numbers)

        self.log(
            f"[settings] saved. Message: {form['message_no']} • "
            f"Modules: {modules_str} ({field_count} field x "
            f"{digits_per_field} = {total} characters total)."
        )

        self._refresh_printer_label()
        self._update_progress()

        self.set_status(
            "ok", "บันทึกการตั้งค่าแล้ว",
            f"{field_count} module • ต้องการ {total} ตัว"
        )
        self._schedule_ready_reset()

    # -----------------------------------------------------------------------
    # Log tab
    # -----------------------------------------------------------------------

    def _build_log_panel(self):

        self.log_card = GlassCard(self.page, radius=28, pad=18)
        self._pages["log"].append(self.log_card)
        frame = self.log_card.inner

        ttk.Label(frame, text="Activity Log (บันทึกการทำงาน)",
                  style="Title.TLabel").pack(anchor="w", pady=(0, 8))

        box = RoundBox(frame, radius=18, fill="#fbfcfe", outline=C_LINE,
                       pad=8)
        box.pack(fill="both", expand=True)

        self.log_box = tk.Text(
            box.inner, bg="#fbfcfe", fg="#334155", insertbackground=C_BLUE,
            font=(MONO, 10), height=10, wrap="word", bd=0,
            highlightthickness=0, padx=10, pady=8
        )
        self.log_scroll = ttk.Scrollbar(box.inner, orient="vertical",
                                        command=self.log_box.yview,
                                        style="Glass.Vertical.TScrollbar")
        self.log_box.configure(yscrollcommand=self.log_scroll.set)
        self.log_scroll.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="both", expand=True)
        self.log_box.configure(state="disabled")

    def log(self, message: str):

        ts = datetime.now().strftime("%H:%M:%S")

        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    if not PIL_AVAILABLE:
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror(
            "ต้องติดตั้ง Pillow",
            "หน้าตาแอปแบบ Glass ต้องใช้ไลบรารี Pillow\n\n"
            "รันคำสั่ง:  pip install pillow"
        )
        sys.exit(1)

    app = App()
    app.mainloop()