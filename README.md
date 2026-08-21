# ◈ TEAMATRIX INDUSTRIAL CONSOLE v2.0
**Automated 13-Ingredient Tea Blending, Conveying, & Mixing System**

## 1. System Overview
TeaMatrix v2.0 is a multi-board robotic solution for the precision blending of premium teas. It automates the entire process from ingredient dispensing to final mixing, controlled by a Raspberry Pi 5 master unit.

## 2. Ingredient & Hardware Matrix
| Station | Ingredient / Component | Hardware Port |
| :--- | :--- | :--- |
| **1 - 5** | Base Teas (BOPF, Peko, BOP, Silver/Golden Tips) | Board 1 & 2 |
| **6 - 13** | Additives (Cinnamon, Ginger, Peels, Petals, Bergamot) | Board 2, 3, & 4 |
| **14** | **Conveyor System** (12V DC Motor) | Board 4, Motor 2 |
| **15** | **Industrial Mixer** (12V DC Motor) | Board 4, Motor 3 |

## 3. The "Smart Dispatch" Logic
The system uses a sequential automation loop to ensure zero material waste:
1. **Parallel Dispensing:** All 13 scales measure ingredients simultaneously using a quadratic ML voltage model.
2. **Conveyor Interlock:** The conveyor starts *before* the servos drop the tea, ensuring the belt is moving to catch the material.
3. **Timed Mixing:** After the drop sequence (forward 70° tilt), the mixer engages for a programmable cycle.
4. **Rejection (Eject):** If a weight is out of tolerance, the "EJECT" button tilts the servo backward (-70°) to discard the batch.

## 4. Technical Specs
* **Polling Rate:** 25Hz per scale via bit-bang HX711 serial.
* **Safety:** Heartbeat Watchdog Timeout at 0.7s.
* **Logging:** CSV-based persistent storage for Order IDs and actual weights.

# ◈ TeaMatrix Industrial Console  v2.0

**Automated 13-Ingredient Tea Blending, Conveying & Mixing System**
Raspberry Pi 5  ·  4× Cytron Motion 2350 Pro  ·  Python / Tkinter

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Hardware Architecture](#2-hardware-architecture)
3. [Wiring Reference](#3-wiring-reference)
4. [Ingredient & Station Matrix](#4-ingredient--station-matrix)
5. [Software Setup](#5-software-setup)
6. [Running the Application](#6-running-the-application)
7. [User Interface Guide](#7-user-interface-guide)
8. [Dispatch Sequence — State Machine](#8-dispatch-sequence--state-machine)
9. [Recipe System](#9-recipe-system)
10. [Serial Protocol Reference](#10-serial-protocol-reference)
11. [Production Log (CSV)](#11-production-log-csv)
12. [Calibration](#12-calibration)
13. [Tuning Constants](#13-tuning-constants)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. System Overview

TeaMatrix v2.0 controls a **13-hopper** precision tea dispensing line driven by
four Cytron Motion 2350 Pro boards, a conveyor belt, and an industrial mixer.
A Raspberry Pi 5 runs the Tkinter HMI, coordinates all serial communications,
executes the automated dispatch sequence, and logs every batch to CSV.

```
Raspberry Pi 5
├── /dev/ttyACM0  ──  Board 1  →  Stations  1–4   (base teas)
├── /dev/ttyACM1  ──  Board 2  →  Stations  5–8   (additives)
├── /dev/ttyACM2  ──  Board 3  →  Stations  9–12  (botanicals)
└── /dev/ttyACM3  ──  Board 4  →  Station   13    (Bergamot)
                                   Motor slot 2    (Conveyor)
                                   Motor slot 3    (Mixer)
```

---

## 2. Hardware Architecture

### Per-station hardware (×13)

| Component | Spec |
|-----------|------|
| DC gear motor | 12 V, H-bridge via Motion Pro, PWM 20 kHz |
| Servo motor | MG996R, 5 V dedicated rail, 50 Hz, 500–2500 µs |
| Load cell | 100 g capacity |
| ADC | HX711, bit-banged, unique REF_UNIT per cell |

### Conveyor & Mixer (Board 4)

| Component | Motor slot | Duty constant |
|-----------|-----------|---------------|
| Conveyor DC motor (12 V) | Slot 2 (`D:2:<duty>`) | `CONVEYOR_DUTY = 52000` (~80%) |
| Mixer DC motor (12 V)    | Slot 3 (`D:3:<duty>`) | `MIXER_DUTY = 45000` (~69%) |

### Power rails

```
12 V  20 A PSU ──┬── Board VIN (all 4 boards, DC motors, conveyor, mixer)
                 └── DO NOT connect to Pi

5 V   10 A buck ──── All 13× MG996R servo VCC (dedicated rail — mandatory)

Pi 5  USB-C PD  ──── 5 V 5 A dedicated supply

Single common GND: 12 V PSU −, 5 V buck −, Pi GND
```

> **Critical:** Never power servos from the Motion Pro 3.3 V rail or the Pi.
> Servo back-EMF will crash or damage both.

---

## 3. Wiring Reference

### Board 1  (`/dev/ttyACM0`)  —  Stations 1–4

```
Station 1  Scale: CLK GP4 / DAT GP5    Motor: GP8   Servo: GP0
Station 2  Scale: CLK GP6 / DAT GP7    Motor: GP10  Servo: GP1
Station 3  Scale: CLK GP16/ DAT GP17   Motor: GP13  Servo: GP2
Station 4  Scale: CLK GP26/ DAT GP27   Motor: GP15  Servo: GP3
```

### Boards 2 & 3  (`/dev/ttyACM1`, `/dev/ttyACM2`)

Identical GPIO layout — same pin mapping, different USB device.

### Board 4  (`/dev/ttyACM3`)  —  Station 13 + Conveyor + Mixer

```
Station 13  Scale: CLK GP4 / DAT GP5   Motor: GP8   Servo: GP0
Conveyor    Motor: GP10   (D:2:<duty>)
Mixer       Motor: GP13   (D:3:<duty>)
```

> Conveyor and Mixer are pure DC motors — no servo, no HX711 on those slots.

### HX711 wiring (per channel)

```
HX711 VCC → Motion Pro 3V3
HX711 GND → GND
HX711 CLK → GPxx (see above)
HX711 DAT → GPxx (see above)
```

---

## 4. Ingredient & Station Matrix

| ID | Lane | Ingredient | Board | Serial Port |
|----|------|------------|-------|-------------|
| 1  | 01 | Strathspey BOPF    | 1 | /dev/ttyACM0 |
| 2  | 02 | Laxapana Peko      | 1 | /dev/ttyACM0 |
| 3  | 03 | Moray BOP          | 1 | /dev/ttyACM0 |
| 4  | 04 | Silver Tips        | 1 | /dev/ttyACM0 |
| 5  | 05 | Golden Tips        | 2 | /dev/ttyACM1 |
| 6  | 06 | Cinnamon Chips     | 2 | /dev/ttyACM1 |
| 7  | 07 | Ginger Pieces      | 2 | /dev/ttyACM1 |
| 8  | 08 | Orange Peel        | 2 | /dev/ttyACM1 |
| 9  | 09 | Lemon Peel         | 3 | /dev/ttyACM2 |
| 10 | 10 | Lemongrass         | 3 | /dev/ttyACM2 |
| 11 | 11 | Rose Petals        | 3 | /dev/ttyACM2 |
| 12 | 12 | Jasmine Petals     | 3 | /dev/ttyACM2 |
| 13 | 13 | Bergamot Flavour   | 4 | /dev/ttyACM3 |
| —  | CV | Conveyor Motor     | 4 | /dev/ttyACM3 |
| —  | MX | Mixer Motor        | 4 | /dev/ttyACM3 |

---

## 5. Software Setup

### Firmware (each Motion Pro board)

Copy to each board's **CIRCUITPY** drive:

```
CIRCUITPY/
├── hx711_gpio.py
└── code.py
```

`code.py` is **identical** across all 4 boards. Board identity is determined by
USB serial number set in `boot.py` (not used by v2.0 test app — ACM device order is used).

### Raspberry Pi dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-serial python3-tk

# Verify
python3 -c "import serial, tkinter; print('OK')"
```

### User permissions

```bash
sudo usermod -aG dialout $USER
# Log out and back in
```

### UDEV rules (stable device paths)

```bash
# Find serial numbers of each board
for dev in /dev/ttyACM*; do
    echo "$dev: $(udevadm info -a -n $dev | grep 'ATTRS{serial}' | head -1)"
done

# Create rule (replace SERIAL_N with actual values)
sudo tee /etc/udev/rules.d/99-teamatrix.rules <<'EOF'
SUBSYSTEM=="tty", ATTRS{serial}=="SERIAL_1", SYMLINK+="ttyBOARD1"
SUBSYSTEM=="tty", ATTRS{serial}=="SERIAL_2", SYMLINK+="ttyBOARD2"
SUBSYSTEM=="tty", ATTRS{serial}=="SERIAL_3", SYMLINK+="ttyBOARD3"
SUBSYSTEM=="tty", ATTRS{serial}=="SERIAL_4", SYMLINK+="ttyBOARD4"
EOF

sudo udevadm control --reload-rules
```

Then update `BOARD_PORTS` in `teamatrix_v2.py`:

```python
BOARD_PORTS = {
    1: "/dev/ttyBOARD1",
    2: "/dev/ttyBOARD2",
    3: "/dev/ttyBOARD3",
    4: "/dev/ttyBOARD4",
}
```

---

## 6. Running the Application

```bash
python3 teamatrix_v2.py
```

### Auto-start on boot (systemd)

```bash
sudo tee /etc/systemd/system/teamatrix.service <<'EOF'
[Unit]
Description=TeaMatrix Industrial Console v2.0
After=graphical-session.target
Wants=graphical-session.target

[Service]
Type=simple
User=pi
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/pi/.Xauthority
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 /home/pi/teamatrix_v2.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF

sudo systemctl enable teamatrix
sudo systemctl start teamatrix
journalctl -u teamatrix -f
```

---

## 7. User Interface Guide

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ◈ TEAMATRIX v2.0  ⬛E-STOP  [ORD-1001]  IDLE          BRD4 BRD3 BRD2 BRD1 │
├───────────────┬─────────────────────────────────────────────────────────────┤
│ SELECT RECIPE │ LN01 Strathspey  LN02 Laxapana  LN03 Moray  LN04 Silver    │
│ [listbox]     │  85.04g           0.00g          0.00g        0.00g         │
│               │ LN05 Golden      LN06 Cinnamon   LN07 Ginger  LN08 Orange   │
│ INGREDIENTS   │  0.00g           0.00g           0.00g        0.00g         │
│ 1 Strath 85g  │ LN09 Lemon      LN10 Lemon      LN11 Rose    LN12 Jasmine  │
│ 6 Cinna  10g  │  0.00g           0.00g           0.00g        0.00g         │
│ 7 Ginge   5g  │         LN13 Bergamot  0.00g                                │
│               ├─────────────────────────────────────────────────────────────┤
│ BATCH WEIGHT  │  CONVEYOR: IDLE    MIXER: IDLE  30s    SEQUENCE: IDLE       │
│ [50g][80g][100│  [CONV ON][CONV OFF]  [MIX ON][MIX OFF]                    │
│ Custom:[  ][SE├─────────────────────────────────────────────────────────────┤
│               │ SYSTEM LOG                                                  │
│ ▶ DISPATCH    │ 09:41:22  Recipe loaded: Ceylon Spiced Breakfast (100g)     │
│ ⊙ TARE ALL    │ 09:41:25  Order ORD-1001 → Ceylon Spiced Breakfast          │
│               │ 09:41:25    Fill → Station 1 Strathspey BOPF  85.0g        │
│ MIXER: [30]s  └─────────────────────────────────────────────────────────────┤
└───────────────────────────────────────────────────────────────────────────────┘
```

### Per-station card controls

| Button | Label | Action |
|--------|-------|--------|
| `▶` | START | Begin fill sequence for this station |
| `■` | STOP | Emergency stop this station's motor |
| `⬇` | DROP | Execute servo drop sequence |
| `EJ` | EJECT | Servo backward −70° (reject batch) |
| `T` | TARE | Zero this station's scale |

### System strip controls

| Button | Action |
|--------|--------|
| `CONV ON` | Manually start conveyor (Board 4, Motor 2) |
| `CONV OFF` | Stop conveyor |
| `MIX ON` | Manually start mixer (Board 4, Motor 3) |
| `MIX OFF` | Stop mixer |

### Board status LEDs (top-right)

`BRD1`–`BRD4` show green when that board responded on startup, red when offline.
The system continues operating on connected boards even if one board is offline.

---

## 8. Dispatch Sequence — State Machine

When `▶ DISPATCH RECIPE` is pressed, the orchestrator runs this sequence in a
background thread. The UI updates every 40 ms from the main thread.

```
IDLE
  │
  ▼ [DISPATCH pressed]
FILLING ──── All active lanes fill in parallel threads.
  │           EMA-filtered load cell feedback. Pulse mode < 5g.
  │           Waits for all fill threads to complete (timeout 180s).
  │
  ▼
VERIFYING ── Settles 500ms, checks each lane ±0.2g tolerance.
  │           Logs OUT_OF_TOLERANCE warning if exceeded.
  │
  ▼
CONVEYOR ─── Starts conveyor motor (Board 4, D:2:<duty>).
  │           2 second ramp-up before drop.
  │
  ▼
DROPPING ─── All active servos execute drop sequence simultaneously:
  │             0° → smooth to 70° → tap 3× (45°↔70°) → 0° → signal off
  │           Waits for all drop threads. Stops conveyor.
  │
  ▼
MIXING ───── Starts mixer motor (Board 4, D:3:<duty>).
  │           Counts down configured duration (default 30s).
  │           Stops mixer.
  │
  ▼
COMPLETE ─── Writes batch record to production_log.csv.
  │           Displays order result (OK / CHECK TOLERANCES).
  │
  ▼ [3s pause]
IDLE
```

### Emergency stop at any phase

Pressing `⬛ E-STOP` sends `X\n` to all 4 boards simultaneously, stops conveyor
and mixer immediately, and returns state to `IDLE`. A new order can be started
after manually confirming all hoppers are safe.

---

## 9. Recipe System

### Built-in recipes (from blend specification PDF)

| Recipe | Lane | Ingredient | Weight |
|--------|------|------------|--------|
| **Ceylon Spiced Breakfast** | 1 | Strathspey BOPF | 85 g |
| | 6 | Cinnamon Chips | 10 g |
| | 7 | Ginger Pieces | 5 g |
| **Citrus Earl Grey Style** | 2 | Laxapana Peko | 92 g |
| | 8 | Orange Peel | 5 g |
| | 9 | Lemon Peel | 3 g |
| **Ginger Lemongrass** | 3 | Moray BOP | 88 g |
| | 7 | Ginger | 7 g |
| | 10 | Lemongrass | 5 g |
| **Silver Tips Rose & Citrus** | 4 | Silver Tips | 94 g |
| | 11 | Rose Petals | 4 g |
| | 9 | Lemon Peel | 2 g |
| **Golden Tips Jasmine & Citrus** | 5 | Golden Tips | 94 g |
| | 12 | Jasmine Petals | 4 g |
| | 8 | Orange Peel | 2 g |

### Batch weight scaling

The `[50g]` `[80g]` `[100g]` buttons and the custom weight field scale all
ingredient weights proportionally. Example: `[50g]` on Ceylon Spiced Breakfast:
BOPF → 42.5 g, Cinnamon → 5.0 g, Ginger → 2.5 g.

### Adding a recipe

Edit the `RECIPES` dict in `teamatrix_v2.py`. Keys must match entries in
`INGREDIENT_BY_KEY`:

```python
"My New Blend": {
    "STRATHSPEY_BOPF": 80.0,
    "BERGAMOT":        15.0,
    "ROSE_PETALS":      5.0,
},
```

Valid ingredient keys: `STRATHSPEY_BOPF`, `LAXAPANA_PEKO`, `MORAY_BOP`,
`SILVER_TIPS`, `GOLDEN_TIPS`, `CINNAMON`, `GINGER`, `ORANGE_PEEL`,
`LEMON_PEEL`, `LEMONGRASS`, `ROSE_PETALS`, `JASMINE_PETALS`, `BERGAMOT`.

---

## 10. Serial Protocol Reference

All messages are newline-terminated ASCII. Baud rate: 115200. One serial port
per board, each polled by its own background thread at 50 Hz.

### Pi → Board

| Command | Description |
|---------|-------------|
| `W:ALL\n` | Request all 4 weight readings |
| `T:ALL\n` | Tare all 4 scales (3-sample) |
| `T:<N>\n` | Tare scale N (1–4), 5-sample |
| `D:<N>:<duty>\n` | Set motor N duty cycle 0–65535 |
| `S:<N>:<angle>\n` | Servo N angle 0–180°, or -1 to cut signal |
| `X\n` | Emergency stop — all motor duty = 0 immediately |

### Board → Pi

| Response | Description |
|----------|-------------|
| `WA:<w1>:<w2>:<w3>:<w4>` | Weight response in grams (float). `ERR`/`OFF`/`BUSY` if unavailable |
| `T:<N>:DONE` | Tare acknowledged |
| `T:ALL:DONE` | Tare all acknowledged |

### Voltage → duty conversion

```python
duty = int((voltage / 12.0) * 65535)
```

### Watchdog heartbeat

The application sends `W:ALL\n` to every connected board every 500 ms.
The `code.py` firmware has a 700 ms watchdog — if no valid command arrives
within 700 ms all motors stop automatically. The heartbeat prevents this during
idle UI time.

---

## 11. Production Log (CSV)

Every completed order is appended to `production_log.csv` in the application
directory.

### Columns

| Column | Description |
|--------|-------------|
| `OrderID` | Auto-incrementing, format `ORD-NNNN`, starts at 1001 |
| `Timestamp` | `YYYY-MM-DD HH:MM:SS` |
| `Recipe` | Recipe name string |
| `Station` | Station ID 1–13 |
| `Ingredient` | Ingredient display name |
| `Target_g` | Target weight in grams |
| `Actual_g` | Actual weight measured after fill |
| `Delta_g` | Actual − Target (signed) |
| `Status` | `OK` or `OUT_OF_TOLERANCE` |

### Example row

```
ORD-1001,2025-06-01 09:41:35,Ceylon Spiced Breakfast,1,Strathspey BOPF,85.00,85.04,+0.04,OK
```

### Order ID persistence

On startup the app reads the last `OrderID` from the CSV and increments it.
If the file doesn't exist it starts at `ORD-1001`.

---

## 12. Calibration

### HX711 REF_UNIT calibration

Each scale channel requires a unique `REF_UNIT`. To calibrate:

1. Place a precise **100 g calibration weight** on the empty platform.
2. Open a serial terminal to the board (`minicom -b 115200 -D /dev/ttyACM0`).
3. Send `T:1` to tare, wait for `T:1:DONE`.
4. Modify `code.py` temporarily to print the raw HX711 value.
5. `REF_UNIT = raw_value / 100.0`
6. Update `REF_UNITS` array in `code.py` and save to CIRCUITPY.
7. Repeat for all 13 channels (13 entries across 4 boards).

### Servo home check

Before any fill cycle, confirm:
- All servos home to 0° on boot (upright, container closed position).
- 70° = forward pour angle (material falls into conveyor cup).
- −70° = backward eject angle (material falls into reject tray).

Test manually from terminal:
```bash
echo -e "S:1:70\n" > /dev/ttyACM0    # tilt forward
echo -e "S:1:0\n"  > /dev/ttyACM0    # home
echo -e "S:1:-1\n" > /dev/ttyACM0    # cut signal
```

---

## 13. Tuning Constants

All constants are at the top of `teamatrix_v2.py`.

| Constant | Default | Effect |
|----------|---------|--------|
| `POLL_INTERVAL_S` | 0.02 | Serial poll rate (50 Hz). Lower = more CPU |
| `GUI_UPDATE_MS` | 40 | UI refresh rate (25 Hz) |
| `WATCHDOG_MS` | 500 | Heartbeat period (must be < firmware 700 ms) |
| `VOLT_MIN` | 2.0 | Minimum motor voltage during ramp |
| `VOLT_MAX` | 6.0 | Maximum cruise voltage cap |
| `ACCEL_STEP` | 0.2 | Voltage ramp rate per 20 ms tick |
| `PRE_ACT_TIME` | 0.12 | Motor stop lead time (s) — reduce if overshooting |
| `PULSE_VOLTAGE` | 2.8 | Micro-dispense pulse voltage (for <5 g fills) |
| `PULSE_DURATION` | 0.04 | Pulse on-time (s) |
| `PULSE_WAIT` | 0.6 | Settle time between pulses (s) |
| `EMA_ALPHA` | 0.7 | Weight filter speed (0–1, higher = faster) |
| `ZERO_RANGE` | 0.50 | Readings within ±0.5 g of zero displayed as 0.00 |
| `WEIGHT_TOLERANCE` | 0.2 | ±g tolerance for batch verification |
| `MIXER_DURATION_S` | 30 | Default mixer run time |
| `CONVEYOR_DUTY` | 52000 | Conveyor motor PWM (~80% of 12 V) |
| `MIXER_DUTY` | 45000 | Mixer motor PWM (~69% of 12 V) |

### Fill overshoot fix

If filling consistently overshoots by X grams, decrease `PRE_ACT_TIME` by 0.01
increments or decrease `ML_COEF_B` slightly.

### EMA tuning for botanicals

Light floral ingredients (rose petals, jasmine) drift more on the scale.
Set `EMA_ALPHA` lower (0.4) for these channels by adding per-station override
logic if needed.

---

## 14. Troubleshooting

### One or more boards show OFFLINE (red LED)

1. Check USB cable is connected and the CIRCUITPY drive mounts.
2. Check `ls /dev/ttyACM*` — how many devices are listed?
3. Check user is in `dialout` group: `groups | grep dialout`
4. Check port assignment in `BOARD_PORTS` matches actual `/dev/ttyACMx` order.
5. Try `sudo python3 teamatrix_v2.py` to rule out permissions.

> The app will operate on boards that are online. Stations on offline boards
> show weight 0.00 and cannot be filled.

### Weights stuck at 0.00 g

1. Check 3.3 V from Motion Pro → HX711 VCC.
2. Check CLK/DAT pin assignment matches `code.py` `SCALE_PINS`.
3. Send manual tare from UI (`T` button on the station card).
4. Open terminal and send `W:ALL` manually — check raw response.

### Motor doesn't run

1. Check 12 V PSU is on. Check VIN on Motion Pro.
2. Check `MOTOR_PINS` in `code.py` matches physical wiring.
3. Test from terminal: `echo -e "D:1:32768\n" > /dev/ttyACM0` (50% duty, motor 1).
4. If motor runs from terminal but not from app, check BOARD_PORTS mapping.

### Servo doesn't move

1. Check 5 V buck converter is powered. Measure servo VCC with multimeter.
2. Verify `SERVO_PINS` in `code.py`.
3. Test from terminal: `echo -e "S:1:70\n" > /dev/ttyACM0`

### Conveyor/Mixer don't respond

Both are on **Board 4 only** (`/dev/ttyACM3`). Confirm:
- Board 4 LED shows ONLINE (green).
- Motor slot 2 wired to `GP10`, slot 3 wired to `GP13` on Board 4.
- Correct duty constants: `CONVEYOR_HW_SLOT = 2`, `MIXER_HW_SLOT = 3`.

### Consistent weight overshoot

Reduce `PRE_ACT_TIME` in 0.01 s steps. Default is 0.12 s.

### Dispatch never reaches VERIFYING

One or more fill threads are stuck (scale reading `ERR` or motor not responding).
Check individual station status badges. Use `■ STOP` on the stuck station then
`⬛ E-STOP` to reset the orchestrator.

### production_log.csv not created

Check write permissions on the application directory:
```bash
ls -la /home/pi/
touch /home/pi/production_log.csv
```

---

## File Summary

```
teamatrix_v2.py        Main application (single file — all logic included)
production_log.csv     Auto-created on first completed order
README.md              This document
code.py                Motion Pro firmware (identical on all 4 boards)
hx711_gpio.py          HX711 bit-bang driver (deploy to CIRCUITPY)
```

---

*TeaMatrix Industrial Console v2.0 — Raspberry Pi 5 + Cytron Motion 2350 Pro*
