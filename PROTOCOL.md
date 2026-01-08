# Serial Monitor Protocol Reference

Binary protocol for sending data to the Serial Monitor application.

## Overview

Two frame types are supported:

| Frame Type | Start Byte | Purpose |
|------------|------------|---------|
| Data       | `0xAA`     | Send int16 values for plotting |
| Label      | `0xAB`     | Set channel names |

## Data Frame (0xAA)

Send numerical values for one or more channels.

### Format

```
[0xAA] [COUNT] [DATA...]

0xAA      - 1 byte  - Start marker
COUNT     - 1 byte  - Number of channels (1-32)
DATA      - N×2 bytes - int16 values, little-endian, signed
```

No checksum - minimal overhead for maximum speed.

### Value Range

Values are **signed 16-bit integers**:
- Range: -32768 to 32767
- Use scale/offset in the UI for unit conversion (e.g., raw ADC to voltage)

### Frame Size

```
Total bytes = 2 + (channel_count × 2)

Examples:
  1 channel:  4 bytes
  3 channels: 8 bytes
  8 channels: 18 bytes
  16 channels: 34 bytes
```

### Example: 3 Channels

Values: 100, 2000, -500

```
Byte-by-byte:
  0xAA                 - Start
  0x03                 - 3 channels
  0x64 0x00            - 100 as int16 LE
  0xD0 0x07            - 2000 as int16 LE
  0x0C 0xFE            - -500 as int16 LE
```

### Example: Negative Values

Values: -1, -32768, 32767

```
Byte-by-byte:
  0xAA                 - Start
  0x03                 - 3 channels
  0xFF 0xFF            - -1 as int16 LE
  0x00 0x80            - -32768 as int16 LE (minimum)
  0xFF 0x7F            - 32767 as int16 LE (maximum)
```

## Label Frame (0xAB)

Set human-readable names for channels. Send once on startup or periodically.

### Format

```
[0xAB] [COUNT] [LABEL_DATA...]

0xAB       - 1 byte  - Start marker
COUNT      - 1 byte  - Number of labels in this frame
LABEL_DATA - Variable - Label entries (see below)
```

### Label Entry Format

```
[CH_INDEX] [LENGTH] [CHARACTERS...]

CH_INDEX   - 1 byte  - Channel index (0-31)
LENGTH     - 1 byte  - String length (1-16)
CHARACTERS - N bytes - UTF-8 string (no null terminator)
```

### Example: 3 Labels

Labels: "Temp", "Humidity", "Press"

```
0xAB                    - Start
0x03                    - 3 labels
0x00 0x04 T e m p       - Channel 0: "Temp"
0x01 0x08 H u m i d i t y  - Channel 1: "Humidity"
0x02 0x05 P r e s s     - Channel 2: "Press"
```

---

## Arduino/C Implementation

### Complete Library

```c
#include <Arduino.h>

// Send data frame with int16 values (no checksum)
void sendPlotData(int16_t* values, uint8_t count) {
    if (count == 0 || count > 32) return;

    Serial.write(0xAA);
    Serial.write(count);

    for (uint8_t i = 0; i < count; i++) {
        uint8_t* bytes = (uint8_t*)&values[i];
        Serial.write(bytes[0]);  // Low byte
        Serial.write(bytes[1]);  // High byte
    }
}

// Send label frame with channel names (no checksum)
void sendPlotLabels(const char** labels, uint8_t count) {
    if (count == 0 || count > 32) return;

    Serial.write(0xAB);
    Serial.write(count);

    for (uint8_t i = 0; i < count; i++) {
        Serial.write(i);  // Channel index

        uint8_t len = strlen(labels[i]);
        if (len > 16) len = 16;  // Max label length
        Serial.write(len);

        for (uint8_t j = 0; j < len; j++) {
            Serial.write(labels[i][j]);
        }
    }
}

// Convenience: send single value
void sendPlotValue(int16_t value) {
    sendPlotData(&value, 1);
}

// Convenience: send two values
void sendPlotValues2(int16_t v1, int16_t v2) {
    int16_t values[2] = {v1, v2};
    sendPlotData(values, 2);
}

// Convenience: send three values
void sendPlotValues3(int16_t v1, int16_t v2, int16_t v3) {
    int16_t values[3] = {v1, v2, v3};
    sendPlotData(values, 3);
}
```

### Example Sketch

```c
const char* labels[] = {"ADC0", "Gyro X", "Accel Z"};
bool labelsSent = false;

void setup() {
    Serial.begin(115200);
}

void loop() {
    // Send labels once at startup
    if (!labelsSent) {
        sendPlotLabels(labels, 3);
        labelsSent = true;
    }

    // Read sensors as raw int16 values
    int16_t adc0 = analogRead(A0) - 512;   // Centered around 0
    int16_t gyroX = readGyroRaw();          // e.g., -32768 to 32767
    int16_t accelZ = readAccelRaw();        // e.g., -32768 to 32767

    // Send data
    sendPlotValues3(adc0, gyroX, accelZ);

    delay(10);  // 100 Hz update rate
}
```

**Tip**: Use the scale/offset controls in the UI to convert raw values to real units:
- ADC (0-1023) with offset -512, scale 0.00322 → -1.65V to 1.65V
- Gyro raw with scale 0.061 → degrees/second
- Accel raw with scale 0.000488 → g-force

### Periodic Label Updates

For robustness (e.g., if monitor connects after MCU starts):

```c
uint32_t lastLabelTime = 0;
const uint32_t LABEL_INTERVAL = 5000;  // Every 5 seconds

void loop() {
    // Periodically resend labels
    if (millis() - lastLabelTime > LABEL_INTERVAL) {
        sendPlotLabels(labels, 3);
        lastLabelTime = millis();
    }

    // Send data as usual
    sendPlotValues3(temp, humidity, pressure);
    delay(50);
}
```

---

## STM32/HAL Implementation

```c
#include "main.h"
#include <string.h>

extern UART_HandleTypeDef huart2;

void sendPlotData(int16_t* values, uint8_t count) {
    if (count == 0 || count > 32) return;

    uint8_t header[2] = {0xAA, count};
    HAL_UART_Transmit(&huart2, header, 2, HAL_MAX_DELAY);

    for (uint8_t i = 0; i < count; i++) {
        HAL_UART_Transmit(&huart2, (uint8_t*)&values[i], 2, HAL_MAX_DELAY);
    }
}

void sendPlotLabels(const char** labels, uint8_t count) {
    if (count == 0 || count > 32) return;

    uint8_t header[2] = {0xAB, count};
    HAL_UART_Transmit(&huart2, header, 2, HAL_MAX_DELAY);

    for (uint8_t i = 0; i < count; i++) {
        uint8_t idx = i;
        uint8_t len = strlen(labels[i]);
        if (len > 16) len = 16;

        HAL_UART_Transmit(&huart2, &idx, 1, HAL_MAX_DELAY);
        HAL_UART_Transmit(&huart2, &len, 1, HAL_MAX_DELAY);
        HAL_UART_Transmit(&huart2, (uint8_t*)labels[i], len, HAL_MAX_DELAY);
    }
}
```

---

## ESP-IDF Implementation

```c
#include "driver/uart.h"
#include <string.h>

#define UART_NUM UART_NUM_0

void sendPlotData(int16_t* values, uint8_t count) {
    if (count == 0 || count > 32) return;

    uint8_t header[2] = {0xAA, count};
    uart_write_bytes(UART_NUM, header, 2);

    for (uint8_t i = 0; i < count; i++) {
        uart_write_bytes(UART_NUM, (uint8_t*)&values[i], 2);
    }
}
```

---

## Python Implementation (for testing)

```python
import struct
import serial

def send_plot_data(ser: serial.Serial, values: list[int]):
    """Send int16 values. Values must be in range -32768 to 32767."""
    count = len(values)
    if count == 0 or count > 32:
        return

    data = bytes([0xAA, count])
    for v in values:
        data += struct.pack('<h', v)  # signed int16, little-endian
    ser.write(data)

def send_plot_labels(ser: serial.Serial, labels: list[str]):
    count = len(labels)
    if count == 0 or count > 32:
        return

    data = bytes([0xAB, count])
    for i, label in enumerate(labels):
        label_bytes = label.encode('utf-8')[:16]
        data += bytes([i, len(label_bytes)]) + label_bytes
    ser.write(data)

# Example usage
ser = serial.Serial('/dev/ttyUSB0', 115200)
send_plot_labels(ser, ['Sensor1', 'Sensor2'])
send_plot_data(ser, [1000, -500])  # int16 values
```

---

## Performance Considerations

### Recommended Update Rates

| Baud Rate | Max Channels | Recommended Rate |
|-----------|--------------|------------------|
| 9600      | 2-4          | 20-50 Hz         |
| 115200    | 16           | 100-200 Hz       |
| 921600    | 32           | 500+ Hz          |

### Bandwidth Calculation

```
Bytes per frame = 2 + (channels × 2)
Bits per frame = bytes × 10  (8N1 encoding)
Max frames/sec = baud_rate / bits_per_frame

Example: 3 channels at 115200 baud
  Bytes = 2 + 6 = 8
  Bits = 80
  Max rate = 115200 / 80 = 1440 Hz

Example: 8 channels at 115200 baud
  Bytes = 2 + 16 = 18
  Bits = 180
  Max rate = 115200 / 180 = 640 Hz
```

### Tips

1. **Buffer size**: The plotter keeps 10,000 samples per channel
2. **Labels**: Send once on startup, optionally every few seconds
3. **No checksum**: Frames are minimal for maximum throughput
4. **Partial frames**: The parser auto-recovers by scanning for 0xAA/0xAB
5. **Value conversion**: Use UI scale/offset to convert raw int16 to real units

---

## Troubleshooting

### No data appearing
- Check port and baud rate match your device
- Verify start byte (0xAA) and channel count are correct
- Use a logic analyzer or terminal to verify bytes

### Garbled channel names
- Ensure UTF-8 encoding
- Check label length ≤ 16 characters
- Verify label count matches actual labels sent

### Data looks wrong
- Confirm little-endian int16 encoding
- Check scale/offset settings in the UI
- Verify sensor readings on the MCU side

---

## Text Protocol (Commands)

DragoonPlot can discover and display command buttons by parsing help output from the device. This allows devices to expose their available commands without hardcoding them in the application.

### Command Discovery

When the user clicks "Discover" in the Commands section, DragoonPlot sends:

```
help\r\n
```

The device should respond with a formatted table that DragoonPlot parses to create command buttons.

### Help Output Format

```
GMU Commands
---------+----------+-------+---------------------------
CMD      | ARGS     | CAT   | DESCRIPTION
---------+----------+-------+---------------------------
start    | -        | state | Start the motor
stop     | -        | state | Stop the motor
status   | -        | diag  | Show system status
setpid   | kp ki kd | param | Set PID parameters
help     | -        | sys   | Show this help
---------+----------+-------+---------------------------
```

### Parsing Rules

1. **Start marker:** Line containing "GMU Commands" begins parsing
2. **Table format:** Columns separated by `|` character
3. **Command extraction:**
   - `CMD`: Command name (becomes button label, capitalized)
   - `ARGS`: If `-`, command takes no arguments and gets a button
   - `CAT`: Category for grouping buttons
   - `DESCRIPTION`: Ignored by parser (for human reference)
4. **End markers:**
   - Closing separator line starting with `---------+`
   - Or line containing `help` command in `sys` category
   - Or 2-second timeout after last line received

### Categories

Commands are grouped in the UI by category:

| Category | Display Name | Description |
|----------|--------------|-------------|
| `state`  | State        | State control commands (start, stop, etc.) |
| `diag`   | Diagnostics  | Diagnostic and status commands |
| `param`  | Parameters   | Parameter configuration commands |
| `sys`    | System       | System commands (help, reset, dfu, etc.) |

### Button Behavior

- Only commands with `ARGS == "-"` (no arguments) get buttons
- Clicking a button sends: `<command>\r\n`
- Commands requiring arguments must be sent manually via the Terminal tab

### STM32/C Implementation

```c
void cmd_help(void) {
    printf("\r\nGMU Commands\r\n");
    printf("---------+----------+-------+---------------------------\r\n");
    printf("CMD      | ARGS     | CAT   | DESCRIPTION\r\n");
    printf("---------+----------+-------+---------------------------\r\n");
    printf("start    | -        | state | Start the motor\r\n");
    printf("stop     | -        | state | Stop the motor\r\n");
    printf("status   | -        | diag  | Show system status\r\n");
    printf("setpid   | kp ki kd | param | Set PID parameters\r\n");
    printf("dfu      | -        | sys   | Enter DFU bootloader\r\n");
    printf("help     | -        | sys   | Show this help\r\n");
    printf("---------+----------+-------+---------------------------\r\n");
}
```

### Arduino Implementation

```c
void printHelp() {
    Serial.println("\r\nGMU Commands");
    Serial.println("---------+----------+-------+---------------------------");
    Serial.println("CMD      | ARGS     | CAT   | DESCRIPTION");
    Serial.println("---------+----------+-------+---------------------------");
    Serial.println("start    | -        | state | Start data streaming");
    Serial.println("stop     | -        | state | Stop data streaming");
    Serial.println("status   | -        | diag  | Show device status");
    Serial.println("help     | -        | sys   | Show this help");
    Serial.println("---------+----------+-------+---------------------------");
}
```

### Troubleshooting Commands

#### Buttons not appearing after Discover
- Verify the help output contains "GMU Commands" header
- Check that columns are separated by `|` character
- Ensure `ARGS` column contains exactly `-` for no-argument commands
- Check Terminal tab to see raw help output

#### Wrong buttons appearing
- Only commands with `ARGS == "-"` get buttons
- Commands with arguments (e.g., `setpid | kp ki kd`) are intentionally excluded
- Clear saved config (`~/.dragoonplot.json`) to reset buttons
