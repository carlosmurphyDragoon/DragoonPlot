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
from pathlib import Path
from collections import deque
from typing import Optional, Union
from dataclasses import dataclass, field

import dearpygui.dearpygui as dpg
import serial
import serial.tools.list_ports

# === Constants ===
START_DATA = 0xAA
START_LABEL = 0xAB
MAX_CHANNELS = 32
MAX_LABEL_LEN = 16
BUFFER_SIZE = 10000
DEFAULT_TIME_WINDOW = 10.0
CONFIG_FILE = Path.home() / ".serial_monitor.json"
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
        while self.running:
            try:
                if self.port and self.port.in_waiting:
                    with self.lock:
                        data = self.port.read(self.port.in_waiting)
                    for byte_val in data:
                        result = self.parser.feed(byte_val)
                        if result is not None:
                            self.on_data(result)
                else:
                    time.sleep(0.001)
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
        self._setup_gui()

    def _load_config(self) -> AppConfig:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return AppConfig.from_dict(json.load(f))
            except Exception as e:
                print(f"Error loading config: {e}")
        return AppConfig()

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

    def _rebuild_command_buttons(self):
        if dpg.does_item_exist("cmd_buttons_group"):
            dpg.delete_item("cmd_buttons_group", children_only=True)
            for i, btn in enumerate(self.command_buttons):
                with dpg.group(horizontal=True, parent="cmd_buttons_group"):
                    dpg.add_button(
                        label=btn.label,
                        callback=lambda s, a, b=btn: self._send_command(b),
                        width=80,
                    )
                    dpg.add_input_text(
                        default_value=btn.label,
                        width=60,
                        callback=lambda s, a, b=btn: setattr(b, 'label', a),
                        on_enter=True,
                        hint="Label",
                    )
                    dpg.add_input_text(
                        default_value=btn.data,
                        width=100,
                        callback=lambda s, a, b=btn: setattr(b, 'data', a),
                        on_enter=True,
                        hint="Data",
                    )
                    dpg.add_combo(
                        items=["ascii", "hex"],
                        default_value=btn.mode,
                        width=60,
                        callback=lambda s, a, b=btn: setattr(b, 'mode', a),
                    )
                    dpg.add_button(
                        label="X",
                        callback=lambda s, a, idx=i: self._remove_command_button(idx),
                        width=25,
                    )

    def _rebuild_channel_controls(self):
        if dpg.does_item_exist("channel_controls_group"):
            dpg.delete_item("channel_controls_group", children_only=True)
            for i, cfg in enumerate(self.channel_configs):
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
                        width=30,
                    )
                    dpg.add_input_text(
                        default_value=cfg.name,
                        width=70,
                        callback=lambda s, a, c=cfg: setattr(c, 'name', a),
                        on_enter=True,
                    )
                    dpg.add_input_float(
                        default_value=cfg.scale,
                        width=50,
                        callback=lambda s, a, c=cfg: setattr(c, 'scale', a),
                        format="%.2f",
                        step=0,
                    )
                    dpg.add_input_float(
                        default_value=cfg.offset,
                        width=50,
                        callback=lambda s, a, c=cfg: setattr(c, 'offset', a),
                        format="%.1f",
                        step=0,
                    )

    def _setup_gui(self):
        dpg.create_context()
        dpg.create_viewport(title="DragoonPlot", width=1200, height=700)

        with dpg.window(tag="main_window"):
            # Top panel - Graph (takes most space)
            with dpg.child_window(tag="graph_panel", height=-180):
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

            # Bottom panel - Controls
            with dpg.child_window(tag="bottom_panel", height=170):
                with dpg.group(horizontal=True):
                    # Connection section
                    with dpg.group(width=220):
                        dpg.add_text("Connection", color=(200, 200, 255))
                        with dpg.group(horizontal=True):
                            dpg.add_combo(
                                tag="port_combo",
                                items=[],
                                default_value=self.config.last_port,
                                width=120,
                            )
                            dpg.add_button(label="R", callback=self._refresh_ports, width=25)
                        dpg.add_combo(
                            tag="baud_combo",
                            items=[str(b) for b in BAUD_RATES],
                            default_value=str(self.config.last_baud),
                            width=145,
                        )
                        with dpg.group(horizontal=True):
                            dpg.add_button(
                                label="Connect",
                                tag="connect_btn",
                                callback=self._toggle_connection,
                                width=70,
                            )
                            dpg.add_button(label="Clear", callback=self._clear_data, width=50)
                            dpg.add_button(label="Save", callback=self._save_config, width=50)
                        dpg.add_text("Disconnected", tag="status_text", color=(255, 100, 100))

                    dpg.add_separator()

                    # Graph settings
                    with dpg.group(width=150):
                        dpg.add_text("X Axis Range", color=(200, 200, 255))
                        dpg.add_slider_float(
                            tag="time_slider",
                            default_value=self.time_window,
                            min_value=1.0,
                            max_value=300.0,
                            callback=lambda s, a: setattr(self, 'time_window', a),
                            width=140,
                            format="%.0f sec",
                        )
                        dpg.add_input_float(
                            tag="time_input",
                            default_value=self.time_window,
                            width=140,
                            callback=self._on_time_input,
                            format="%.1f",
                            step=1.0,
                        )

                    dpg.add_separator()

                    # Channels section
                    with dpg.group(width=350):
                        dpg.add_text("Channels (Vis|Color|Name|Scale|Offset)", color=(200, 200, 255))
                        with dpg.child_window(height=120, width=340):
                            dpg.add_group(tag="channel_controls_group")

                    dpg.add_separator()

                    # Commands section
                    with dpg.group():
                        with dpg.group(horizontal=True):
                            dpg.add_text("Commands", color=(200, 200, 255))
                            dpg.add_button(label="+", callback=self._add_command_button, width=25)
                        with dpg.child_window(height=120, width=-1):
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
