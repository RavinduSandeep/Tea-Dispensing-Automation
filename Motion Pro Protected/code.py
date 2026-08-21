import board
import pwmio
import supervisor
import sys
import time
from hx711_gpio import HX711

# ---------------- CONFIG ----------------
BOARD_ID = 1
WATCHDOG_TIMEOUT_S = 0.7
MOTOR_PWM_FREQ = 20000
SERVO_FREQ = 50

# --- SCALE CONFIG ---
REF_UNITS = [16387, 16387, 16387, 16387] # Calibrate each scale individually later

# Pin Definitions (4 Scales)
SCALE_PINS = [
    (board.GP4, board.GP5),   # Scale 1: CLK, DAT
    (board.GP6, board.GP7),   # Scale 2: CLK, DAT
    (board.GP16, board.GP17), # Scale 3: CLK, DAT
    (board.GP18, board.GP19)  # Scale 4: CLK, DAT
]

# --- FIX: MOTOR PINS UPDATED TO PREVENT PWM CONFLICT ---
MOTOR_PINS = [board.GP8, board.GP10, board.GP13, board.GP15]

# Servo Pins
SERVO_PINS = [board.GP0, board.GP1, board.GP2, board.GP3]

# ---------------- SETUP ----------------
# 1. Motors
motors = []
for pin in MOTOR_PINS:
    motors.append(pwmio.PWMOut(pin, frequency=MOTOR_PWM_FREQ, duty_cycle=0))

# 2. Servos
SERVO_MIN_DUTY = 1638
SERVO_MAX_DUTY = 8192
servos = []
for pin in SERVO_PINS:
    servos.append(pwmio.PWMOut(pin, frequency=SERVO_FREQ, duty_cycle=0))

# 3. Scales
scales = [None, None, None, None]

def init_scale(clk, dat, ref):
    try:
        hx = HX711(clk, dat)
        hx.set_scale(ref)
        if hx.read() is not None:
            hx.tare(times=5) 
            return hx
    except: 
        pass
    return None

print(f"BOOT: Initializing 4-Channel Board {BOARD_ID}...")

for i in range(4):
    scales[i] = init_scale(SCALE_PINS[i][0], SCALE_PINS[i][1], REF_UNITS[i])
    if scales[i]: print(f"Scale {i+1}: ONLINE")
    else: print(f"Scale {i+1}: ERROR")

last_cmd_time = time.monotonic()

# ---------------- FUNCTIONS ----------------
def stop_all_motors():
    for m in motors:
        m.duty_cycle = 0

def set_motor_duty(idx, duty):
    if duty < 0: duty = 0
    if duty > 65535: duty = 65535
    if 0 <= idx < 4:
        motors[idx].duty_cycle = duty

def set_servo_angle(idx, angle):
    if not (0 <= idx < 4): return
    if angle == -1:
        servos[idx].duty_cycle = 0
        return
    angle = max(0, min(180, angle))
    duty = int(SERVO_MIN_DUTY + (angle / 180.0) * (SERVO_MAX_DUTY - SERVO_MIN_DUTY))
    servos[idx].duty_cycle = duty

# ---------------- READY ----------------
stop_all_motors()
for i in range(4):
    set_servo_angle(i, 0)
time.sleep(0.2)
for i in range(4):
    set_servo_angle(i, -1)
print(f"SYSTEM READY. ID:{BOARD_ID}")

# ---------------- LOOP ----------------
while True:
    now = time.monotonic()
    if (now - last_cmd_time) > WATCHDOG_TIMEOUT_S:
        stop_all_motors()
        
    if supervisor.runtime.serial_bytes_available:
        line = sys.stdin.readline().strip()
        if not line: continue
        last_cmd_time = now
        
        # BATCH WEIGHT READ 
        if line == "W:ALL":
            out = ["WA"]
            for i in range(4):
                if scales[i]:
                    val = scales[i].get_units(1)
                    out.append(f"{val:.2f}" if val is not None else "ERR")
                else:
                    out.append("OFF")
            print(":".join(out))
            
        elif line.startswith("T:"):
            try:
                if line == "T:ALL":
                    for s in scales:
                        if s: s.tare(times=3)
                    print("T:ALL:DONE")
                else:
                    idx = int(line.split(":")[1]) - 1
                    if 0 <= idx < 4 and scales[idx]:
                        scales[idx].tare(times=5)
                        print(f"T:{idx+1}:DONE")
            except: pass
            
        elif line.startswith("D:"):
            try:
                parts = line.split(":")
                idx = int(parts[1]) - 1
                duty = int(parts[2])
                set_motor_duty(idx, duty)
            except: pass
            
        elif line.startswith("S:"):
            try:
                parts = line.split(":")
                idx = int(parts[1]) - 1
                angle = int(parts[2])
                set_servo_angle(idx, angle)
            except: pass
            
        elif line == "X":
            stop_all_motors()