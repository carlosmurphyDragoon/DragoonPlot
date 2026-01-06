#!/usr/bin/env python3
"""
DragoonPlot - Portable Serial Plotter with Real-Time Graphing
A lightweight, cross-platform serial plotter using DearPyGui.

Binary Protocol (no checksum):
    Data Frame:   [0xAA] [channel_count] [int16 x N]
                  Values: signed 16-bit integers, little-endian (-32768 to 32767)
    Label Frame:  [0xAB] [channel_count] [labels...]
                  Labels: [channel_idx] [len] [string bytes...]

Dependencies:
    pip install dearpygui pyserial
"""

import struct
import threading
import time
import json
import os
import subprocess
import sys
from pathlib import Path
from collections import deque
from typing import Optional, Union
from dataclasses import dataclass, field

import dearpygui.dearpygui as dpg
import serial
import serial.tools.list_ports


def get_linux_display_scale() -> float:
    """
    Detect the display scale factor on Linux.
    Tries multiple methods in order of reliability.
    """
    # Method 1: Check environment variables (set by some desktop environments)
    for env_var in ['GDK_SCALE', 'QT_SCALE_FACTOR', 'ELM_SCALE']:
        scale = os.environ.get(env_var)
        if scale:
            try:
                return float(scale)
            except ValueError:
                pass

    # Method 2: Query GNOME/Mutter for the current display scale via D-Bus
    try:
        result = subprocess.run(
            ['gdbus', 'call', '--session',
             '--dest', 'org.gnome.Mutter.DisplayConfig',
             '--object-path', '/org/gnome/Mutter/DisplayConfig',
             '--method', 'org.gnome.Mutter.DisplayConfig.GetCurrentState'],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            # Parse the output to find scale factors
            # The scale is in the logical monitors section, format: (x, y, scale, ...)
            import re
            # Look for the primary monitor's scale (the one with 'true' for is-primary)
            # Pattern matches: (x, y, scale, uint32 N, true, ...)
            matches = re.findall(r'\((\d+), (\d+), ([\d.]+), uint32 \d+, true,', result.stdout)
            if matches:
                return float(matches[0][2])
            # If no primary found, try to get any scale > 1
            matches = re.findall(r'\((\d+), (\d+), ([\d.]+), uint32 \d+,', result.stdout)
            for match in matches:
                scale = float(match[2])
                if scale > 1.0:
                    return scale
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass

    # Method 3: Check gsettings for text scaling factor
    try:
        result = subprocess.run(
            ['gsettings', 'get', 'org.gnome.desktop.interface', 'text-scaling-factor'],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            scale = float(result.stdout.strip())
            if scale > 1.0:
                return scale
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, Exception):
        pass

    # Method 4: Check Xft.dpi from xrdb (X11)
    try:
        result = subprocess.run(
            ['xrdb', '-query'],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'Xft.dpi' in line:
                    dpi = float(line.split(':')[1].strip())
                    # Standard DPI is 96, calculate scale
                    return dpi / 96.0
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, Exception):
        pass

    return 1.0


def get_windows_display_scale() -> float:
    """Detect the display scale factor on Windows."""
    try:
        import ctypes
        # Try to get DPI awareness context first
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Per-monitor aware
        except Exception:
            pass
        # Get the DPI for the primary monitor
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return dpi / 96.0
    except Exception:
        pass
    return 1.0


def get_display_scale() -> float:
    """Get display scale factor for the current platform."""
    if sys.platform == 'linux':
        return get_linux_display_scale()
    elif sys.platform == 'win32':
        return get_windows_display_scale()
    # macOS handles DPI scaling automatically
    return 1.0

# === Constants ===
START_DATA = 0xAA
START_LABEL = 0xAB
MAX_CHANNELS = 32
MAX_LABEL_LEN = 16
BUFFER_SIZE = 10000
DEFAULT_TIME_WINDOW = 10.0
CONFIG_FILE = Path.home() / ".dragoonplot.json"
BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
DEFAULT_COLORS = [
    (255, 87, 51),    # Red-orange
    (51, 255, 87),    # Green
    (51, 87, 255),    # Blue
    (255, 255, 51),   # Yellow
    (255, 51, 255),   # Magenta
    (51, 255, 255),   # Cyan
    (255, 153, 51),   # Orange
    (153, 51, 255),   # Purple
    (51, 255, 153),   # Mint
    (255, 51, 153),   # Pink
    (153, 255, 51),   # Lime
    (51, 153, 255),   # Sky blue
]


@dataclass
class ChannelConfig:
    name: str = ""
    color: tuple = (255, 255, 255)
    visible: bool = True
    scale: float = 1.0
    offset: float = 0.0


@dataclass
class CommandButton:
    label: str = "Cmd"
    data: str = ""
    mode: str = "ascii"  # "ascii" or "hex"


# Default commands from GMU-RTOS serial_terminal.c
DEFAULT_COMMAND_BUTTONS = [
    # State control
    CommandButton(label="Output", data="output\r\n", mode="ascii"),
    CommandButton(label="Idle", data="idle\r\n", mode="ascii"),
    CommandButton(label="Run", data="run\r\n", mode="ascii"),
    CommandButton(label="Reset", data="reset\r\n", mode="ascii"),
    CommandButton(label="Max", data="max\r\n", mode="ascii"),
    CommandButton(label="EV", data="ev\r\n", mode="ascii"),
    # Diagnostics
    CommandButton(label="Status", data="status\r\n", mode="ascii"),
    CommandButton(label="AP", data="ap\r\n", mode="ascii"),
    CommandButton(label="ECU", data="ecu\r\n", mode="ascii"),
    CommandButton(label="ADC", data="adc\r\n", mode="ascii"),
    CommandButton(label="CAN", data="can\r\n", mode="ascii"),
    CommandButton(label="UART", data="uart\r\n", mode="ascii"),
    CommandButton(label="Params", data="params\r\n", mode="ascii"),
    CommandButton(label="ESCs", data="escs\r\n", mode="ascii"),
    CommandButton(label="Perf", data="perf\r\n", mode="ascii"),
    CommandButton(label="Help", data="help\r\n", mode="ascii"),
]


@dataclass
class AppConfig:
    last_port: str = ""
    last_baud: int = 115200
    channels: list = field(default_factory=list)
    buttons: list = field(default_factory=list)
    time_window: float = DEFAULT_TIME_WINDOW

    def to_dict(self):
        return {
            "last_port": self.last_port,
            "last_baud": self.last_baud,
            "channels": [
                {"name": c.name, "color": list(c.color), "visible": c.visible,
                 "scale": c.scale, "offset": c.offset}
                for c in self.channels
            ],
            "buttons": [
                {"label": b.label, "data": b.data, "mode": b.mode}
                for b in self.buttons
            ],
            "time_window": self.time_window,
        }

    @classmethod
    def from_dict(cls, d):
        cfg = cls()
        cfg.last_port = d.get("last_port", "")
        cfg.last_baud = d.get("last_baud", 115200)
        cfg.channels = [
            ChannelConfig(
                name=c.get("name", ""),
                color=tuple(c.get("color", (255, 255, 255))),
                visible=c.get("visible", True),
                scale=c.get("scale", 1.0),
                offset=c.get("offset", 0.0),
            )
            for c in d.get("channels", [])
        ]
        cfg.buttons = [
            CommandButton(
                label=b.get("label", "Cmd"),
                data=b.get("data", ""),
                mode=b.get("mode", "ascii"),
            )
            for b in d.get("buttons", [])
        ]
        cfg.time_window = d.get("time_window", DEFAULT_TIME_WINDOW)
        return cfg


class BinaryProtocolParser:
    """
    State machine parser for binary protocol (no checksum).

    Data Frame:  [0xAA] [count] [int16 x N]
                 Values: signed 16-bit little-endian (-32768 to 32767)
    Label Frame: [0xAB] [count] [ch_idx, len, chars...] x N
    """

    def __init__(self, on_labels_callback=None):
        self.on_labels = on_labels_callback
        self.reset()

    def reset(self):
        self.state = "WAIT_START"
        self.frame_type = None
        self.channel_count = 0
        self.data_bytes = bytearray()
        self.expected_data_len = 0

    def feed(self, byte_val: int) -> Optional[list]:
        """Feed a byte. Returns parsed values for data frames, None otherwise."""
        if self.state == "WAIT_START":
            if byte_val == START_DATA:
                self.frame_type = "DATA"
                self.state = "READ_COUNT"
            elif byte_val == START_LABEL:
                self.frame_type = "LABEL"
                self.state = "READ_COUNT"
            return None

        elif self.state == "READ_COUNT":
            self.channel_count = byte_val
            if self.channel_count == 0 or self.channel_count > MAX_CHANNELS:
                self.reset()
                return None
            self.data_bytes = bytearray()

            if self.frame_type == "DATA":
                self.expected_data_len = self.channel_count * 2  # 2 bytes per int16
            else:  # LABEL - variable length
                self.expected_data_len = -1
            self.state = "READ_DATA"
            return None

        elif self.state == "READ_DATA":
            if self.frame_type == "DATA":
                self.data_bytes.append(byte_val)
                if len(self.data_bytes) >= self.expected_data_len:
                    # Frame complete - parse and return
                    values = []
                    for i in range(self.channel_count):
                        offset = i * 2
                        val = struct.unpack('<h', self.data_bytes[offset:offset + 2])[0]
                        values.append(val)
                    self.reset()
                    return values
            else:  # LABEL frame
                self.data_bytes.append(byte_val)
                if self._check_labels_complete():
                    labels = self._parse_labels()
                    if self.on_labels and labels:
                        self.on_labels(labels)
                    self.reset()
            return None

        return None

    def _check_labels_complete(self) -> bool:
        """Check if we have received all label data."""
        pos = 0
        labels_found = 0
        data = self.data_bytes

        while pos < len(data) and labels_found < self.channel_count:
            if pos + 2 > len(data):
                return False  # Need more data
            ch_idx = data[pos]
            str_len = data[pos + 1]
            if str_len > MAX_LABEL_LEN:
                self.reset()
                return False  # Invalid
            if pos + 2 + str_len > len(data):
                return False  # Need more data
            pos += 2 + str_len
            labels_found += 1

        return labels_found == self.channel_count

    def _parse_labels(self) -> dict:
        """Parse label data into {channel_idx: label_string}."""
        labels = {}
        pos = 0
        data = self.data_bytes

        while pos < len(data):
            if pos + 2 > len(data):
                break
            ch_idx = data[pos]
            str_len = data[pos + 1]
            if pos + 2 + str_len > len(data):
                break
            label = data[pos + 2:pos + 2 + str_len].decode('utf-8', errors='replace')
            labels[ch_idx] = label
            pos += 2 + str_len

        return labels


class SerialManager:
    """Threaded serial port manager."""

    def __init__(self, on_data_callback, on_labels_callback=None):
        self.port: Optional[serial.Serial] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.on_data = on_data_callback
        self.parser = BinaryProtocolParser(on_labels_callback)
        self.lock = threading.Lock()

    @staticmethod
    def list_ports() -> list:
        """List available serial ports."""
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports]

    def connect(self, port_name: str, baud_rate: int) -> bool:
        """Connect to serial port."""
        self.disconnect()
        try:
            self.port = serial.Serial(port_name, baud_rate, timeout=0.1)
            self.running = True
            self.parser.reset()
            self.thread = threading.Thread(target=self._read_loop, daemon=True)
            self.thread.start()
            return True
        except Exception as e:
            print(f"Connection error: {e}")
            return False

    def disconnect(self):
        """Disconnect from serial port."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)
            self.thread = None
        if self.port:
            try:
                self.port.close()
            except:
                pass
            self.port = None

    def is_connected(self) -> bool:
        return self.port is not None and self.port.is_open

    def send(self, data: bytes):
        """Send data to serial port."""
        if self.is_connected():
            try:
                with self.lock:
                    self.port.write(data)
            except Exception as e:
                print(f"Send error: {e}")

    def _read_loop(self):
        """Background thread for reading serial data."""
        bytes_received = 0
        frames_parsed = 0
        last_report = time.time()
        while self.running:
            try:
                if self.port and self.port.in_waiting:
                    with self.lock:
                        data = self.port.read(self.port.in_waiting)
                    bytes_received += len(data)
                    for byte_val in data:
                        result = self.parser.feed(byte_val)
                        if result is not None:
                            frames_parsed += 1
                            self.on_data(result)
                else:
                    time.sleep(0.001)
                # Report stats every 2 seconds
                now = time.time()
                if now - last_report >= 2.0:
                    if bytes_received > 0:
                        print(f"Serial: {bytes_received} bytes, {frames_parsed} frames parsed")
                    bytes_received = 0
                    frames_parsed = 0
                    last_report = now
            except Exception as e:
                if self.running:
                    print(f"Read error: {e}")
                break


class DataBuffer:
    """Circular buffer for channel data with timestamps."""

    def __init__(self, max_size: int = BUFFER_SIZE):
        self.max_size = max_size
        self.channels: dict[int, deque] = {}
        self.timestamps: dict[int, deque] = {}
        self.start_time = time.time()
        self.lock = threading.Lock()

    def add_sample(self, channel: int, value: float):
        with self.lock:
            if channel not in self.channels:
                self.channels[channel] = deque(maxlen=self.max_size)
                self.timestamps[channel] = deque(maxlen=self.max_size)
            t = time.time() - self.start_time
            self.channels[channel].append(value)
            self.timestamps[channel].append(t)

    def get_data(self, channel: int) -> tuple:
        with self.lock:
            if channel not in self.channels:
                return [], []
            return list(self.timestamps[channel]), list(self.channels[channel])

    def get_channel_count(self) -> int:
        with self.lock:
            return len(self.channels)

    def clear(self):
        with self.lock:
            self.channels.clear()
            self.timestamps.clear()
            self.start_time = time.time()


class DragoonPlotApp:
    """Main application class."""

    def __init__(self):
        self.config = self._load_config()
        self.data_buffer = DataBuffer()
        self.serial_manager = SerialManager(self._on_serial_data, self._on_labels)
        self.channel_configs: list[ChannelConfig] = list(self.config.channels)
        self.command_buttons: list[CommandButton] = list(self.config.buttons)
        self.time_window = self.config.time_window
        self.pending_labels: dict[int, str] = {}
        self.labels_updated = False
        self.ui_scale = 1.0  # Will be set properly in _setup_gui
        self._setup_gui()

    def _load_config(self) -> AppConfig:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = AppConfig.from_dict(json.load(f))
                    # Use default buttons if none saved
                    if not config.buttons:
                        config.buttons = [CommandButton(b.label, b.data, b.mode) for b in DEFAULT_COMMAND_BUTTONS]
                    return config
            except Exception as e:
                print(f"Error loading config: {e}")
        # Return default config with default command buttons
        config = AppConfig()
        config.buttons = [CommandButton(b.label, b.data, b.mode) for b in DEFAULT_COMMAND_BUTTONS]
        return config

    def _save_config(self):
        self.config.last_port = self._get_selected_port()
        self.config.last_baud = self._get_selected_baud()
        self.config.channels = list(self.channel_configs)
        self.config.buttons = list(self.command_buttons)
        self.config.time_window = self.time_window
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config.to_dict(), f, indent=2)
        except Exception as e:
            print(f"Error saving config: {e}")

    def _on_serial_data(self, values: list):
        """Callback for incoming serial data."""
        if not hasattr(self, '_data_frame_count'):
            self._data_frame_count = 0
        self._data_frame_count += 1
        if self._data_frame_count == 1:
            print(f"First data frame: {len(values)} channels, values[0:5]={values[0:5]}")
        for i, val in enumerate(values):
            self.data_buffer.add_sample(i, val)
            if i >= len(self.channel_configs):
                color = DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
                name = self.pending_labels.get(i, f"Ch{i}")
                self.channel_configs.append(ChannelConfig(
                    name=name,
                    color=color,
                    visible=True,
                ))

    def _on_labels(self, labels: dict):
        """Callback for incoming channel labels from MCU."""
        print(f"Received labels for {len(labels)} channels: {list(labels.values())[:5]}...")
        for ch_idx, label in labels.items():
            self.pending_labels[ch_idx] = label
            if ch_idx < len(self.channel_configs):
                self.channel_configs[ch_idx].name = label
                self.labels_updated = True

    def _get_selected_port(self) -> str:
        if dpg.does_item_exist("port_combo"):
            return dpg.get_value("port_combo") or ""
        return ""

    def _get_selected_baud(self) -> int:
        if dpg.does_item_exist("baud_combo"):
            val = dpg.get_value("baud_combo")
            return int(val) if val else 115200
        return 115200

    def _refresh_ports(self):
        ports = SerialManager.list_ports()
        if dpg.does_item_exist("port_combo"):
            dpg.configure_item("port_combo", items=ports)
            if ports and not dpg.get_value("port_combo"):
                dpg.set_value("port_combo", ports[0])

    def _toggle_connection(self):
        if self.serial_manager.is_connected():
            self.serial_manager.disconnect()
            dpg.set_value("connect_btn", "Connect")
            dpg.configure_item("status_text", default_value="Disconnected", color=(255, 100, 100))
        else:
            port = self._get_selected_port()
            baud = self._get_selected_baud()
            if port and self.serial_manager.connect(port, baud):
                dpg.set_value("connect_btn", "Disconnect")
                dpg.configure_item("status_text", default_value=f"Connected: {port}", color=(100, 255, 100))
            else:
                dpg.configure_item("status_text", default_value="Connection failed", color=(255, 100, 100))

    def _clear_data(self):
        self.data_buffer.clear()

    def _on_time_input(self, sender, value):
        """Handle manual time window input."""
        if value > 0:
            self.time_window = min(value, 300.0)
            dpg.set_value("time_slider", self.time_window)

    def _send_command(self, button: CommandButton):
        if button is None:
            print("Error: button is None")
            return
        if button.mode == "hex":
            try:
                hex_str = button.data.replace("0x", "").replace(" ", "").replace(",", "")
                data = bytes.fromhex(hex_str)
            except ValueError:
                print(f"Invalid hex: {button.data}")
                return
        else:
            data = button.data.encode('utf-8')
        self.serial_manager.send(data)

    def _add_command_button(self):
        btn = CommandButton(label=f"Btn{len(self.command_buttons)}", data="", mode="ascii")
        self.command_buttons.append(btn)
        self._rebuild_command_buttons()

    def _remove_command_button(self, idx: int):
        if 0 <= idx < len(self.command_buttons):
            self.command_buttons.pop(idx)
            self._rebuild_command_buttons()

    def _sz(self, value: int) -> int:
        """Scale a size value by the UI scale factor."""
        return int(value * self.ui_scale)

    def _rebuild_command_buttons(self):
        if dpg.does_item_exist("cmd_buttons_group"):
            dpg.delete_item("cmd_buttons_group", children_only=True)
            for i, btn in enumerate(self.command_buttons):
                with dpg.group(horizontal=True, parent="cmd_buttons_group"):
                    dpg.add_button(
                        label=btn.label,
                        callback=lambda s, a, u: self._send_command(u),
                        user_data=btn,
                        width=self._sz(80),
                    )
                    dpg.add_input_text(
                        default_value=btn.label,
                        width=self._sz(60),
                        callback=lambda s, a, u: setattr(u, 'label', a),
                        user_data=btn,
                        on_enter=True,
                        hint="Label",
                    )
                    dpg.add_input_text(
                        default_value=btn.data,
                        width=self._sz(100),
                        callback=lambda s, a, u: setattr(u, 'data', a),
                        user_data=btn,
                        on_enter=True,
                        hint="Data",
                    )
                    dpg.add_combo(
                        items=["ascii", "hex"],
                        default_value=btn.mode,
                        width=self._sz(60),
                        callback=lambda s, a, u: setattr(u, 'mode', a),
                        user_data=btn,
                    )
                    dpg.add_button(
                        label="X",
                        callback=lambda s, a, u: self._remove_command_button(u),
                        user_data=i,
                        width=self._sz(25),
                    )

    def _rebuild_channel_controls(self):
        if dpg.does_item_exist("channel_controls_group"):
            dpg.delete_item("channel_controls_group", children_only=True)
            for cfg in self.channel_configs:
                with dpg.group(horizontal=True, parent="channel_controls_group"):
                    dpg.add_checkbox(
                        default_value=cfg.visible,
                        callback=lambda s, a, c=cfg: setattr(c, 'visible', a),
                    )
                    dpg.add_color_edit(
                        default_value=(*cfg.color, 255),
                        callback=lambda s, a, c=cfg: setattr(c, 'color', (int(a[0]), int(a[1]), int(a[2]))),
                        no_alpha=True,
                        no_inputs=True,
                        width=self._sz(30),
                    )
                    dpg.add_input_text(
                        default_value=cfg.name,
                        width=self._sz(70),
                        callback=lambda s, a, c=cfg: setattr(c, 'name', a),
                        on_enter=True,
                    )
                    dpg.add_input_float(
                        default_value=cfg.scale,
                        width=self._sz(50),
                        callback=lambda s, a, c=cfg: setattr(c, 'scale', a),
                        format="%.2f",
                        step=0,
                    )
                    dpg.add_input_float(
                        default_value=cfg.offset,
                        width=self._sz(50),
                        callback=lambda s, a, c=cfg: setattr(c, 'offset', a),
                        format="%.1f",
                        step=0,
                    )

    def _create_splitter_theme(self):
        """Create a theme for the splitter bar."""
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (60, 60, 60, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (100, 100, 100, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (80, 80, 80, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 0)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 0, 0)
        return theme

    def _on_mouse_down(self, sender, app_data):
        """Track mouse down on splitter."""
        if self.splitter_hovered:
            self.splitter_dragging = True
            # Store the initial mouse Y and panel height when drag starts
            self.drag_start_mouse_y = dpg.get_mouse_pos(local=False)[1]
            self.drag_start_panel_height = self.bottom_panel_height

    def _on_mouse_release(self, sender, app_data):
        """Track mouse release."""
        self.splitter_dragging = False

    def _update_splitter(self):
        """Update splitter position based on current mouse position."""
        if not self.splitter_dragging:
            return

        # Get current mouse Y position
        current_mouse_y = dpg.get_mouse_pos(local=False)[1]

        # Calculate delta from drag start
        delta_y = current_mouse_y - self.drag_start_mouse_y

        # Calculate new height (dragging down = smaller bottom panel)
        new_height = self.drag_start_panel_height - delta_y

        # Clamp to reasonable bounds
        min_height = self._sz(100)
        max_height = dpg.get_viewport_height() - self._sz(150)
        new_height = max(min_height, min(new_height, max_height))

        self.bottom_panel_height = new_height

        # Update panel sizes
        dpg.configure_item("graph_panel", height=-int(new_height + self._sz(12)))
        dpg.configure_item("bottom_panel", height=int(new_height))

    def _setup_gui(self):
        dpg.create_context()

        # Detect and apply display scaling for HiDPI support
        self.ui_scale = get_display_scale()

        # Scale viewport size
        viewport_width = int(1200 * self.ui_scale)
        viewport_height = int(700 * self.ui_scale)
        dpg.create_viewport(title="DragoonPlot", width=viewport_width, height=viewport_height)

        # Apply global font scaling for HiDPI displays
        if self.ui_scale > 1.0:
            dpg.set_global_font_scale(self.ui_scale)

        # Helper to scale sizes
        def sz(value):
            return int(value * self.ui_scale)

        # Store initial bottom panel height for splitter
        self.bottom_panel_height = sz(170)
        self.splitter_hovered = False
        self.splitter_dragging = False
        self.drag_start_mouse_y = 0
        self.drag_start_panel_height = 0

        # Register global mouse handlers for splitter dragging
        with dpg.handler_registry(tag="global_handlers"):
            dpg.add_mouse_click_handler(button=0, callback=self._on_mouse_down)
            dpg.add_mouse_release_handler(button=0, callback=self._on_mouse_release)

        with dpg.window(tag="main_window"):
            # Top panel - Graph (takes most space)
            with dpg.child_window(tag="graph_panel", height=sz(-182)):
                with dpg.plot(
                    label="Serial Data",
                    tag="main_plot",
                    height=-1,
                    width=-1,
                    anti_aliased=True,
                ):
                    dpg.add_plot_legend()
                    dpg.add_plot_axis(dpg.mvXAxis, label="Seconds", tag="x_axis")
                    dpg.add_plot_axis(dpg.mvYAxis, label="Value", tag="y_axis")

            # Splitter bar - draggable divider
            dpg.add_button(tag="splitter_bar", label="", height=sz(10), width=-1)
            dpg.bind_item_theme("splitter_bar", self._create_splitter_theme())

            # Bottom panel - Controls
            with dpg.child_window(tag="bottom_panel", height=sz(170)):
                with dpg.group(horizontal=True):
                    # Connection section
                    with dpg.group(width=sz(220)):
                        dpg.add_text("Connection", color=(200, 200, 255))
                        with dpg.group(horizontal=True):
                            dpg.add_combo(
                                tag="port_combo",
                                items=[],
                                default_value=self.config.last_port,
                                width=sz(120),
                            )
                            dpg.add_button(label="R", callback=self._refresh_ports, width=sz(25))
                        dpg.add_combo(
                            tag="baud_combo",
                            items=[str(b) for b in BAUD_RATES],
                            default_value=str(self.config.last_baud),
                            width=sz(145),
                        )
                        with dpg.group(horizontal=True):
                            dpg.add_button(
                                label="Connect",
                                tag="connect_btn",
                                callback=self._toggle_connection,
                                width=sz(70),
                            )
                            dpg.add_button(label="Clear", callback=self._clear_data, width=sz(50))
                            dpg.add_button(label="Save", callback=self._save_config, width=sz(50))
                        dpg.add_text("Disconnected", tag="status_text", color=(255, 100, 100))

                    dpg.add_separator()

                    # Graph settings
                    with dpg.group(width=sz(150)):
                        dpg.add_text("X Axis Range", color=(200, 200, 255))
                        dpg.add_slider_float(
                            tag="time_slider",
                            default_value=self.time_window,
                            min_value=1.0,
                            max_value=300.0,
                            callback=lambda s, a: setattr(self, 'time_window', a),
                            width=sz(140),
                            format="%.0f sec",
                        )
                        dpg.add_input_float(
                            tag="time_input",
                            default_value=self.time_window,
                            width=sz(140),
                            callback=self._on_time_input,
                            format="%.1f",
                            step=1.0,
                        )

                    dpg.add_separator()

                    # Channels section
                    with dpg.group(width=sz(350)):
                        dpg.add_text("Channels (Vis|Color|Name|Scale|Offset)", color=(200, 200, 255))
                        with dpg.child_window(tag="channels_window", height=-1, width=sz(340)):
                            dpg.add_group(tag="channel_controls_group")

                    dpg.add_separator()

                    # Commands section
                    with dpg.group():
                        with dpg.group(horizontal=True):
                            dpg.add_text("Commands", color=(200, 200, 255))
                            dpg.add_button(label="+", callback=self._add_command_button, width=sz(25))
                        with dpg.child_window(tag="cmd_window", height=-1, width=-1):
                            dpg.add_group(tag="cmd_buttons_group")

        dpg.set_primary_window("main_window", True)
        dpg.setup_dearpygui()
        dpg.show_viewport()

        self._refresh_ports()
        self._rebuild_command_buttons()

        # Restore last port selection if available
        ports = SerialManager.list_ports()
        if self.config.last_port in ports:
            dpg.set_value("port_combo", self.config.last_port)

    def _update_plot(self):
        """Update plot with current data."""
        # X axis always starts at 0, ends at time_window
        # Data is shifted so newest point is at time_window
        dpg.set_axis_limits("x_axis", 0, self.time_window)

        # Update each channel
        num_channels = max(len(self.channel_configs), self.data_buffer.get_channel_count())

        for i in range(num_channels):
            series_tag = f"series_{i}"

            if i >= len(self.channel_configs):
                continue

            cfg = self.channel_configs[i]
            timestamps, values = self.data_buffer.get_data(i)

            # Shift timestamps so newest data is at time_window, oldest at 0
            # This makes the graph scroll with 0 always on the left
            current_time = time.time() - self.data_buffer.start_time
            timestamps = [self.time_window - (current_time - t) for t in timestamps]

            # Apply scale and offset
            if cfg.scale != 1.0 or cfg.offset != 0.0:
                values = [v * cfg.scale + cfg.offset for v in values]

            # Check if series exists
            if dpg.does_item_exist(series_tag):
                if cfg.visible and timestamps:
                    dpg.set_value(series_tag, [timestamps, values])
                    dpg.configure_item(series_tag, show=True, label=cfg.name or f"Ch{i}")
                else:
                    dpg.configure_item(series_tag, show=False)
            else:
                if timestamps and cfg.visible:
                    dpg.add_line_series(
                        timestamps,
                        values,
                        label=cfg.name or f"Ch{i}",
                        tag=series_tag,
                        parent="y_axis",
                    )
                    dpg.bind_item_theme(series_tag, self._create_line_theme(cfg.color))

    def _create_line_theme(self, color: tuple) -> str:
        """Create a theme for line color."""
        theme_tag = f"theme_{color[0]}_{color[1]}_{color[2]}"
        if not dpg.does_item_exist(theme_tag):
            with dpg.theme(tag=theme_tag):
                with dpg.theme_component(dpg.mvLineSeries):
                    dpg.add_theme_color(dpg.mvPlotCol_Line, (*color, 255), category=dpg.mvThemeCat_Plots)
        return theme_tag

    def run(self):
        """Main application loop."""
        last_channel_count = 0

        while dpg.is_dearpygui_running():
            self._update_plot()

            # Check if mouse is over splitter
            if dpg.does_item_exist("splitter_bar"):
                self.splitter_hovered = dpg.is_item_hovered("splitter_bar")

            # Update splitter position if dragging
            self._update_splitter()

            # Rebuild channel controls if new channels detected or labels updated
            current_count = len(self.channel_configs)
            if current_count != last_channel_count or self.labels_updated:
                self._rebuild_channel_controls()
                last_channel_count = current_count
                self.labels_updated = False

            dpg.render_dearpygui_frame()

        self.serial_manager.disconnect()
        self._save_config()
        dpg.destroy_context()


def main():
    app = DragoonPlotApp()
    app.run()


if __name__ == "__main__":
    main()
