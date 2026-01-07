# DragoonPlot

A lightweight, portable serial plotter with real-time graphing and DFU firmware flashing. Single Python file, runs on Linux and Windows.

## Quick Start - Build Executable

### Windows (One Command)

**Prerequisites:** Python 3.8+ from [python.org](https://python.org) (check "Add Python to PATH" during install)

```batch
build.bat
```

That's it! The script automatically installs dependencies and creates `dist\DragoonPlot.exe`.

### Linux

```bash
pip install dearpygui pyserial pyinstaller
./build.sh
./dist/DragoonPlot
```

## Run from Source (Development)

```bash
pip install -r requirements.txt
python dragoonplot.py
```

Or manually:
```bash
pip install dearpygui pyserial
python dragoonplot.py
```

## Features

- **Real-time plotting** of serial data with configurable time window
- **Terminal view** for raw serial output
- **DFU flashing** for STM32 devices (dfu-util bundled on Windows)
- **Command discovery** - auto-detects device commands via `help`
- **Configurable channels** - visibility, colors, scale, offset
- **HiDPI support** - automatic scaling on high-resolution displays

## Interface

```
+------------------------------------------------------------------+
|  [Graph] [Terminal] [DFU]                                         |
|                         Graph Area                                |
|   (real-time scrolling plot, X axis: 0 to time_window seconds)   |
+------------------------------------------------------------------+
|  ═══════════════════ Draggable Splitter ════════════════════════ |
+------------------------------------------------------------------+
| Connection | X Axis  | Channels              | Commands          |
| [Port ▼][R]| Range   | [x] ■ Name Scale Off  | [Discover]        |
| [Baud ▼]   | [slider]| [x] ■ Name Scale Off  | [Cmd1] [Cmd2] ... |
| [Connect]  | [input] | ...                   |                   |
| [Clear]    |         |                       |                   |
+------------------------------------------------------------------+
```

### Tabs
- **Graph**: Real-time scrolling plot
- **Terminal**: Raw text output with auto-scroll
- **DFU**: Firmware flashing for STM32 devices

### Controls

- **Port**: Select serial port (click R to refresh list)
- **Baud**: Select baud rate (9600 - 921600)
- **Connect/Disconnect**: Toggle serial connection
- **Clear**: Clear all graph data
- **Save**: Save current configuration

### DFU Flashing

1. Click **Browse** to select a `.bin` firmware file
2. Set the target address (default: `0x08004000` for 2nd sector)
3. Click **Enter DFU** to put device in bootloader mode
4. Click **Flash** to program the firmware

**Note:** Windows users need to install the WinUSB driver via [Zadig](https://zadig.akeo.ie/) for the STM32 DFU device.

### Command Buttons

- Click **Discover** to auto-detect commands from the device (sends `help` command)
- Commands are automatically grouped by category (State, Diagnostics, Parameters, System)
- Click any command button to send it to the device

## Configuration

Settings are saved to `~/.dragoonplot.json` and restored on startup:
- Last used port and baud rate
- Channel names, colors, visibility, scale, offset
- Command buttons
- Time window
- Last DFU file path

## Protocol

For details on implementing the serial protocol on your microcontroller, see [PROTOCOL.md](PROTOCOL.md).

### Quick Start

Send signed 16-bit integers:
```
[0xAA] [channel_count] [int16 x N]
```

Example (3 channels: 100, 2000, -500):
```
0xAA 0x03 0x64 0x00 0xD0 0x07 0x0C 0xFE
```
