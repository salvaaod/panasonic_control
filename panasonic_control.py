#!/usr/bin/env python3
"""
Panasonic AW camera Image Adjust controller
Dark Tkinter GUI with three tabs: Brightness, Picture, Matrix.

Designed from the Panasonic web GUI layout shown in the provided recording.
Uses the camera HTTP CGI interface:
  /cgi-bin/aw_cam?cmd=COMMAND&res=1
and polls:
  /live/camdata.html

Default camera:
  192.168.101.30

Dependencies:
  pip install requests
"""

import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from typing import Dict, List, Optional, Tuple

import requests  # type: ignore[import-untyped]
from requests.auth import HTTPBasicAuth, HTTPDigestAuth  # type: ignore[import-untyped]


# ============================================================
# Configuration
# ============================================================

CAMERA_IP = "192.168.103.30"
USERNAME = "admin"
PASSWORD = "Spain01@"

POLL_INTERVAL_MS = 200
HTTP_TIMEOUT = 3.0
SLIDER_SEND_DEBOUNCE_MS = 70
IGNORE_STALE_POLL_AFTER_LOCAL_CHANGE_SEC = 0.70

# If True, the log panel receives one line for every good poll.
LOG_SUCCESSFUL_POLLS = False


# ============================================================
# Conversion helpers
# ============================================================

def strip_0x(value: str) -> str:
    value = str(value).strip()
    if value.lower().startswith("0x"):
        value = value[2:]
    return value


def parse_hex_int(value: str) -> int:
    return int(strip_0x(value), 16)


def parse_mixed_int(value: str) -> int:
    value = str(value).strip()
    if value.lower().startswith("0x"):
        return int(value[2:], 16)
    # Panasonic replies sometimes use hex without 0x for camera commands.
    # For single digit codes, decimal and hex are the same.
    # If it contains A-F, treat as hex.
    if any(c in value.upper() for c in "ABCDEF"):
        return int(value, 16)
    return int(value, 10)


def hex_no_prefix(value: int, width: int = 2) -> str:
    return f"{int(value):0{width}X}"


def kelvin_to_hex(value: int) -> str:
    value = int(max(2000, min(15000, value)))
    return f"{value:05X}"


def now_str() -> str:
    return time.strftime("%H:%M:%S")


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


# ============================================================
# Camera HTTP client
# ============================================================

class CameraClient:
    def __init__(self, ip: str, username: str, password: str):
        self.ip = ip
        self.base = f"http://{ip}"
        self.username = username
        self.password = password
        self.auth = None
        self.auth_name = "none"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "AW-ImageAdjust-Tk/1.0",
            # The camera closes connections anyway; this makes the behavior explicit.
            "Connection": "close",
        })

    def request(self, path: str, params: Optional[dict] = None) -> dict:
        url = self.base + path
        start = time.monotonic()
        try:
            r = self.session.get(
                url,
                params=params,
                auth=self.auth,
                timeout=HTTP_TIMEOUT,
                allow_redirects=False,
            )

            if r.status_code == 401:
                www = r.headers.get("WWW-Authenticate", "").lower()
                if "digest" in www:
                    self.auth = HTTPDigestAuth(self.username, self.password)
                    self.auth_name = "digest"
                elif "basic" in www:
                    self.auth = HTTPBasicAuth(self.username, self.password)
                    self.auth_name = "basic"
                else:
                    self.auth = None
                    self.auth_name = "unknown"

                start = time.monotonic()
                r = self.session.get(
                    url,
                    params=params,
                    auth=self.auth,
                    timeout=HTTP_TIMEOUT,
                    allow_redirects=False,
                )

            elapsed_ms = (time.monotonic() - start) * 1000.0
            return {
                "ok": True,
                "url": r.url,
                "status": r.status_code,
                "reason": r.reason,
                "elapsed_ms": elapsed_ms,
                "headers": dict(r.headers),
                "body": r.text.strip(),
                "auth": self.auth_name,
            }
        except Exception as e:
            return {
                "ok": False,
                "url": url,
                "error": f"{type(e).__name__}: {e}",
            }

    def aw_cam(self, cmd: str) -> dict:
        return self.request("/cgi-bin/aw_cam", {"cmd": cmd, "res": "1"})

    def camdata(self) -> dict:
        return self.request("/live/camdata.html")


# ============================================================
# Parameter definitions
# ============================================================

@dataclass
class ControlDef:
    key: str
    label: str
    kind: str  # slider, radio, combo, button, section, note
    cmd: Optional[str] = None
    options: Dict[str, str] = field(default_factory=dict)
    min_value: int = 0
    max_value: int = 100
    step: int = 1
    suffix: str = ""
    center: Optional[int] = None
    width: int = 2
    value_type: str = "hex_offset"  # hex_offset, kelvin, direct_hex, direct_dec, gain_db, synchro, raw_hex
    command_suffix: str = ""
    command_override: Optional[str] = None
    enabled: bool = True
    help_text: str = ""

    def read_prefix(self) -> Optional[str]:
        return self.cmd

    def write_command(self, value) -> str:
        if self.kind == "button":
            return self.command_override or self.cmd or ""

        if self.kind in ("radio", "combo"):
            code = self.options[str(value)]
            return f"{self.cmd}:{code}{self.command_suffix}"

        if self.kind == "slider":
            v = int(round(float(value)))

            if self.value_type == "kelvin":
                encoded = kelvin_to_hex(v)
            elif self.value_type == "hex_offset":
                if self.center is None:
                    raise ValueError(f"center missing for {self.key}")
                encoded = hex_no_prefix(self.center + v, self.width)
            elif self.value_type == "direct_hex":
                encoded = hex_no_prefix(v, self.width)
            elif self.value_type == "raw_hex":
                encoded = hex_no_prefix(v, self.width)
            elif self.value_type == "direct_dec":
                encoded = str(v)
            elif self.value_type == "gain_db":
                # Observed OGU:0x1D displayed as 17 dB in the web GUI.
                encoded = hex_no_prefix(v + 12, self.width)
            elif self.value_type == "synchro":
                # Observed OSJ:09:0x001F4 displayed as 50.
                encoded = hex_no_prefix(v * 10, self.width)
            else:
                encoded = str(v)

            return f"{self.cmd}:{encoded}{self.command_suffix}"

        return ""

    def decode_value(self, value_text: str):
        """Decode camdata value into the value shown in UI."""
        token = value_text.split(":")[0].strip()

        if self.kind in ("radio", "combo"):
            # Try decimal/hex normalization and find matching label.
            raw_text = strip_0x(token).upper()
            for label, code in self.options.items():
                if strip_0x(code).upper() == raw_text:
                    return label
                try:
                    if parse_mixed_int(code) == parse_mixed_int(token):
                        return label
                except Exception:
                    pass
            return None

        if self.kind == "slider":
            if self.value_type == "kelvin":
                return parse_hex_int(token)
            if self.value_type == "hex_offset":
                return parse_hex_int(token) - int(self.center or 0)
            if self.value_type == "direct_hex":
                return parse_hex_int(token)
            if self.value_type == "raw_hex":
                return parse_hex_int(token)
            if self.value_type == "direct_dec":
                return int(token, 10)
            if self.value_type == "gain_db":
                return parse_hex_int(token) - 12
            if self.value_type == "synchro":
                return round(parse_hex_int(token) / 10)

        return None


# ============================================================
# UI control definitions
# ============================================================

# Codes are based on the Panasonic command style and values observed from your camera's
# /live/camdata.html and HAR captures. Some options vary by model/firmware; the app logs
# the exact command sent so mappings can be adjusted quickly if one option differs.

BRIGHTNESS: List[ControlDef] = [
    ControlDef("picture_level", "Picture Level", "slider", "OSD:48", min_value=-50, max_value=50, center=0x38, width=2),
    ControlDef("iris_mode", "Iris Mode", "radio", "ORS", options={"Manual": "0", "Auto": "1"}),
    ControlDef("auto_iris_speed", "Auto Iris Speed", "radio", "OSJ:01", options={"Slow": "1", "Normal": "2", "Fast": "3"}),
    ControlDef("auto_iris_window", "Auto Iris Window", "radio", "OSJ:02", options={"Normal1": "0", "Normal2": "1", "Center": "2"}),
    ControlDef("iris_limit", "Iris Limit", "radio", "OSJ:90", options={"Off": "0", "On": "1"}),
    ControlDef("shutter_mode", "Shutter Mode", "radio", "OSJ:03", options={"Off": "1", "Step": "2", "Synchro": "3", "ELC": "4"}),
    ControlDef("shutter_step", "Step", "combo", "OSJ:06", options={
        "1/24": "09C4", "1/25": "0960", "1/30": "07D0", "1/50": "04B0", "1/60": "03E8",
        "1/100": "0258", "1/120": "01F4", "1/250": "00F0", "1/500": "0078", "1/1000": "003C",
        "1/2000": "001E", "1/4000": "000F", "1/10000": "0006"
    }),
    ControlDef("synchro", "Synchro", "slider", "OSJ:09", min_value=30, max_value=250, step=1, value_type="synchro", width=5),
    ControlDef("elc_limit", "ELC Limit", "radio", "OSD:BF", options={"1/100": "5", "1/120": "6", "1/250": "7"}),
    ControlDef("gain", "Gain", "slider", "OGU", min_value=-3, max_value=30, step=1, suffix="dB", value_type="gain_db", width=2),
    ControlDef("gain_auto", "Gain Auto", "button", command_override="OGU:80"),
    ControlDef("super_gain", "Super Gain", "radio", "OSI:28", options={"Off": "0", "On": "1"}),
    ControlDef("agc_max_gain", "AGC Max Gain", "radio", "OSD:69", options={"6dB": "2", "12dB": "4", "18dB": "6"}),
    ControlDef("frame_mix", "Frame Mix", "combo", "OSA:65", options={"Off": "0", "6dB": "1", "12dB": "2", "18dB": "3", "24dB": "4"}),
    ControlDef("auto_fmix_max_gain", "Auto F.Mix Max Gain", "radio", "OSE:74", options={"0dB": "0", "6dB": "1", "12dB": "2", "18dB": "3"}),
    ControlDef("nd_filter", "ND Filter", "radio", "OSE:73", options={"Through": "0", "1/4": "1", "1/16": "2", "1/64": "3"}),
    ControlDef("day_night", "Day/Night", "radio", "ODT", options={"Day": "1", "Night": "0"}),
]

PICTURE: List[ControlDef] = [
    ControlDef("wb_mode", "White Balance Mode", "radio", "OAW", options={"ATW": "0", "AWB A": "1", "AWB B": "2", "3200K": "4", "5600K": "5", "VAR": "9"}),
    ControlDef("awb_execute", "AWB", "button", command_override="OWS"),
    ControlDef("var_color_temp", "Color Temperature", "slider", "OSI:20", min_value=2000, max_value=15000, step=10, suffix="K", value_type="kelvin", width=5, command_suffix=":0"),
    ControlDef("var_r_gain", "R Gain", "slider", "OSG:39", min_value=-200, max_value=200, step=1, center=0x800, width=3),
    ControlDef("var_b_gain", "B Gain", "slider", "OSG:3A", min_value=-200, max_value=200, step=1, center=0x800, width=3),
    ControlDef("section_cts", "Color Temperature Setting", "section"),
    ControlDef("awb_color_temp", "Color Temperature", "slider", "OSJ:4A", min_value=2000, max_value=15000, step=10, suffix="K", value_type="kelvin", width=5, command_suffix=":0"),
    ControlDef("awb_r_gain", "R Gain", "slider", "OSJ:4B", min_value=-400, max_value=400, step=1, center=0x800, width=3),
    ControlDef("awb_b_gain", "B Gain", "slider", "OSJ:4C", min_value=-400, max_value=400, step=1, center=0x800, width=3),
    ControlDef("awb_g_axis", "G Axis", "slider", "OSJ:4D", min_value=-400, max_value=400, step=1, center=0x800, width=3),
    ControlDef("awb_gain_offset", "AWB Gain Offset", "radio", "OSJ:0C", options={"Off": "0", "On": "1"}),
    ControlDef("atw_speed", "ATW Speed", "radio", "OSI:25", options={"Slow": "1", "Normal": "0", "Fast": "2"}),
    ControlDef("atw_target_r", "ATW Target R", "slider", "OSJ:0D", min_value=-50, max_value=50, step=1, center=0x80, width=2),
    ControlDef("atw_target_b", "ATW Target B", "slider", "OSJ:0E", min_value=-50, max_value=50, step=1, center=0x80, width=2),
    ControlDef("chroma_level", "Chroma Level", "slider", "OSD:B0", min_value=-31, max_value=31, step=1, suffix="%", center=0x80, width=2),
    ControlDef("chroma_phase", "Chroma Phase", "slider", "OSJ:0B", min_value=-31, max_value=31, step=1, center=0x80, width=2),
    ControlDef("abb_execute", "ABB", "button", command_override="OAS"),
    ControlDef("master_pedestal", "Master Pedestal", "slider", "OSJ:0F", min_value=-200, max_value=200, step=1, center=0x800, width=3),
    ControlDef("r_pedestal", "R Pedestal", "slider", "OSD:A1", min_value=-128, max_value=127, step=1, center=0x80, width=2),
    ControlDef("g_pedestal", "G Pedestal", "slider", "OSA:40", min_value=-128, max_value=127, step=1, center=0x80, width=2),
    ControlDef("b_pedestal", "B Pedestal", "slider", "OSD:A3", min_value=-128, max_value=127, step=1, center=0x80, width=2),
    ControlDef("pedestal_offset", "Pedestal Offset", "radio", "OSJ:D7", options={"Off": "0", "On": "1"}),
    ControlDef("detail", "Detail", "radio", "OSE:33", options={"Off": "0", "On": "1"}),
    ControlDef("master_detail", "Master Detail", "slider", "OSA:6A", min_value=-31, max_value=31, step=1, center=0x6C, width=2),
    ControlDef("v_detail_level", "V Detail Level", "slider", "OSE:33", min_value=-31, max_value=31, step=1, center=0x80, width=2, enabled=False, help_text="Model-dependent mapping"),
    ControlDef("detail_frequency", "Detail Frequency", "slider", "OSA:2D", min_value=-7, max_value=7, step=1, value_type="direct_dec"),
    ControlDef("level_depend", "Level Depend.", "slider", "OSA:2E", min_value=-7, max_value=7, step=1, value_type="direct_dec"),
    ControlDef("knee_aperture_level", "Knee Aperture Level", "slider", "OSA:2A", min_value=0, max_value=31, step=1, value_type="direct_hex", width=2),
    ControlDef("detail_gain_plus", "Detail Gain(+)", "slider", "OSD:3A", min_value=-31, max_value=31, step=1, center=0x02, width=2),
    ControlDef("detail_gain_minus", "Detail Gain(-)", "slider", "OSE:31", min_value=-31, max_value=31, step=1, center=0x00, width=2),
    ControlDef("skin_detail", "Skin Detail", "radio", "OSJ:4F", options={"Off": "0", "On": "1"}),
    ControlDef("skin_detail_effect", "Skin Detail Effect", "slider", "OSD:A4", min_value=0, max_value=31, step=1, value_type="direct_hex", width=2, enabled=False, help_text="Disabled here because OSD:A4 is used by Matrix R-G on this firmware."),
    ControlDef("gamma_mode", "Gamma Mode", "combo", "OSJ:1C", options={"HD": "0", "FILMLIKE1": "1", "FILMLIKE2": "2", "FILMLIKE3": "3", "VIDEO REC": "4", "HLG": "5"}),
    ControlDef("gamma", "Gamma", "slider", "OSJ:1D", min_value=30, max_value=75, step=1, value_type="direct_hex", width=2),
    ControlDef("black_gamma", "Black Gamma", "slider", "OSD:8C", min_value=-31, max_value=31, step=1, center=0x80, width=2),
    ControlDef("black_gamma_range", "Black Gamma Range", "slider", "OSD:8D", min_value=1, max_value=4, step=1, value_type="direct_hex", width=2),
    ControlDef("drs", "DRS", "radio", "OSD:8E", options={"Off": "0", "Low": "1", "Mid": "2", "High": "3"}),
    ControlDef("knee_mode", "Knee Mode", "radio", "OSD:8F", options={"Off": "0", "Auto": "1", "Manual": "2"}),
    ControlDef("knee_point", "Knee Point", "slider", "OSD:90", min_value=70, max_value=107, step=1, value_type="direct_hex", width=2),
    ControlDef("knee_slope", "Knee Slope", "slider", "OSD:91", min_value=0, max_value=99, step=1, value_type="direct_hex", width=2),
]

MATRIX: List[ControlDef] = [
    ControlDef("matrix_type", "Matrix Type", "combo", "OSJ:4F", options={"Normal": "0", "EBU": "1", "NTSC": "2", "User": "3"}),
    ControlDef("adaptive_matrix", "Adaptive Matrix", "radio", "OSE:31", options={"Off": "0", "On": "1"}),
    ControlDef("note_matrix", "*setting data which changed are reflected immediately", "note"),
    ControlDef("section_linear", "Matrix Settings", "section"),
    ControlDef("rg", "R-G", "slider", "OSD:A4", min_value=-63, max_value=63, step=1, center=0x80, width=2),
    ControlDef("rb", "R-B", "slider", "OSD:A5", min_value=-63, max_value=63, step=1, center=0x80, width=2),
    ControlDef("gr", "G-R", "slider", "OSD:A6", min_value=-63, max_value=63, step=1, center=0x80, width=2),
    ControlDef("gb", "G-B", "slider", "OSD:A7", min_value=-63, max_value=63, step=1, center=0x80, width=2),
    ControlDef("br", "B-R", "slider", "OSD:A8", min_value=-63, max_value=63, step=1, center=0x80, width=2),
    ControlDef("bg", "B-G", "slider", "OSD:A9", min_value=-63, max_value=63, step=1, center=0x80, width=2),
    ControlDef("section_cc", "Color Correction", "section"),
]

# Color-correction rows, saturation + phase. Query order observed in HAR:
# 80/81, 82/83, 84/85, 9A/9B, 86/87, 9C/9D, 88/89, 9E/9F,
# 8A/8B, JS:1C/1D, 8C/8D, 8E/8F, 90/91, 92/93, 94/95, 96/97.
COLOR_CORR_ROWS: List[Tuple[str, str, str, str]] = [
    ("B_MG", "OSD:80", "OSD:81", "b_mg"),
    ("Mg", "OSD:82", "OSD:83", "mg"),
    ("Mg_R", "OSD:84", "OSD:85", "mg_r"),
    ("Mg_R_R", "OSD:9A", "OSD:9B", "mg_r_r"),
    ("R", "OSD:86", "OSD:87", "r"),
    ("R_R_Yl", "OSD:9C", "OSD:9D", "r_r_yl"),
    ("R_Yl", "OSD:88", "OSD:89", "r_yl"),
    ("R_Yl_Yl", "OSD:9E", "OSD:9F", "r_yl_yl"),
    ("Yl", "OSD:8A", "OSD:8B", "yl"),
    ("Yl_Yl_G", "OSJ:1C", "OSJ:1D", "yl_yl_g"),
    ("Yl_G", "OSD:8C", "OSD:8D", "yl_g"),
    ("G", "OSD:8E", "OSD:8F", "g"),
    ("G_Cy", "OSD:90", "OSD:91", "g_cy"),
    ("Cy", "OSD:92", "OSD:93", "cy"),
    ("Cy_B", "OSD:94", "OSD:95", "cy_b"),
    ("Cy_B_B", "OSD:96", "OSD:97", "cy_b_b"),
]

for _cc_name, _sat_cmd, _phase_cmd, _cc_key in COLOR_CORR_ROWS:
    MATRIX.append(ControlDef(f"cc_{_cc_key}_sat", f"{_cc_name} Saturation", "slider", _sat_cmd, min_value=-63, max_value=63, step=1, center=0x80, width=2))
    MATRIX.append(ControlDef(f"cc_{_cc_key}_phase", f"{_cc_name} Phase", "slider", _phase_cmd, min_value=-63, max_value=63, step=1, center=0x80, width=2))

ALL_CONTROLS: List[ControlDef] = BRIGHTNESS + PICTURE + MATRIX
CONTROL_BY_KEY = {_control.key: _control for _control in ALL_CONTROLS if _control.kind not in ("section", "note")}
PREFIX_TO_CONTROLS: Dict[str, List[ControlDef]] = {}
for _control in CONTROL_BY_KEY.values():
    if _control.cmd:
        PREFIX_TO_CONTROLS.setdefault(_control.cmd, []).append(_control)


# ============================================================
# Scrollable frame
# ============================================================

class ScrollableFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, bg="#1f1f22")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_inner_configure(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self.inner_id, width=event.width)

    def _on_mousewheel(self, event):
        # Windows/macOS style. Linux wheel events still work through scrollbar.
        if self.winfo_containing(event.x_root, event.y_root) is not None:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


# ============================================================
# Main application
# ============================================================

class ImageAdjustApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Panasonic AW Image Adjust")
        self.root.geometry("980x830")
        self.root.minsize(820, 620)

        self.client = CameraClient(CAMERA_IP, USERNAME, PASSWORD)
        self.stop_event = threading.Event()
        self.send_queue: queue.Queue[Tuple[str, str]] = queue.Queue()

        self.connected_var = tk.StringVar(value="DISCONNECTED")
        self.model_var = tk.StringVar(value="Model: unknown")
        self.auth_var = tk.StringVar(value="Auth: unknown")
        self.poll_var = tk.StringVar(value="Last poll: never")
        self.error_var = tk.StringVar(value="No errors")
        self.log_success_polls_var = tk.BooleanVar(value=LOG_SUCCESSFUL_POLLS)

        self.tk_vars: Dict[str, tk.Variable] = {}
        self.value_labels: Dict[str, ttk.Label] = {}
        self.pending_after: Dict[str, str] = {}
        self.last_local_change: Dict[str, float] = {}
        self.updating_from_camera = False
        self.poll_counter = 0
        self.error_counter = 0

        self._style()
        self._build_ui()

        self.sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self.sender_thread.start()
        self.poller_thread = threading.Thread(target=self._poller_loop, daemon=True)
        self.poller_thread.start()

        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.log("App started")
        self.log(f"Camera IP: {CAMERA_IP}")
        self.log("Polling /live/camdata.html every 200 ms")

    # --------------------------------------------------------
    # Styling and UI
    # --------------------------------------------------------

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        bg = "#1f1f22"
        panel = "#252528"
        fg = "#dddddd"
        accent = "#9b9b9b"
        dark = "#111111"

        self.root.configure(bg=bg)
        style.configure("TFrame", background=bg)
        style.configure("Panel.TFrame", background=panel)
        style.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#444444", foreground="#eeeeee", font=("Segoe UI", 11))
        style.configure("Section.TLabel", background="#4a4a4a", foreground="#eeeeee", padding=(10, 4), font=("Segoe UI", 10))
        style.configure("Note.TLabel", background="#2a2a2d", foreground="#eeeeee", padding=(10, 4), font=("Segoe UI", 10, "italic"))
        style.configure("Value.TLabel", background=bg, foreground="#cccccc", font=("Segoe UI", 10))
        style.configure("Connected.TLabel", background=bg, foreground="#00c853", font=("Segoe UI", 11, "bold"))
        style.configure("Disconnected.TLabel", background=bg, foreground="#ff5252", font=("Segoe UI", 11, "bold"))
        style.configure("TButton", background="#3d3d40", foreground=fg, borderwidth=1, padding=(8, 4))
        style.map("TButton", background=[("active", "#555558")])
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("TRadiobutton", background=bg, foreground=fg)
        style.map("TRadiobutton", background=[("active", bg)], foreground=[("active", fg)])
        style.configure("TCombobox", fieldbackground="#4a4a4a", background="#4a4a4a", foreground=fg, arrowcolor=fg)
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure("TNotebook.Tab", background="#090909", foreground=fg, padding=(40, 6), borderwidth=1)
        style.map("TNotebook.Tab", background=[("selected", accent)], foreground=[("selected", "#111111")])
        style.configure("Horizontal.TScale", background=bg, troughcolor=dark, borderwidth=0)

    def _build_ui(self):
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill="both", expand=True)

        title = ttk.Label(main_frame, text="Image adjust", style="Title.TLabel", anchor="center")
        title.pack(fill="x")

        status = ttk.Frame(main_frame, padding=(10, 6))
        status.pack(fill="x")
        ttk.Label(status, text=f"Camera: http://{CAMERA_IP}").pack(side="left")
        ttk.Label(status, textvariable=self.model_var).pack(side="left", padx=(20, 0))
        ttk.Label(status, textvariable=self.auth_var).pack(side="left", padx=(20, 0))
        ttk.Label(status, textvariable=self.poll_var).pack(side="left", padx=(20, 0))
        self.conn_label = ttk.Label(status, textvariable=self.connected_var, style="Disconnected.TLabel")
        self.conn_label.pack(side="right")

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill="both", expand=True, padx=18, pady=(0, 6))

        self._add_tab("Brightness", BRIGHTNESS)
        self._add_tab("Picture", PICTURE)
        self._add_tab("Matrix", MATRIX, matrix=True)

        bottom = ttk.Frame(main_frame, padding=(10, 4))
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.error_var, foreground="#ff8a80").pack(side="left")
        ttk.Checkbutton(bottom, text="Log successful polls", variable=self.log_success_polls_var).pack(side="right", padx=8)
        ttk.Button(bottom, text="Clear log", command=self.clear_log).pack(side="right")

        log_frame = ttk.Frame(main_frame, padding=(10, 0, 10, 10))
        log_frame.pack(fill="both", expand=False)
        self.log_box = tk.Text(
            log_frame,
            height=7,
            bg="#101010",
            fg="#cfcfcf",
            insertbackground="#ffffff",
            relief="flat",
            font=("Consolas", 9),
            wrap="word",
        )
        self.log_box.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        sb.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=sb.set)

    def _add_tab(self, name: str, controls: List[ControlDef], matrix: bool = False):
        sf = ScrollableFrame(self.notebook)
        self.notebook.add(sf, text=name)

        if matrix:
            # Build custom matrix so color correction has two sliders per row.
            self._build_matrix_tab(sf.inner, controls)
        else:
            for c in controls:
                self._create_control_row(sf.inner, c)

    def _build_matrix_tab(self, parent, controls):
        # Build first part until color correction section.
        for c in controls:
            if c.key.startswith("cc_"):
                continue
            self._create_control_row(parent, c)

        # Color correction header with columns.
        header = ttk.Frame(parent, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 0))
        ttk.Label(header, text="Color", width=16, background="#4a4a4a").pack(side="left", padx=(28, 0))
        ttk.Label(header, text="Saturation", width=28, background="#4a4a4a", anchor="center").pack(side="left", padx=(4, 0), fill="x", expand=True)
        ttk.Label(header, text="Phase", width=28, background="#4a4a4a", anchor="center").pack(side="left", padx=(4, 18), fill="x", expand=True)

        for color_name, _sat_cmd, _phase_cmd, color_key in COLOR_CORR_ROWS:
            c_sat = CONTROL_BY_KEY[f"cc_{color_key}_sat"]
            c_phase = CONTROL_BY_KEY[f"cc_{color_key}_phase"]
            self._create_dual_slider_row(parent, color_name, c_sat, c_phase)

    def _create_control_row(self, parent, c: ControlDef):
        if c.kind == "section":
            ttk.Label(parent, text=c.label, style="Section.TLabel", anchor="w").pack(fill="x", pady=(12, 3))
            return
        if c.kind == "note":
            ttk.Label(parent, text=c.label, style="Note.TLabel", anchor="center").pack(fill="x", pady=(6, 6))
            return

        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=c.label, width=24, anchor="w").pack(side="left", padx=(12, 8))

        if c.kind == "slider":
            self._build_slider(row, c)
        elif c.kind == "radio":
            self._build_radio(row, c)
        elif c.kind == "combo":
            self._build_combo(row, c)
        elif c.kind == "button":
            ttk.Button(row, text="Execute" if "execute" in c.key else "Auto", command=lambda cc=c: self._send_button(cc)).pack(side="left")

        ttk.Separator(parent).pack(fill="x", padx=12, pady=(3, 0))

    def _create_dual_slider_row(self, parent, name: str, c1: ControlDef, c2: ControlDef):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=name, width=13, anchor="w").pack(side="left", padx=(28, 4))
        self._build_slider(row, c1, compact=True)
        self._build_slider(row, c2, compact=True)
        ttk.Separator(parent).pack(fill="x", padx=12, pady=(2, 0))

    def _build_slider(self, row, c: ControlDef, compact: bool = False):
        var = tk.IntVar(value=0)
        self.tk_vars[c.key] = var

        ttk.Button(row, text="-", width=3, command=lambda cc=c: self._step_slider(cc, -cc.step)).pack(side="left", padx=(0, 4))
        scale = ttk.Scale(row, from_=c.min_value, to=c.max_value, variable=var, command=lambda _v, cc=c: self._on_slider_changed(cc))
        scale.pack(side="left", fill="x", expand=True, padx=(0, 4))
        scale.bind("<ButtonRelease-1>", lambda _e, cc=c: self._flush_slider(cc))
        ttk.Button(row, text="+", width=3, command=lambda cc=c: self._step_slider(cc, cc.step)).pack(side="left", padx=(0, 6))
        value_label = ttk.Label(row, text="0", width=8 if not compact else 5, style="Value.TLabel", anchor="center")
        value_label.pack(side="left", padx=(0, 4))
        self.value_labels[c.key] = value_label

        if not c.enabled:
            for child in row.winfo_children()[1:]:
                try:
                    child.configure(state="disabled")
                except tk.TclError:
                    pass
            if c.help_text:
                value_label.configure(text="disabled")

    def _build_radio(self, row, c: ControlDef):
        var = tk.StringVar(value=next(iter(c.options.keys())))
        self.tk_vars[c.key] = var
        for label in c.options.keys():
            ttk.Radiobutton(row, text=label, variable=var, value=label, command=lambda cc=c: self._on_choice_changed(cc)).pack(side="left", padx=7)

    def _build_combo(self, row, c: ControlDef):
        var = tk.StringVar(value=next(iter(c.options.keys())))
        self.tk_vars[c.key] = var
        combo = ttk.Combobox(row, textvariable=var, values=list(c.options.keys()), state="readonly", width=18)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _e, cc=c: self._on_choice_changed(cc))

    # --------------------------------------------------------
    # User event handlers
    # --------------------------------------------------------

    def _step_slider(self, c: ControlDef, delta: int):
        var = self.tk_vars[c.key]
        new_v = clamp(int(round(float(var.get()))) + delta, c.min_value, c.max_value)
        var.set(new_v)
        self._update_value_label(c)
        self._mark_local(c.key)
        self._flush_slider(c)

    def _on_slider_changed(self, c: ControlDef):
        if self.updating_from_camera or not c.enabled:
            return
        var = self.tk_vars[c.key]
        v = clamp(int(round(float(var.get()))), c.min_value, c.max_value)
        var.set(v)
        self._update_value_label(c)
        self._mark_local(c.key)
        if c.key in self.pending_after:
            try:
                self.root.after_cancel(self.pending_after[c.key])
            except tk.TclError:
                pass
        self.pending_after[c.key] = self.root.after(SLIDER_SEND_DEBOUNCE_MS, lambda cc=c: self._flush_slider(cc))

    def _flush_slider(self, c: ControlDef):
        if self.updating_from_camera or not c.enabled:
            return
        if c.key in self.pending_after:
            try:
                self.root.after_cancel(self.pending_after[c.key])
            except tk.TclError:
                pass
            self.pending_after.pop(c.key, None)
        value = self.tk_vars[c.key].get()
        self._enqueue(c.key, c.write_command(value))

    def _on_choice_changed(self, c: ControlDef):
        if self.updating_from_camera or not c.enabled:
            return
        value = self.tk_vars[c.key].get()
        self._mark_local(c.key)
        self._enqueue(c.key, c.write_command(value))

    def _send_button(self, c: ControlDef):
        cmd = c.write_command(None)
        self._enqueue(c.key, cmd)

    # --------------------------------------------------------
    # Communication threads
    # --------------------------------------------------------

    def _enqueue(self, key: str, cmd: str):
        if not cmd:
            return
        self.send_queue.put((key, cmd))
        self.log(f"QUEUE {cmd}")

    def _sender_loop(self):
        while not self.stop_event.is_set():
            try:
                key, cmd = self.send_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            latest = {key: cmd}
            while True:
                try:
                    queued_key, queued_cmd = self.send_queue.get_nowait()
                    latest[queued_key] = queued_cmd
                except queue.Empty:
                    break

            for control_key, command_text in latest.items():
                result = self.client.aw_cam(command_text)
                self.root.after(0, self._handle_send_result, control_key, command_text, result)

    def _poller_loop(self):
        while not self.stop_event.is_set():
            start = time.monotonic()
            result = self.client.camdata()
            self.root.after(0, self._handle_poll_result, result)
            elapsed = time.monotonic() - start
            sleep_s = max(0.02, POLL_INTERVAL_MS / 1000.0 - elapsed)
            self.stop_event.wait(sleep_s)

    def _handle_send_result(self, key: str, cmd: str, result: dict):
        if result.get("ok") and result.get("status") == 200:
            self._set_connected(True, result)
            body = result.get("body", "")
            self.log(f"SEND OK {cmd} | {result.get('elapsed_ms', 0):.1f} ms | {body!r}")
        else:
            self.error_counter += 1
            err = self._format_error(result)
            self.error_var.set(err)
            self.log(f"SEND ERROR {cmd} | {err}")
            self._set_connected(False, result)

    def _handle_poll_result(self, result: dict):
        self.poll_counter += 1
        if result.get("ok") and result.get("status") == 200:
            self._set_connected(True, result)
            elapsed = result.get("elapsed_ms", 0.0)
            self.poll_var.set(f"Last poll: {elapsed:.1f} ms")
            self.auth_var.set(f"Auth: {result.get('auth', 'unknown')}")
            self.error_var.set("No errors")
            state = self._parse_camdata(result.get("body", ""))
            self._apply_state(state)
            if self.log_success_polls_var.get():
                self.log(f"POLL OK {elapsed:.1f} ms | {len(state)} values")
        else:
            self.error_counter += 1
            err = self._format_error(result)
            self.error_var.set(err)
            self.log(f"POLL ERROR | {err}")
            self._set_connected(False, result)

    # --------------------------------------------------------
    # Camdata parsing and UI update
    # --------------------------------------------------------

    def _parse_camdata(self, text: str) -> Dict[str, str]:
        raw: Dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            parts = line.split(":")
            if len(parts) >= 2:
                # Command prefixes can be OAW, ORS, OSD:A4, OSJ:0F, etc.
                if parts[0] in {"OSD", "OSJ", "OSI", "OSG", "OSA", "OSE", "QSD", "QSJ", "QSI", "QSG", "QSA", "QSE"} and len(parts) >= 3:
                    prefix = f"{parts[0]}:{parts[1]}"
                    value = ":".join(parts[2:])
                else:
                    prefix = parts[0]
                    value = ":".join(parts[1:])
                raw[prefix] = value

        if "OID" in raw:
            self.model_var.set(f"Model: {raw['OID']}")
        return raw

    def _apply_state(self, raw: Dict[str, str]):
        self.updating_from_camera = True
        try:
            for prefix, value_text in raw.items():
                controls = PREFIX_TO_CONTROLS.get(prefix, [])
                for c in controls:
                    if c.key not in self.tk_vars:
                        continue
                    if self._ignore_camera_update(c.key):
                        continue
                    try:
                        decoded = c.decode_value(value_text)
                    except Exception:
                        continue
                    if decoded is None:
                        continue
                    var = self.tk_vars[c.key]
                    if isinstance(var, tk.IntVar):
                        decoded = clamp(int(decoded), c.min_value, c.max_value)
                    var.set(decoded)
                    if c.kind == "slider":
                        self._update_value_label(c)
        finally:
            self.updating_from_camera = False

    def _update_value_label(self, c: ControlDef):
        label = self.value_labels.get(c.key)
        if not label:
            return
        v = int(round(float(self.tk_vars[c.key].get())))
        # Official GUI shows color level as percent for Chroma Level.
        label.configure(text=f"{v}{c.suffix}")

    def _mark_local(self, key: str):
        self.last_local_change[key] = time.monotonic()

    def _ignore_camera_update(self, key: str) -> bool:
        t = self.last_local_change.get(key)
        if t is None:
            return False
        return (time.monotonic() - t) < IGNORE_STALE_POLL_AFTER_LOCAL_CHANGE_SEC

    # --------------------------------------------------------
    # Status/log helpers
    # --------------------------------------------------------

    def _set_connected(self, connected: bool, result: Optional[dict] = None):
        if connected:
            self.connected_var.set("CONNECTED")
            self.conn_label.configure(style="Connected.TLabel")
        else:
            self.connected_var.set("DISCONNECTED")
            self.conn_label.configure(style="Disconnected.TLabel")
        if result and result.get("ok"):
            self.auth_var.set(f"Auth: {result.get('auth', 'unknown')}")

    def _format_error(self, result: dict) -> str:
        if not result.get("ok"):
            return result.get("error", "Unknown connection error")
        return f"HTTP {result.get('status')} {result.get('reason')} body={result.get('body', '')[:120]!r}"

    def log(self, text: str):
        line = f"[{now_str()}] {text}"
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        print(line)

    def clear_log(self):
        self.log_box.delete("1.0", "end")

    def _close(self):
        self.stop_event.set()
        self.root.after(200, self.root.destroy)


def main():
    root = tk.Tk()
    ImageAdjustApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

