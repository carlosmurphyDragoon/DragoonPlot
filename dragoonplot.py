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


def get_dfu_util_path() -> str:
    """Get path to dfu-util executable (bundled on Windows, system on Linux)."""
    if sys.platform == 'win32':
        if getattr(sys, 'frozen', False):
            # Running as PyInstaller bundle on Windows
            return os.path.join(sys._MEIPASS, 'dfu-util.exe')
        else:
            # Running as script on Windows - look in same directory
            local_path = os.path.join(os.path.dirname(__file__), 'dfu-util.exe')
            if os.path.exists(local_path):
                return local_path
    # Linux/Mac: use system dfu-util
    return 'dfu-util'


def get_resource_path(relative_path: str) -> str:
    """Get path to bundled resource (works in both dev and PyInstaller bundle)."""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        base_path = sys._MEIPASS
    else:
        # Running as script
        base_path = os.path.dirname(__file__)
    return os.path.join(base_path, relative_path)


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
    category: str = ""  # Category from help output (state, diag, param, sys)


# Default commands (empty - use Discover to populate from device)
DEFAULT_COMMAND_BUTTONS = []


@dataclass
class AppConfig:
    last_port: str = ""
    last_baud: int = 115200
    channels: list = field(default_factory=list)
    buttons: list = field(default_factory=list)
    time_window: float = DEFAULT_TIME_WINDOW
    dfu_file_path: str = ""

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
                {"label": b.label, "data": b.data, "mode": b.mode, "category": b.category}
                for b in self.buttons
            ],
            "time_window": self.time_window,
            "dfu_file_path": self.dfu_file_path,
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
                category=b.get("category", ""),
            )
            for b in d.get("buttons", [])
        ]
        cfg.time_window = d.get("time_window", DEFAULT_TIME_WINDOW)
        cfg.dfu_file_path = d.get("dfu_file_path", "")
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
        self.frame_start_time = 0

    def check_timeout(self):
        """Reset parser if frame takes too long (protects against false starts)."""
        if self.state != "WAIT_START" and self.frame_start_time > 0:
            if time.time() - self.frame_start_time > 0.1:  # 100ms timeout
                self.reset()
                return True
        return False

    def feed(self, byte_val: int) -> Optional[list]:
        """Feed a byte. Returns parsed values for data frames, None otherwise."""
        # Check for frame timeout
        self.check_timeout()

        if self.state == "WAIT_START":
            if byte_val == START_DATA:
                self.frame_type = "DATA"
                self.state = "READ_COUNT"
                self.frame_start_time = time.time()
            elif byte_val == START_LABEL:
                self.frame_type = "LABEL"
                self.state = "READ_COUNT"
                self.frame_start_time = time.time()
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

    def __init__(self, on_data_callback, on_labels_callback=None, on_text_callback=None):
        self.port: Optional[serial.Serial] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.on_data = on_data_callback
        self.on_text = on_text_callback
        self.parser = BinaryProtocolParser(on_labels_callback)
        self.lock = threading.Lock()
        self.text_buffer = bytearray()

    @staticmethod
    def list_ports() -> list:
        """List available serial ports."""
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports]

    def connect(self, port_name: str, baud_rate: int) -> bool:
        """Connect to serial port."""
        self.disconnect()
        try:
            # Use larger read buffer and disable flow control for USB CDC
            self.port = serial.Serial(
                port_name,
                baud_rate,
                timeout=0.05,
                write_timeout=1.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False
            )
            # Set RTS high to signal ready-to-receive (important for some USB CDC)
            self.port.rts = True
            self.port.dtr = True
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
                if not self.port:
                    time.sleep(0.01)
                    continue

                # Read available data - use read(1) with timeout as fallback
                # This helps with USB CDC flow control
                waiting = self.port.in_waiting
                if waiting > 0:
                    data = self.port.read(waiting)
                else:
                    # Do a blocking read with short timeout to trigger USB polling
                    data = self.port.read(1)
                    if not data:
                        continue

                bytes_received += len(data)

                for byte_val in data:
                    # Always try to collect printable ASCII as text first
                    # This runs in parallel with binary parsing
                    if self.on_text:
                        if byte_val == 0x0A:  # LF - end of line
                            if self.text_buffer:
                                try:
                                    line = self.text_buffer.decode('utf-8', errors='replace').strip()
                                    if line:
                                        self.on_text(line)
                                except:
                                    pass
                                self.text_buffer = bytearray()
                        elif byte_val == 0x0D:  # CR - ignore
                            pass
                        elif 0x20 <= byte_val < 0x7F or byte_val == 0x09:  # Printable or tab
                            self.text_buffer.append(byte_val)
                            # Limit buffer size (increased for long help lines)
                            if len(self.text_buffer) > 1024:
                                self.text_buffer = bytearray()

                    # Also feed to binary parser
                    result = self.parser.feed(byte_val)
                    if result is not None:
                        frames_parsed += 1
                        self.on_data(result)
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
        self.serial_manager = SerialManager(self._on_serial_data, self._on_labels, self._on_text_line)
        self.channel_configs: list[ChannelConfig] = list(self.config.channels)
        self.command_buttons: list[CommandButton] = list(self.config.buttons)
        self.time_window = self.config.time_window
        self.pending_labels: dict[int, str] = {}
        self.labels_updated = False
        self.ui_scale = 1.0  # Will be set properly in _setup_gui
        self.help_parsing = False  # Flag to indicate we're parsing help output
        self.parsed_commands: list[CommandButton] = []  # Commands parsed from help
        self.commands_updated = False  # Flag to rebuild command buttons
        self.terminal_queue: list[str] = []  # Queue for terminal output (thread-safe)
        self.terminal_lock = threading.Lock()
        self.dfu_output_queue: list[str] = []  # Queue for DFU output (thread-safe)
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
                # Only mark as updated if the label actually changed
                if self.channel_configs[ch_idx].name != label:
                    self.channel_configs[ch_idx].name = label
                    self.labels_updated = True

    def _on_text_line(self, line: str):
        """Callback for incoming text lines from serial port."""
        # Queue text for terminal output (called from serial thread)
        with self.terminal_lock:
            self.terminal_queue.append(line)

        # Check for help output start
        if "GMU Commands" in line:
            self.help_parsing = True
            self.parsed_commands = []
            self.help_parse_start_time = time.time()
            self.help_last_line_time = time.time()
            return

        # Check for help output end (separator line at end, or "help" command itself)
        if self.help_parsing:
            # Update last line time whenever we receive a line
            self.help_last_line_time = time.time()

            # End on closing separator or on "help" line (last command in list)
            if line.startswith("---------+") or ("|" in line and "help" in line.lower() and "sys" in line.lower()):
                if self.parsed_commands:
                    self.help_parsing = False
                    self.command_buttons = self.parsed_commands
                    self.commands_updated = True
                return

        # Parse command lines during help output
        if self.help_parsing and "|" in line:
            # Format: "CMD      | ARGS     | CAT   | DESCRIPTION"
            # Skip header line
            if "CMD" in line and "ARGS" in line:
                return

            parts = [p.strip() for p in line.split("|")]
            print(f"DEBUG: parts={parts}, len={len(parts)}")
            if len(parts) >= 3:
                cmd = parts[0].strip()
                args = parts[1].strip()
                cat = parts[2].strip()
                print(f"DEBUG: cmd={cmd}, args={args}, cat={cat}")

                # Only add commands without arguments (ARGS == "-")
                if cmd and args == "-":
                    btn = CommandButton(
                        label=cmd.capitalize(),
                        data=f"{cmd}\r\n",
                        mode="ascii",
                        category=cat
                    )
                    self.parsed_commands.append(btn)
                    print(f"DEBUG: Added command {cmd}")

    def _discover_commands(self):
        """Send help command to discover available commands."""
        if self.serial_manager.is_connected():
            self.help_parsing = False
            self.parsed_commands = []
            self.serial_manager.send(b"help\r\n")

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

    def _clear_terminal(self):
        """Clear the terminal output."""
        if dpg.does_item_exist("terminal_output"):
            dpg.set_value("terminal_output", "")

    def _process_terminal_queue(self):
        """Process queued terminal output (must be called from main thread)."""
        if not dpg.does_item_exist("terminal_output"):
            return

        # Get all queued lines
        with self.terminal_lock:
            if not self.terminal_queue:
                return
            lines = self.terminal_queue.copy()
            self.terminal_queue.clear()

        # Append to terminal
        current = dpg.get_value("terminal_output")
        new_text = current + "\n".join(lines) + "\n"

        # Limit terminal buffer to ~50KB to prevent memory issues
        max_len = 50000
        if len(new_text) > max_len:
            new_text = new_text[-max_len:]
        dpg.set_value("terminal_output", new_text)

        # Auto-scroll to bottom if enabled
        if dpg.does_item_exist("terminal_autoscroll") and dpg.get_value("terminal_autoscroll"):
            # Scroll the child_window container to bottom
            try:
                max_scroll = dpg.get_y_scroll_max("terminal_scroll_container")
                dpg.set_y_scroll("terminal_scroll_container", max_scroll)
            except Exception:
                pass

    def _browse_dfu_file(self):
        """Open file dialog to select .bin file."""
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        file_path = filedialog.askopenfilename(
            title="Select Firmware File",
            filetypes=[("Binary Files", "*.bin"), ("All Files", "*.*")]
        )
        root.destroy()
        if file_path:
            dpg.set_value("dfu_file_path", file_path)
            self.config.dfu_file_path = file_path

    def _enter_dfu_mode(self):
        """Send DFU command to device or show manual instructions."""
        if self.serial_manager.is_connected():
            self.serial_manager.send(b"dfu\r\n")
            self._append_dfu_output("Sent 'dfu' command to device...")
            self._append_dfu_output("Device should disconnect and enter DFU bootloader.")
        else:
            self._append_dfu_output("Not connected. Manual DFU entry:")
            self._append_dfu_output("1. Hold BOOT0 button")
            self._append_dfu_output("2. Press and release RESET")
            self._append_dfu_output("3. Release BOOT0")
            self._append_dfu_output("Device should appear as STM32 BOOTLOADER")

    def _flash_dfu(self):
        """Flash firmware using dfu-util in background thread."""
        file_path = dpg.get_value("dfu_file_path")
        address = dpg.get_value("dfu_address")

        if not file_path or not Path(file_path).exists():
            self._append_dfu_output("ERROR: Please select a valid .bin file")
            return

        dfu_util = get_dfu_util_path()
        # On Windows, check if bundled exe exists; on Linux, just use system command
        if sys.platform == 'win32' and not Path(dfu_util).exists():
            self._append_dfu_output("ERROR: dfu-util.exe not found in application directory")
            return

        self._append_dfu_output(f"Flashing {Path(file_path).name}...")
        self._append_dfu_output(f"Target: {address}")

        def worker():
            try:
                cmd = [dfu_util, '-a', '0', '-s', address, '-D', file_path]
                self._append_dfu_output(f"Running: {' '.join(cmd)}")

                # Use Popen for real-time output streaming
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # Merge stderr into stdout
                    text=True,
                    bufsize=1,  # Line buffered
                )

                # Stream output line by line
                for line in process.stdout:
                    line = line.rstrip('\n\r')
                    if line:
                        self._append_dfu_output(line)

                process.wait()

                if process.returncode == 0:
                    self._append_dfu_output("Flash completed successfully!")
                    dpg.configure_item("dfu_status", default_value="Success", color=(100, 255, 100))
                else:
                    self._append_dfu_output(f"Flash failed (exit code {process.returncode})")
                    dpg.configure_item("dfu_status", default_value="Failed", color=(255, 100, 100))
            except FileNotFoundError:
                if sys.platform == 'win32':
                    self._append_dfu_output("ERROR: dfu-util.exe not found in application directory")
                else:
                    self._append_dfu_output("ERROR: dfu-util not found. Install: sudo apt install dfu-util")
            except Exception as e:
                self._append_dfu_output(f"ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _append_dfu_output(self, text: str):
        """Thread-safe append to DFU output."""
        with self.terminal_lock:
            self.dfu_output_queue.append(text)

    def _process_dfu_queue(self):
        """Process DFU output queue (called from main loop)."""
        if not dpg.does_item_exist("dfu_output"):
            return
        with self.terminal_lock:
            if not self.dfu_output_queue:
                return
            lines = self.dfu_output_queue.copy()
            self.dfu_output_queue.clear()
        current = dpg.get_value("dfu_output")
        new_text = current + "\n".join(lines) + "\n"
        if len(new_text) > 50000:
            new_text = new_text[-50000:]
        dpg.set_value("dfu_output", new_text)
        try:
            max_scroll = dpg.get_y_scroll_max("dfu_output_container")
            dpg.set_y_scroll("dfu_output_container", max_scroll)
        except Exception:
            pass

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

    def _sz(self, value: int) -> int:
        """Scale a size value by the UI scale factor."""
        return int(value * self.ui_scale)

    def _rebuild_command_buttons(self):
        if dpg.does_item_exist("cmd_buttons_group"):
            dpg.delete_item("cmd_buttons_group", children_only=True)

            # Group buttons by category
            categories = {}
            uncategorized = []
            for i, btn in enumerate(self.command_buttons):
                if btn.category:
                    if btn.category not in categories:
                        categories[btn.category] = []
                    categories[btn.category].append((i, btn))
                else:
                    uncategorized.append((i, btn))

            # Category display names and order
            cat_names = {
                "state": "State",
                "diag": "Diagnostics",
                "param": "Parameters",
                "sys": "System"
            }
            cat_order = ["state", "diag", "param", "sys"]

            # Render categorized buttons
            for cat in cat_order:
                if cat in categories:
                    # Category header
                    dpg.add_text(cat_names.get(cat, cat.capitalize()),
                                color=(150, 200, 255), parent="cmd_buttons_group")
                    # Buttons in a horizontal flow
                    with dpg.group(horizontal=True, parent="cmd_buttons_group"):
                        for i, btn in categories[cat]:
                            dpg.add_button(
                                label=btn.label,
                                callback=lambda s, a, u: self._send_command(u),
                                user_data=btn,
                                width=self._sz(70),
                            )
                    dpg.add_spacer(height=5, parent="cmd_buttons_group")

            # Render any uncategorized buttons as simple buttons
            if uncategorized:
                if categories:
                    dpg.add_text("Other", color=(150, 200, 255), parent="cmd_buttons_group")
                with dpg.group(horizontal=True, parent="cmd_buttons_group"):
                    for i, btn in uncategorized:
                        dpg.add_button(
                            label=btn.label,
                            callback=lambda s, a, u: self._send_command(u),
                            user_data=btn,
                            width=self._sz(70),
                        )

    def _on_channel_visible(self, sender, value, user_data):
        idx = user_data
        if 0 <= idx < len(self.channel_configs):
            self.channel_configs[idx].visible = value

    def _on_channel_color(self, sender, value, user_data):
        idx = user_data
        if 0 <= idx < len(self.channel_configs):
            self.channel_configs[idx].color = (int(value[0]), int(value[1]), int(value[2]))

    def _on_channel_name(self, sender, value, user_data):
        idx = user_data
        if 0 <= idx < len(self.channel_configs):
            self.channel_configs[idx].name = value

    def _on_channel_scale(self, sender, value, user_data):
        idx = user_data
        if 0 <= idx < len(self.channel_configs):
            self.channel_configs[idx].scale = value

    def _on_channel_offset(self, sender, value, user_data):
        idx = user_data
        if 0 <= idx < len(self.channel_configs):
            self.channel_configs[idx].offset = value

    def _rebuild_channel_controls(self):
        if dpg.does_item_exist("channel_controls_group"):
            dpg.delete_item("channel_controls_group", children_only=True)
            for i, cfg in enumerate(self.channel_configs):
                with dpg.group(horizontal=True, parent="channel_controls_group"):
                    dpg.add_checkbox(
                        default_value=cfg.visible,
                        callback=self._on_channel_visible,
                        user_data=i,
                    )
                    dpg.add_color_edit(
                        default_value=(*cfg.color, 255),
                        callback=self._on_channel_color,
                        user_data=i,
                        no_alpha=True,
                        no_inputs=True,
                        width=self._sz(30),
                    )
                    dpg.add_input_text(
                        default_value=cfg.name,
                        width=self._sz(70),
                        callback=self._on_channel_name,
                        user_data=i,
                        on_enter=True,
                    )
                    dpg.add_input_float(
                        default_value=cfg.scale,
                        width=self._sz(50),
                        callback=self._on_channel_scale,
                        user_data=i,
                        format="%.2f",
                        step=0,
                        on_enter=True,
                    )
                    dpg.add_input_float(
                        default_value=cfg.offset,
                        width=self._sz(50),
                        callback=self._on_channel_offset,
                        user_data=i,
                        format="%.1f",
                        step=0,
                        on_enter=True,
                    )

    def _create_splitter_theme(self):
        """Create a theme for the horizontal splitter bar."""
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (60, 60, 60, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (100, 100, 100, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (80, 80, 80, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 0)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 0, 0)
        return theme

    def _create_vsplitter_theme(self):
        """Create a theme for the vertical splitter bar."""
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (50, 50, 50, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (100, 100, 100, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (80, 80, 80, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 0)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 0, 0)
        return theme

    def _on_mouse_down(self, sender, app_data):
        """Track mouse down on splitter."""
        # Check horizontal splitter first
        if self.splitter_hovered:
            self.splitter_dragging = True
            self.drag_start_mouse_y = dpg.get_mouse_pos(local=False)[1]
            self.drag_start_panel_height = self.bottom_panel_height
        # Check vertical splitters
        elif self.h_splitter_hovered is not None:
            self.h_splitter_dragging = self.h_splitter_hovered
            self.h_drag_start_mouse_x = dpg.get_mouse_pos(local=False)[0]
            self.h_drag_start_widths = list(self.section_widths)

    def _on_mouse_release(self, sender, app_data):
        """Track mouse release."""
        self.splitter_dragging = False
        self.h_splitter_dragging = None

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
        dpg.configure_item("top_panel", height=-int(new_height + self._sz(12)))
        dpg.configure_item("bottom_panel", height=int(new_height))

    def _update_h_splitters(self):
        """Update horizontal section widths based on vertical splitter dragging."""
        # Check which vertical splitter is hovered
        self.h_splitter_hovered = None
        for i in range(3):
            tag = f"vsplitter_{i}"
            if dpg.does_item_exist(tag) and dpg.is_item_hovered(tag):
                self.h_splitter_hovered = i
                break

        # Handle dragging
        if self.h_splitter_dragging is None:
            return

        idx = self.h_splitter_dragging
        current_mouse_x = dpg.get_mouse_pos(local=False)[0]
        delta_x = current_mouse_x - self.h_drag_start_mouse_x

        min_width = self._sz(80)

        # For splitter 2 (between Channels and Commands), only adjust Channels width
        # Commands section always stays at width=-1 to fill remaining space
        if idx == 2:
            new_left = self.h_drag_start_widths[idx] + delta_x
            if new_left < min_width:
                new_left = min_width
            self.section_widths[idx] = int(new_left)
        else:
            # For splitters 0 and 1, adjust both adjacent sections
            new_left = self.h_drag_start_widths[idx] + delta_x
            new_right = self.h_drag_start_widths[idx + 1] - delta_x

            # Clamp to minimum widths
            if new_left < min_width:
                delta_x = min_width - self.h_drag_start_widths[idx]
                new_left = min_width
                new_right = self.h_drag_start_widths[idx + 1] - delta_x
            if new_right < min_width:
                delta_x = self.h_drag_start_widths[idx + 1] - min_width
                new_right = min_width
                new_left = self.h_drag_start_widths[idx] + delta_x

            self.section_widths[idx] = int(new_left)
            self.section_widths[idx + 1] = int(new_right)

        # Update section widths (only first 3 sections - Commands section stays at width=-1)
        section_tags = ["section_connection", "section_time", "section_channels"]
        for i, tag in enumerate(section_tags):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, width=self.section_widths[i])

    def _setup_gui(self):
        dpg.create_context()

        # Detect and apply display scaling for HiDPI support
        self.ui_scale = get_display_scale()

        # Scale viewport size
        viewport_width = int(1200 * self.ui_scale)
        viewport_height = int(700 * self.ui_scale)

        # Get icon path for viewport
        icon_path = get_resource_path('branding/Dragoon-icon.ico')
        if not os.path.exists(icon_path):
            icon_path = None

        dpg.create_viewport(
            title="DragoonPlot",
            width=viewport_width,
            height=viewport_height,
            small_icon=icon_path,
            large_icon=icon_path,
        )

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

        # Horizontal section widths (resizable)
        self.section_widths = [sz(220), sz(150), sz(350)]  # Connection, Time, Channels (Commands fills remaining)
        self.h_splitter_dragging = None  # Which splitter is being dragged (0, 1, 2)
        self.h_splitter_hovered = None
        self.h_drag_start_mouse_x = 0
        self.h_drag_start_widths = []

        # Register global mouse handlers for splitter dragging
        with dpg.handler_registry(tag="global_handlers"):
            dpg.add_mouse_click_handler(button=0, callback=self._on_mouse_down)
            dpg.add_mouse_release_handler(button=0, callback=self._on_mouse_release)

        with dpg.window(tag="main_window"):
            # Top panel - Tabbed view (Graph and Terminal)
            with dpg.child_window(tag="top_panel", height=sz(-182)):
                with dpg.tab_bar(tag="main_tabs"):
                    # Graph tab
                    with dpg.tab(label="Graph", tag="graph_tab"):
                        with dpg.plot(
                            label="Serial Data",
                            tag="main_plot",
                            height=-1,
                            width=-1,
                            anti_aliased=True,
                        ):
                            dpg.add_plot_legend()
                            dpg.add_plot_axis(dpg.mvXAxis, label="Seconds", tag="x_axis")
                            dpg.add_plot_axis(dpg.mvYAxis, label="Value", tag="y_axis", auto_fit=True)

                    # Terminal tab
                    with dpg.tab(label="Terminal", tag="terminal_tab"):
                        with dpg.group(horizontal=True):
                            dpg.add_button(label="Clear", callback=self._clear_terminal, width=sz(60))
                            dpg.add_checkbox(label="Auto-scroll", tag="terminal_autoscroll", default_value=True)
                        with dpg.child_window(tag="terminal_scroll_container", height=-1, width=-1):
                            dpg.add_text(
                                tag="terminal_output",
                                default_value="",
                                tracked=True,
                                track_offset=1.0,  # Track at bottom
                            )

                    # DFU tab
                    with dpg.tab(label="DFU", tag="dfu_tab"):
                        with dpg.group(horizontal=True):
                            dpg.add_button(label="Browse", callback=self._browse_dfu_file, width=sz(60))
                            dpg.add_input_text(
                                tag="dfu_file_path",
                                default_value=self.config.dfu_file_path,
                                hint="Select .bin file...",
                                width=-1,
                                readonly=True
                            )
                        with dpg.group(horizontal=True):
                            dpg.add_text("Address:")
                            dpg.add_input_text(tag="dfu_address", default_value="0x08004000", width=sz(100))
                            dpg.add_button(label="Enter DFU", callback=self._enter_dfu_mode, width=sz(80))
                            dpg.add_button(label="Flash", callback=self._flash_dfu, width=sz(60))
                        dpg.add_text("", tag="dfu_status", color=(200, 200, 200))
                        with dpg.child_window(tag="dfu_output_container", height=-1, width=-1):
                            dpg.add_text(
                                tag="dfu_output",
                                default_value="",
                                tracked=True,
                                track_offset=1.0,
                            )

            # Splitter bar - draggable divider
            dpg.add_button(tag="splitter_bar", label="", height=sz(10), width=-1)
            dpg.bind_item_theme("splitter_bar", self._create_splitter_theme())

            # Bottom panel - Controls with resizable sections
            with dpg.child_window(tag="bottom_panel", height=sz(170)):
                with dpg.group(horizontal=True):
                    # Connection section
                    with dpg.child_window(tag="section_connection", width=self.section_widths[0], height=-1, border=False):
                        dpg.add_text("Connection", color=(200, 200, 255))
                        with dpg.group(horizontal=True):
                            dpg.add_combo(
                                tag="port_combo",
                                items=[],
                                default_value=self.config.last_port,
                                width=-25,
                            )
                            dpg.add_button(label="R", callback=self._refresh_ports, width=sz(25))
                        dpg.add_combo(
                            tag="baud_combo",
                            items=[str(b) for b in BAUD_RATES],
                            default_value=str(self.config.last_baud),
                            width=-1,
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

                    # Vertical splitter 0
                    dpg.add_button(tag="vsplitter_0", label="", width=sz(6), height=-1)
                    dpg.bind_item_theme("vsplitter_0", self._create_vsplitter_theme())

                    # Time/Graph settings section
                    with dpg.child_window(tag="section_time", width=self.section_widths[1], height=-1, border=False):
                        dpg.add_text("X Axis Range", color=(200, 200, 255))
                        dpg.add_slider_float(
                            tag="time_slider",
                            default_value=self.time_window,
                            min_value=1.0,
                            max_value=300.0,
                            callback=lambda s, a: setattr(self, 'time_window', a),
                            width=-1,
                            format="%.0f sec",
                        )
                        dpg.add_input_float(
                            tag="time_input",
                            default_value=self.time_window,
                            width=-1,
                            callback=self._on_time_input,
                            format="%.1f",
                            step=1.0,
                        )

                    # Vertical splitter 1
                    dpg.add_button(tag="vsplitter_1", label="", width=sz(6), height=-1)
                    dpg.bind_item_theme("vsplitter_1", self._create_vsplitter_theme())

                    # Channels section
                    with dpg.child_window(tag="section_channels", width=self.section_widths[2], height=-1, border=False):
                        dpg.add_text("Channels (Vis|Color|Name|Scale|Offset)", color=(200, 200, 255))
                        with dpg.child_window(tag="channels_window", height=-1, width=-1):
                            dpg.add_group(tag="channel_controls_group")

                    # Vertical splitter 2
                    dpg.add_button(tag="vsplitter_2", label="", width=sz(6), height=-1)
                    dpg.bind_item_theme("vsplitter_2", self._create_vsplitter_theme())

                    # Commands section
                    with dpg.child_window(tag="section_commands", width=-1, height=-1, border=False):
                        with dpg.group(horizontal=True):
                            dpg.add_text("Commands", color=(200, 200, 255))
                            dpg.add_button(label="Discover", callback=self._discover_commands, width=sz(60))
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

            # Update horizontal section splitters
            self._update_h_splitters()

            # Rebuild channel controls if new channels detected or labels updated
            current_count = len(self.channel_configs)
            if current_count != last_channel_count or self.labels_updated:
                self._rebuild_channel_controls()
                last_channel_count = current_count
                self.labels_updated = False

            # Rebuild command buttons if discovered new commands
            if self.commands_updated:
                self._rebuild_command_buttons()
                self.commands_updated = False

            # Check for help parsing timeout (finalize after 2 seconds of no new lines)
            if self.help_parsing and self.parsed_commands:
                if hasattr(self, 'help_last_line_time'):
                    if time.time() - self.help_last_line_time > 2.0:
                        self.help_parsing = False
                        self.command_buttons = self.parsed_commands
                        self.commands_updated = True

            # Process terminal output queue (thread-safe GUI updates)
            self._process_terminal_queue()
            self._process_dfu_queue()

            dpg.render_dearpygui_frame()

        self.serial_manager.disconnect()
        self._save_config()
        dpg.destroy_context()


def main():
    app = DragoonPlotApp()
    app.run()


if __name__ == "__main__":
    main()
