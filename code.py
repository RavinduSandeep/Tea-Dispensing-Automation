"""
code.py  —  TeaMatrix Industrial Console v2.0  Firmware
=========================================================
Target  : Cytron Motion 2350 Pro  (RP2350, CircuitPython 10+)
Deploy  : Copy this file + hx711_gpio.py to every board's CIRCUITPY drive.
          Change BOARD_ID (1–4) on each board before copying.

Board mapping:
    BOARD_ID = 1   /dev/ttyACM0   Stations 1–4   (base teas)
    BOARD_ID = 2   /dev/ttyACM1   Stations 5–8   (additives)
    BOARD_ID = 3   /dev/ttyACM2   Stations 9–12  (botanicals)
    BOARD_ID = 4   /dev/ttyACM3   Station 13 (Bergamot) + Conveyor(M2) + Mixer(M3)

Serial protocol (115200 baud, newline-terminated ASCII):

  Pi → Board             Board → Pi
  ----------             ----------
  W:ALL                  WA:<w1>:<w2>:<w3>:<w4>
  T:ALL                  T:ALL:DONE
  T:<N>                  T:<N>:DONE
  D:<N>:<duty 0-65535>   (no reply)
  S:<N>:<angle -90..+90> (no reply — signed, centred on neutral; clamped in firmware)
  S:<N>:-9999            (servo signal off / detach — safe rest, no holding current)
  X                      (no reply — all motors stop immediately)
"""

import board
import pwmio
import supervisor
import sys
import time
from hx711_gpio import HX711

# ============================================================
#  ▶  EDIT THIS  ◀  —  Unique per board  (1, 2, 3, or 4)
# ============================================================
BOARD_ID = 1

# ============================================================
# TUNING CONSTANTS
# ============================================================
WATCHDOG_TIMEOUT_S = 0.7     # If no command arrives within this time, stop all motors
MOTOR_PWM_FREQ     = 20000   # Hz — matches Cytron recommended 20 kHz
SERVO_FREQ         = 50      # Hz — standard 50 Hz servo signal

# ── SERVO PWM MAPPING ────────────────────────────────────────────────────────
# Safe pulse band for hobby servos (SG90 / MG90S / MG996R) is 1000–2000 µs.
# Driving below ~900 µs or above ~2100 µs forces the gear train past its
# internal end stop. The internal motor stalls against the stop, the H-bridge
# in the servo controller PCB sees full stall current continuously, and the
# winding / driver IC burns out — even when the user thinks the servo is
# "at rest" at 0° or 180°.
#
# OLD (UNSAFE) MAPPING:
#   SERVO_MIN_DUTY = 1638  (≈ 500 µs → past lower stop)
#   SERVO_MAX_DUTY = 8192  (≈ 2500 µs → past upper stop)
#   angle 0..180 mapped linearly into 500..2500 µs
#   → every "home to 0°" command stalled the servo against the stop.
#   → every "180°" command did the same on the upper side.
#
# NEW (SAFE) MAPPING — signed-angle, centred on neutral:
#   angle  -90° → 1000 µs (≈ duty 3277) — lower mechanical limit, safe
#   angle    0° → 1500 µs (≈ duty 4915) — servo neutral / no stress
#   angle  +90° → 2000 µs (≈ duty 6554) — upper mechanical limit, safe
# Existing station_config.json values (e.g. drop_angle_end=70, eject_angle_end=−70)
# are honoured directly; the physical resting position becomes the servo's
# centre, which is the correct mechanical neutral.
SERVO_NEUTRAL_DUTY   = 4915      # ≈ 1500 µs at 50 Hz (65535-step duty)
SERVO_DUTY_PER_DEG   = 18.21     # (6554 − 3277) / 180  → ≈ 5.56 µs per °
SERVO_ANGLE_MIN      = -90
SERVO_ANGLE_MAX      =  90

# Detach (PWM signal off) sentinel. Must be a value that CANNOT be produced
# by any legitimate sweep range. The previous sentinel of -1 collided with
# eject sweeps that step through -1 on their way to -70°, which caused the
# servo to be detached mid-sweep and then re-attached at the past-stop
# position — a classic SG90/MG90S burn pattern.
SERVO_DETACH_SENTINEL = -9999

# ============================================================
# SCALE CALIBRATION  (REF_UNIT per channel)
# ============================================================
# HOW TO CALIBRATE:
#   1. Place a known 100g weight. Run T:<ch> to tare.
#   2. Read raw via temporary debug print of hx.read().
#   3. REF_UNIT = raw_reading / 100.0
#   4. Update and redeploy.
REF_UNITS = [
    16387,   # Channel 1
    16387,   # Channel 2
    16387,   # Channel 3
    16387,   # Channel 4
]

# ============================================================
# PIN DEFINITIONS  (common to all 4 boards)
# ============================================================
# --- Scales: (CLK pin, DAT pin) per channel ---
SCALE_PINS = [
    (board.GP4,  board.GP5),    # Channel 1
    (board.GP6,  board.GP7),    # Channel 2
    (board.GP16, board.GP17),   # Channel 3
    (board.GP26, board.GP27),   # Channel 4
]

# --- DC Motor PWM outputs ---
MOTOR_PINS = [
    board.GP8,    # Motor 1  (Station -1  / Conveyor on Board 4 Ch1 maps here)
    board.GP10,   # Motor 2  (Conveyor on Board 4)
    board.GP13,   # Motor 3  (Mixer on Board 4)
    board.GP15,   # Motor 4  (Spare / reserved)
]

# --- Servo PWM outputs ---
SERVO_PINS = [
    board.GP0,   # Servo 1
    board.GP1,   # Servo 2
    board.GP2,   # Servo 3
    board.GP3,   # Servo 4
]

# ============================================================
# HARDWARE SETUP
# ============================================================
print(f"\n{'='*44}")
print(f" TeaMatrix v2.0  Board {BOARD_ID}  —  Initialising")
print(f"{'='*44}")

# --- Motors ---
motors: list[pwmio.PWMOut] = []
for pin in MOTOR_PINS:
    motors.append(pwmio.PWMOut(pin, frequency=MOTOR_PWM_FREQ, duty_cycle=0))
print(f"Motors    : {len(motors)} channels ready  {[p for p in MOTOR_PINS]}")

# --- Servos ---
servos: list[pwmio.PWMOut] = []
for pin in SERVO_PINS:
    servos.append(pwmio.PWMOut(pin, frequency=SERVO_FREQ, duty_cycle=0))
print(f"Servos    : {len(servos)} channels ready")

# --- Scales ---
scales: list["HX711 | None"] = [None, None, None, None]

# Board 4 only has 1 ingredient (Ch 1 = Bergamot). Ch 2/3/4 are motors with no scale.
# We still attempt init on all channels — motors have no HX711 so init will return None safely.
NUM_CHANNELS = 4
if BOARD_ID == 4:
    NUM_CHANNELS = 1   # Only channel 1 has a load cell on Board 4

def init_scale(clk_pin, dat_pin, ref_unit):
    """Attempt to initialise one HX711.  Returns HX711 object or None on failure."""
    try:
        hx = HX711(clk_pin, dat_pin, gain=128)
        hx.set_scale(ref_unit)
        # Quick sanity-check read before tare
        test = hx.read(timeout_s=1.0)
        if test is None:
            return None
        hx.tare(times=5)
        return hx
    except Exception as e:
        return None

print("Scales    :", end="")
for i in range(NUM_CHANNELS):
    clk, dat = SCALE_PINS[i]
    scales[i] = init_scale(clk, dat, REF_UNITS[i])
    state = "ONLINE" if scales[i] else "ERROR"
    print(f"  Ch{i+1}:{state}", end="")
print()

if BOARD_ID == 4:
    print("Board 4   : Ch2=Conveyor  Ch3=Mixer  Ch4=Spare (no scales)")

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def stop_all_motors():
    """Set every motor duty cycle to 0 immediately (watchdog + E-STOP)."""
    for m in motors:
        m.duty_cycle = 0

def set_motor_duty(channel_1indexed: int, duty: int):
    """
    Set motor PWM duty.
    channel_1indexed : 1–4
    duty             : 0–65535
    """
    idx = channel_1indexed - 1
    if not (0 <= idx < 4):
        return
    duty = max(0, min(65535, int(duty)))
    motors[idx].duty_cycle = duty

def angle_to_duty(angle) -> int:
    """Convert signed servo angle [-90°, +90°] to a duty cycle in the safe
    1000–2000 µs PWM band. Angles outside the range are clamped — the firmware
    will NEVER drive the servo into its internal end stop, regardless of what
    the host commands."""
    a = max(SERVO_ANGLE_MIN, min(SERVO_ANGLE_MAX, angle))
    return int(SERVO_NEUTRAL_DUTY + a * SERVO_DUTY_PER_DEG)

def set_servo_angle(channel_1indexed: int, angle):
    """
    Move servo to angle, or detach.
    channel_1indexed : 1–4
    angle            : signed degrees in [-90, +90], or SERVO_DETACH_SENTINEL
                       (-9999) to cut the PWM signal (saves power at rest).
    """
    idx = channel_1indexed - 1
    if not (0 <= idx < 4):
        return
    if angle == SERVO_DETACH_SENTINEL:
        servos[idx].duty_cycle = 0   # Cut signal — servo holds position mechanically
        return
    servos[idx].duty_cycle = angle_to_duty(angle)

def read_all_weights() -> list:
    """
    Poll all scale channels.  Returns a list of 4 strings:
    - "<float:.2f>" for valid readings
    - "ERR"  if the scale returned None (HX711 timeout)
    - "OFF"  if the channel has no HX711 (non-scale channels on Board 4)
    """
    results = []
    for i in range(4):
        if scales[i] is None:
            results.append("OFF")
        else:
            val = scales[i].get_units(1)
            results.append(f"{val:.2f}" if val is not None else "ERR")
    return results

# ============================================================
# STARTUP — Home servos one at a time, then detach
# ============================================================
# Staggered home prevents the inrush surge that occurs when 4 servos energise
# simultaneously into the 5 V rail (4 × ~600 mA peak = ~2.4 A from a rail
# shared with the HX711 / Pi). Each servo is moved to neutral (1500 µs —
# zero mechanical stress) and then DETACHED, so the gear train holds
# position passively with no current draw.
stop_all_motors()
for ch in range(1, 5):
    set_servo_angle(ch, 0)                       # neutral (1500 µs, safe)
    time.sleep(0.25)                             # let it actually reach the angle
    set_servo_angle(ch, SERVO_DETACH_SENTINEL)   # detach — no holding current
    time.sleep(0.05)                             # small inter-channel gap

print(f"\n{'='*44}")
print(f" SYSTEM READY   Board ID: {BOARD_ID}")
print(f"{'='*44}\n")

# ============================================================
# MAIN COMMAND LOOP
# ============================================================
last_cmd_time = time.monotonic()

while True:
    now = time.monotonic()

    # ---------- WATCHDOG ----------
    # If no serial command has been received for WATCHDOG_TIMEOUT_S seconds,
    # halt all motors.  The Pi heartbeat (W:ALL every 500ms) prevents false trips.
    if (now - last_cmd_time) > WATCHDOG_TIMEOUT_S:
        stop_all_motors()

    # ---------- SERIAL READ ----------
    if not supervisor.runtime.serial_bytes_available:
        continue   # Nothing pending — tight loop to maintain watchdog timing

    raw = sys.stdin.readline()
    line = raw.strip() if raw else ""
    if not line:
        continue

    last_cmd_time = time.monotonic()   # Reset watchdog on any valid input

    # ===========================================================
    #  COMMAND: W:ALL  —  Batch weight read
    #  Response: WA:<w1>:<w2>:<w3>:<w4>
    # ===========================================================
    if line == "W:ALL":
        parts = read_all_weights()
        print("WA:" + ":".join(parts))

    # ===========================================================
    #  COMMAND: T:ALL  —  Tare all scales
    #  COMMAND: T:<N>  —  Tare individual scale (1-indexed)
    # ===========================================================
    elif line.startswith("T:"):
        try:
            if line == "T:ALL":
                for s in scales:
                    if s:
                        s.tare(times=3)
                print("T:ALL:DONE")
            else:
                ch = int(line.split(":")[1])
                idx = ch - 1
                if 0 <= idx < 4 and scales[idx]:
                    scales[idx].tare(times=5)
                    print(f"T:{ch}:DONE")
        except Exception:
            pass

    # ===========================================================
    #  COMMAND: D:<N>:<duty>  —  DC motor PWM
    #  N = channel 1–4,  duty = 0–65535
    #  Board 4: D:2:<duty> = Conveyor,  D:3:<duty> = Mixer
    # ===========================================================
    elif line.startswith("D:"):
        try:
            parts = line.split(":")
            ch    = int(parts[1])
            duty  = int(parts[2])
            set_motor_duty(ch, duty)
        except Exception:
            pass

    # ===========================================================
    #  COMMAND: S:<N>:<angle>  —  Servo position
    #  N = channel 1–4,  angle = 0–180 or -1 (signal off)
    # ===========================================================
    elif line.startswith("S:"):
        try:
            parts = line.split(":")
            ch    = int(parts[1])
            angle = int(parts[2])
            set_servo_angle(ch, angle)
        except Exception:
            pass

    # ===========================================================
    #  COMMAND: X  —  Emergency stop (all motors to 0 immediately)
    # ===========================================================
    elif line == "X":
        stop_all_motors()

    # ===========================================================
    #  COMMAND: ID?  —  Board identity query (for auto-discovery)
    # ===========================================================
    elif line == "ID?":
        print(f"Board {BOARD_ID}")

    # Unknown command — silently ignore to avoid blocking
