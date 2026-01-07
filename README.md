# DragoonPlot

A lightweight, portable serial plotter with real-time graphing. Single Python file, runs on Linux and Windows.

## Installation

### Option 1: Run from Source

```bash
pip install dearpygui pyserial
python dragoonplot.py
```

### Option 2: Build Standalone Executable

Requires PyInstaller:

```bash
pip install dearpygui pyserial pyinstaller
```

**Linux:**
```bash
./build.sh
./dist/DragoonPlot
```

**Windows:**
```batch
build.bat
dist\DragoonPlot.exe
```

The executable is fully self-contained with no dependencies.

## Interface

```
+------------------------------------------------------------------+
|  [Graph] [Terminal]                                               |
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

- **Tabs**: Switch between Graph view and Terminal view
- **Terminal**: Shows raw text output from serial port with auto-scroll
- **Splitter**: Drag the horizontal bar to resize top/bottom panels
- **Section dividers**: Drag vertical splitters to resize bottom panel sections

### Controls

- **Port**: Select serial port (click R to refresh list)
- **Baud**: Select baud rate (9600 - 921600)
- **Connect/Disconnect**: Toggle serial connection
- **Clear**: Clear all graph data
- **Save**: Save current configuration

### Graph Settings

- **X Axis Range**: Time window in seconds (1-300s / 5 minutes max)
- Slider for quick adjustment, input field for precise values

### Channel Configuration

Each channel row:
- **Checkbox**: Show/hide channel on graph
- **Color square**: Click to change line color
- **Name**: Editable channel label
- **Scale**: Multiply values by this factor
- **Offset**: Add this value after scaling

### Command Buttons

- Click **Discover** to auto-detect commands from the device (sends `help` command)
- Commands are automatically grouped by category (State, Diagnostics, Parameters, System)
- Click any command button to send it to the device
- Discovered commands are saved to configuration

## Configuration

Settings are saved to `~/.dragoonplot.json` and restored on startup:
- Last used port and baud rate
- Channel names, colors, visibility, scale, offset
- Command buttons
- Time window

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
