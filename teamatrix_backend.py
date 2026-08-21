"""
teamatrix_backend.py  —  TeaMatrix Industrial Console v3.0
Hardware coordinator, station logic, and orchestrator.
Imported by teamatrix_pro.py (UI layer).

v3.0 additions:
  - Inventory tracking with live deduction and low-stock detection
  - Recipe persistence (recipes.json) + 5 base tea recipes
  - Pre-dispatch stock validation (checks ALL lanes before anything runs)
  - Conveyor starts with the first fill (not after all fills complete)
  - Refill logging
"""
import time, serial, threading, math, csv, os, json
from collections import deque
from datetime import datetime

# ─── CONSTANTS ────────────────────────────────────────────────────────────────
BOARD_PORTS={1:"/dev/module_01",2:"/dev/module_02",3:"/dev/module_03",4:"/dev/module_04"}
BAUD_RATE=115200; POLL_INTERVAL_S=0.02; WATCHDOG_INTERVAL_S=0.5
MIXER_DURATION_S=60; CONVEYOR_CH=2; MIXER_CH=3
CONVEYOR_DUTY=52000; MIXER_DUTY=40000
# FIX (production-log not visible): use absolute paths anchored to the
# directory of this source file so the app reads/writes the SAME CSV/JSON
# no matter which CWD the launcher used (systemd, terminal, IDE).
_APP_DIR=os.path.dirname(os.path.abspath(__file__))
LOG_FILE        =os.path.join(_APP_DIR,"production_log.csv")
CONFIG_FILE     =os.path.join(_APP_DIR,"station_config.json")
RECIPES_FILE    =os.path.join(_APP_DIR,"recipes.json")
INVENTORY_FILE  =os.path.join(_APP_DIR,"inventory.json")
REFILL_LOG_FILE =os.path.join(_APP_DIR,"refill_log.csv")
SUPPLY_VOLTAGE=12.0
ML_A,ML_B,ML_C=-0.0677,1.6538,0.5615
VOLT_MIN,VOLT_MAX=2.0,6.0; ACCEL_STEP=0.2
PULSE_VOLTAGE=2.8; PULSE_DURATION=0.04; PULSE_WAIT=0.6; PRE_ACT_TIME=0.12
EMA_ALPHA=0.7; DEAD_ZONE=0.01; ZERO_RANGE=0.50
# ─── SCALE FILTERING (spike rejection / debounce) ─────────────────────────────
# HX711 channels occasionally emit huge sudden values (electrical noise, EMI
# from motors, glitching DAT line). The smoothing pipeline below rejects those
# without slowing the fill loop:
#   1. raw value from board
#   2. MAX_JUMP rejection: if |raw - last_kept_raw| exceeds MAX_JUMP_G, the
#      sample is dropped (treated as a spike). After SPIKE_TOLERATE_N
#      consecutive "spike" samples it is accepted (real step change, e.g.
#      operator placed a calibration weight).
#   3. median over MED_BUF_LEN samples (was 3, raised to 5 for stronger
#      robustness without adding latency for the fill loop).
#   4. EMA at EMA_ALPHA for ripple suppression.
MED_BUF_LEN          = 5
MAX_JUMP_G           = 25.0    # g — anything bigger than this in one sample is a spike
SPIKE_TOLERATE_N     = 3       # consecutive "spike" samples force acceptance (real change)
SCALE_DEBOUNCE_S     = 0.04    # ignore raw reads occurring less than this apart
HARD_WD_S=0.7; ERR_RETRY=3; VIBE_FREEZE_S=2.0
TECH_PIN="2350"
MAINT_WARN_HOURS=500.0
LOW_STOCK_THRESH=100.0      # grams — below this a yellow warning appears
DEFAULT_CAPACITY=1000.0     # grams per container default

# Per-station dispensing defaults (overridable via station_config.json / Behavior tab)
DEFAULT_DISPENSE_MODE="auto"          # "auto" | "micro" | "normal"
DEFAULT_MICRO_THRESHOLD_G=5.0         # auto-mode pulse-vs-continuous cutoff
DEFAULT_DECEL_FACTOR=1.5              # multiplied by flow to find decel point
DEFAULT_ML_ENABLED=True               # gate solve_v() per station

# Drop-validation tolerance (flat ± grams). Distinct from tolerance() which
# governs when the fill loop exits — this is the post-fill "is it safe to drop"
# check. Out-of-tolerance fills trigger the operator-decision modal.
# DROP_TOLERANCE_G is the legacy global default; per-station scale_tolerance_grams
# (loaded from station_config.json) overrides it during weight validation.
DROP_TOLERANCE_G=3.0
DEFAULT_SCALE_TOLERANCE_G=3.0         # per-station overridable, default ±3g
DEFAULT_FALL_DELAY_S=2.0              # post-dispense gravity/flow settle (s), per-station overridable
DEFAULT_NO_DATA_TIMEOUT_S=10.0        # max wait for first valid scale movement before NO-DATA error
# Per-station target offset (g) — "stop early" semantics (operator-requested).
# POSITIVE value  : auger stops this many grams BEFORE the recipe target
#                   (e.g. target=5 g, offset=0.5 → fill stops at 4.5 g).
# NEGATIVE value  : auger aims to finish ABOVE the recipe target
#                   (rarely useful — only when post-cutoff in-flight is
#                   systematically subtracting too much).
# 0.0 keeps the legacy behavior. The offset is applied to eff_tgt in
# Station._fill_t AS WELL AS to the top-up and rescue stop-checks, so the
# entire fill loop respects the same "land at tgt − offset" target.
# Clamped to ±5 g for safety.
DEFAULT_TARGET_OFFSET_G=0.0
TARGET_OFFSET_MAX_G=5.0               # safety clamp — never aim more than ±5 g off-target

# ─── PRECISION DISPENSING (coarse → fine → settle → micro top-up) ─────────────
# Drives the two-phase fill in Station._fill_t. All values safe defaults and
# overridable per-station via station_config.json. Aim: keep |actual-target|
# under ~0.1 g across stations by predicting in-flight material and learning
# per-station residual overshoot.
# AUTO-mode "micro-tail" — when a station is set to AUTO and the recipe target
# is BIGGER than the per-station micro_threshold_g (so the fill goes COARSE),
# the last AUTO_MICRO_TAIL_G grams are still delivered by the pulse engine
# instead of by the fine-fill voltage. Effect: bulk material is poured fast,
# the auger slows down before the boundary, and the final approach is gentle
# and precise (operator-requested behavior).
#
# Example with target=10 g and tail=3 g:
#   0g → ~7g    : COARSE     (cruise voltage; auger at full speed)
#   ~7g          : SLOWDOWN   (auger voltage decays — UI shows MICRO_TAIL)
#   ~7g → 10g   : MICRO      (pulse-mode, gap-adaptive pulses)
#
# For target ≤ micro_threshold_g (default 5 g) the WHOLE fill is in
# pulse-mode — see `use_pulse=(mode=="micro") or (mode=="auto" and tgt<=thr)`
# in Station._fill_t. Set auto_micro_tail_g=0 to disable the tail and use
# the legacy COARSE→FINE flow for larger targets.
DEFAULT_AUTO_MICRO_TAIL_G     = 3.0     # (legacy, kept for backward compat in saved JSON)
AUTO_MICRO_TAIL_MAX_G         = 25.0   # safety clamp
# SLOWDOWN deceleration ramp — for NORMAL dispensing (tgt > 5 g), the auger
# voltage decays linearly from cruise → cruise*0.4 across the last
# MICRO_TAIL_SLOWDOWN_G grams BEFORE the bulk-stop boundary. Keeps the
# original "decelerate before stopping" behavior the ML model expects.
MICRO_TAIL_SLOWDOWN_G         = 1.0

# ─── DISPENSING DECISION + PRE-STOP + SETTLE + MICRO-PULSE STAGE ──────────────
# UNIVERSAL operator-spec late-fill behavior:
#   • target ≤ micro_threshold_g  → PURE MICRO: pulse-check-wait loop from
#     the start, no bulk phase, target = eff_tgt.
#   • target  > micro_threshold_g → NORMAL: ML-voltage cruise with accel +
#     SLOWDOWN deceleration until BULK_STOP = target − final_micro_amount_g
#     (default 3 g), motor OFF, wait settling_delay_s, stable scale read,
#     then the same micro pulse-check-wait loop fills the remaining grams
#     up to eff_tgt. The final 0.5 g uses extra-careful smaller / softer
#     pulses (handled inside the micro loop).
# Both values are per-station overridable via the Behavior tab.
DEFAULT_FINAL_MICRO_AMOUNT_G  = 3.0     # remaining grams handed off to micro
FINAL_MICRO_AMOUNT_MAX_G      = 10.0    # safety clamp
DEFAULT_SETTLING_DELAY_S      = 1.0     # motor-off wait between bulk and micro
SETTLING_DELAY_MAX_S          = 30.0    # safety clamp
# Extra-careful threshold inside the micro loop. Once the gap to target
# drops below this, pulses shrink to the TINY duration and use a softer
# voltage (PULSE_VOLTAGE * MICRO_FINE_VOLT_FACTOR) so the system can land
# accurately on small remaining amounts.
MICRO_FINE_GAP_G              = 0.5
MICRO_FINE_VOLT_FACTOR        = 0.8
DEFAULT_COARSE_MARGIN_G       = 5.0    # coarse fill stops this far below target
# FIX (early-cutoff bug): previous defaults (fine_m=1.0, inflight_c=0.50) made
# the loop subtract up to 1.5 g+ from the target before stopping the auger.
# The top-up loop (only 10 small pulses) could not always recover that gap,
# leading to chronic 0.4–0.5 g undershoot complaints. Tightened margins +
# bigger pulses + bigger top-up budget close the gap accurately without
# overshooting.
DEFAULT_FINE_MARGIN_G         = 0.30   # fine fill stops this far below target (before in-flight)
DEFAULT_INFLIGHT_COMP_G       = 0.15   # static base in-flight estimate (g)
DEFAULT_LEARNED_COMP_G        = 0.0    # adaptive per-station overshoot bias (g, auto-tuned)
DEFAULT_SETTLE_MS             = 500    # post-stop settle before stable-read window (ms)
DEFAULT_STABLE_WINDOW_G       = 0.05   # consecutive reads within this are "stable" (g)
DEFAULT_STABLE_SAMPLE_CNT     = 4      # required consecutive stable samples
DEFAULT_PULSE_MS_LARGE        = 110    # gap > 2.0 g
DEFAULT_PULSE_MS_MEDIUM       = 60     # gap > 0.8 g
DEFAULT_PULSE_MS_SMALL        = 35     # gap > 0.3 g
DEFAULT_PULSE_MS_TINY         = 20     # gap ≤ 0.3 g
DEFAULT_MAX_TOPUP_PULSES      = 30     # increased so chronic small-gap fills can finish
LEARN_ALPHA                   = 0.30   # EMA weight on per-fill residual overshoot
PRECISION_LEARNED_CLAMP_G     = 3.0    # safety cap on |learned_compensation_g|
FLOW_RATE_WINDOW_S            = 0.5    # rolling window for live flow estimate
SYSTEM_DELAY_S                = 0.08   # cmd→motor-off latency (s) added to in-flight
# Top-up safety net: after the regular pulse budget is exhausted, if the fill
# is still measurably short of target, fire a small batch of stronger pulses.
# This is the final guard against the 0.4–0.5 g chronic undershoot.
TOPUP_RESCUE_PULSES           = 8      # extra pulses available after max_topup_pulses
TOPUP_RESCUE_VOLTAGE          = 3.4    # slightly stronger than PULSE_VOLTAGE for finish-line
TOPUP_RESCUE_TRIGGER_G        = 0.10   # rescue only if still ≥0.10 g short after main top-up

# ─── Servo safety / health ────────────────────────────────────────────────────
# After every drop/eject the servo is RELEASED (PWM detached) so it never
# sits holding load against a mechanical stop. The settle delay below gives
# the linkage time to physically reach the commanded angle BEFORE the detach
# so the servo isn't released mid-motion.
#
# SERVO_DETACH_SENTINEL: signalling value sent over serial to mean "cut PWM
# now". Must be a value the eject/drop sweeps can NEVER generate; the old
# sentinel of -1 collided with eject sweeps that step through -1 on their
# way to -70° (causing detach mid-sweep followed by past-stop re-attach —
# a known SG90/MG90S burn pattern). -9999 is safely outside any sweep range.
SERVO_DETACH_SENTINEL = -9999
SERVO_DETACH_SETTLE_S=0.15            # wait after final move before sending detach
SERVO_DIRECTION_CHANGE_PAUSE_S=0.05   # brief pause when a sweep reverses direction
SERVO_MIN_CMD_INTERVAL_S=0.005        # rate limit between consecutive servo writes
SERVO_MOVE_TIMEOUT_S_DEFAULT=8.0      # per-station overridable cap on one sequence
SERVO_COOLDOWN_RUN_S=30.0             # cumulative active s within window → cooldown
SERVO_COOLDOWN_WINDOW_S=60.0          # rolling window for runtime accounting
SERVO_COOLDOWN_DURATION_S=5.0         # forced rest period (overheat/stall protection)
DEFAULT_SAFE_ANGLE_MIN=-90.0          # widest hardware envelope (per-station overridable)
DEFAULT_SAFE_ANGLE_MAX=120.0
SAFE_ANGLE_MIN_LIMIT=-90.0            # absolute clamps applied to per-station safe range
SAFE_ANGLE_MAX_LIMIT=120.0
OPERATOR_DECISION_TIMEOUT_S=600.0     # 10 minutes → defaults to CANCEL

# Catalogue of available ingredient names (lives next to station_config.json)
CATALOGUE_FILE="ingredients_catalogue.json"

# Manual diagnostics safety
MAX_MANUAL_MOTOR_S=3.0                # cap on how long a manual motor pulse can run
MANUAL_TEST_VOLTAGE=2.5               # volts for the Diagnostics MOTOR ON button
MANUAL_DISP_TEST_TARGET_G=5.0         # default target for the DISP TEST button

# Per-station servo sequence defaults. All values overridable via station_config.json.
DEFAULT_SERVO={
    "drop_angle_start": 0,
    "drop_angle_end":   70,
    "drop_speed_dt":    0.010,
    "drop_hold_s":      0.5,
    "drop_tap_count":   3,
    "drop_tap_low":     45,
    "drop_tap_high":    70,
    "drop_tap_dt":      0.18,
    "eject_angle_start": 0,
    "eject_angle_end":   -70,
    "eject_speed_dt":    0.015,
    "eject_hold_s":      0.8,
    "return_angle":      0,
    "return_speed_dt":   0.015,
}

_SERVO_RANGES={
    "drop_angle_start":(-90,90),  "drop_angle_end":(0,120),
    "drop_speed_dt":(0.005,0.05), "drop_hold_s":(0.0,5.0),
    "drop_tap_count":(0,10),       "drop_tap_low":(0,90),
    "drop_tap_high":(0,120),       "drop_tap_dt":(0.05,0.5),
    "eject_angle_start":(-90,90), "eject_angle_end":(-120,0),
    "eject_speed_dt":(0.005,0.05),"eject_hold_s":(0.0,5.0),
    "return_angle":(-10,10),      "return_speed_dt":(0.005,0.05),
}

def _coerce_servo(raw)->dict:
    """Clamp every servo field to its valid range; backfill missing fields
    from DEFAULT_SERVO. Accepts None / non-dict and returns a fresh dict."""
    out=dict(DEFAULT_SERVO)
    if isinstance(raw,dict):
        for k,(lo,hi) in _SERVO_RANGES.items():
            if k in raw:
                try:
                    v=raw[k]
                    if k=="drop_tap_count":
                        v=int(v)
                    else:
                        v=int(v) if isinstance(DEFAULT_SERVO[k],int) else float(v)
                    out[k]=max(lo,min(hi,v))
                except Exception:
                    out[k]=DEFAULT_SERVO[k]
    # Cross-field sanity
    if out["drop_tap_high"]<=out["drop_tap_low"]:
        out["drop_tap_high"]=min(_SERVO_RANGES["drop_tap_high"][1], out["drop_tap_low"]+5)
    if out["eject_angle_end"]>=out["eject_angle_start"]:
        out["eject_angle_end"]=max(_SERVO_RANGES["eject_angle_end"][0], out["eject_angle_start"]-30)
    return out

# ─── DEFAULT INGREDIENT MAP ────────────────────────────────────────────────────
_DEFAULTS={
    1:{"label":"Strathspey BOPF","board":1,"ch":1},
    2:{"label":"Laxapana Peko",  "board":1,"ch":2},
    3:{"label":"Moray BOP",      "board":1,"ch":3},
    4:{"label":"Silver Tips",    "board":1,"ch":4},
    5:{"label":"Golden Tips",    "board":2,"ch":1},
    6:{"label":"Cinnamon Chips", "board":2,"ch":2},
    7:{"label":"Ginger Pieces",  "board":2,"ch":3},
    8:{"label":"Orange Peel",    "board":2,"ch":4},
    9:{"label":"Lemon Peel",     "board":3,"ch":1},
    10:{"label":"Lemongrass",    "board":3,"ch":2},
    11:{"label":"Rose Petals",   "board":3,"ch":3},
    12:{"label":"Jasmine Petals","board":3,"ch":4},
    13:{"label":"Bergamot",      "board":4,"ch":1},
}

_BEHAVIOR_FIELDS=("label","ingredient_name","dispense_mode",
                  "micro_threshold_g","accel_step","decel_factor",
                  "ml_model_enabled","servo",
                  "scale_tolerance_grams","fall_delay_seconds",
                  "target_offset_g",
                  "safe_angle_min","safe_angle_max","servo_move_timeout_s",
                  # precision dispensing (overshoot reduction)
                  "coarse_margin_g","fine_margin_g","inflight_compensation_g",
                  "learned_compensation_g","settle_ms","stable_window_g",
                  "stable_sample_count","pulse_ms_large","pulse_ms_medium",
                  "pulse_ms_small","pulse_ms_tiny","max_topup_pulses",
                  # AUTO-mode micro-tail (last N grams pulse-dispensed)
                  "auto_micro_tail_g",
                  # Pre-stop + settle + final-micro stage (operator-spec)
                  "final_micro_amount_g","settling_delay_s")

def _apply_behavior_defaults(entry):
    entry.setdefault("ingredient_name",entry.get("label",""))
    entry.setdefault("dispense_mode",DEFAULT_DISPENSE_MODE)
    entry.setdefault("micro_threshold_g",DEFAULT_MICRO_THRESHOLD_G)
    entry.setdefault("accel_step",ACCEL_STEP)
    entry.setdefault("decel_factor",DEFAULT_DECEL_FACTOR)
    entry.setdefault("ml_model_enabled",DEFAULT_ML_ENABLED)
    entry.setdefault("scale_tolerance_grams",DEFAULT_SCALE_TOLERANCE_G)
    entry.setdefault("fall_delay_seconds",DEFAULT_FALL_DELAY_S)
    entry.setdefault("target_offset_g",DEFAULT_TARGET_OFFSET_G)
    entry.setdefault("safe_angle_min",DEFAULT_SAFE_ANGLE_MIN)
    entry.setdefault("safe_angle_max",DEFAULT_SAFE_ANGLE_MAX)
    entry.setdefault("servo_move_timeout_s",SERVO_MOVE_TIMEOUT_S_DEFAULT)
    # precision dispensing defaults
    entry.setdefault("coarse_margin_g",        DEFAULT_COARSE_MARGIN_G)
    entry.setdefault("fine_margin_g",          DEFAULT_FINE_MARGIN_G)
    entry.setdefault("inflight_compensation_g",DEFAULT_INFLIGHT_COMP_G)
    entry.setdefault("learned_compensation_g", DEFAULT_LEARNED_COMP_G)
    entry.setdefault("settle_ms",              DEFAULT_SETTLE_MS)
    entry.setdefault("stable_window_g",        DEFAULT_STABLE_WINDOW_G)
    entry.setdefault("stable_sample_count",    DEFAULT_STABLE_SAMPLE_CNT)
    entry.setdefault("pulse_ms_large",         DEFAULT_PULSE_MS_LARGE)
    entry.setdefault("pulse_ms_medium",        DEFAULT_PULSE_MS_MEDIUM)
    entry.setdefault("pulse_ms_small",         DEFAULT_PULSE_MS_SMALL)
    entry.setdefault("pulse_ms_tiny",          DEFAULT_PULSE_MS_TINY)
    entry.setdefault("max_topup_pulses",       DEFAULT_MAX_TOPUP_PULSES)
    entry.setdefault("auto_micro_tail_g",      DEFAULT_AUTO_MICRO_TAIL_G)
    entry.setdefault("final_micro_amount_g",   DEFAULT_FINAL_MICRO_AMOUNT_G)
    entry.setdefault("settling_delay_s",       DEFAULT_SETTLING_DELAY_S)
    entry["servo"]=_coerce_servo(entry.get("servo"))
    return entry

def load_station_config():
    ing={k:_apply_behavior_defaults(dict(v)) for k,v in _DEFAULTS.items()}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f: saved=json.load(f)
            for k,v in saved.items():
                try: sid=int(k)
                except Exception: continue
                if sid not in ing: continue
                for fld in _BEHAVIOR_FIELDS:
                    if fld in v: ing[sid][fld]=v[fld]
                # Defensive type coercion + validation
                try: ing[sid]["micro_threshold_g"]=float(ing[sid]["micro_threshold_g"])
                except Exception: ing[sid]["micro_threshold_g"]=DEFAULT_MICRO_THRESHOLD_G
                try: ing[sid]["accel_step"]=float(ing[sid]["accel_step"])
                except Exception: ing[sid]["accel_step"]=ACCEL_STEP
                try: ing[sid]["decel_factor"]=float(ing[sid]["decel_factor"])
                except Exception: ing[sid]["decel_factor"]=DEFAULT_DECEL_FACTOR
                ing[sid]["ml_model_enabled"]=bool(ing[sid].get("ml_model_enabled",DEFAULT_ML_ENABLED))
                if ing[sid].get("dispense_mode") not in ("auto","micro","normal"):
                    ing[sid]["dispense_mode"]=DEFAULT_DISPENSE_MODE
                ing[sid]["servo"]=_coerce_servo(ing[sid].get("servo"))
                # Per-station scale tolerance + fall delay (defaults if missing/invalid)
                try: ing[sid]["scale_tolerance_grams"]=float(ing[sid].get("scale_tolerance_grams",DEFAULT_SCALE_TOLERANCE_G))
                except Exception: ing[sid]["scale_tolerance_grams"]=DEFAULT_SCALE_TOLERANCE_G
                try: ing[sid]["fall_delay_seconds"]=float(ing[sid].get("fall_delay_seconds",DEFAULT_FALL_DELAY_S))
                except Exception: ing[sid]["fall_delay_seconds"]=DEFAULT_FALL_DELAY_S
                ing[sid]["scale_tolerance_grams"]=max(0.05,min(50.0,ing[sid]["scale_tolerance_grams"]))
                ing[sid]["fall_delay_seconds"]   =max(0.0, min(30.0,ing[sid]["fall_delay_seconds"]))
                try: ing[sid]["target_offset_g"]=float(ing[sid].get("target_offset_g",DEFAULT_TARGET_OFFSET_G))
                except Exception: ing[sid]["target_offset_g"]=DEFAULT_TARGET_OFFSET_G
                ing[sid]["target_offset_g"]=max(-TARGET_OFFSET_MAX_G,min(TARGET_OFFSET_MAX_G,ing[sid]["target_offset_g"]))
                # Per-station servo safety envelope (clamped to absolute hardware limits)
                try: ing[sid]["safe_angle_min"]=float(ing[sid].get("safe_angle_min",DEFAULT_SAFE_ANGLE_MIN))
                except Exception: ing[sid]["safe_angle_min"]=DEFAULT_SAFE_ANGLE_MIN
                try: ing[sid]["safe_angle_max"]=float(ing[sid].get("safe_angle_max",DEFAULT_SAFE_ANGLE_MAX))
                except Exception: ing[sid]["safe_angle_max"]=DEFAULT_SAFE_ANGLE_MAX
                ing[sid]["safe_angle_min"]=max(SAFE_ANGLE_MIN_LIMIT,min(SAFE_ANGLE_MAX_LIMIT,ing[sid]["safe_angle_min"]))
                ing[sid]["safe_angle_max"]=max(SAFE_ANGLE_MIN_LIMIT,min(SAFE_ANGLE_MAX_LIMIT,ing[sid]["safe_angle_max"]))
                if ing[sid]["safe_angle_max"]<=ing[sid]["safe_angle_min"]:
                    # Defensive: if the saved range is invalid, fall back to defaults
                    ing[sid]["safe_angle_min"]=DEFAULT_SAFE_ANGLE_MIN
                    ing[sid]["safe_angle_max"]=DEFAULT_SAFE_ANGLE_MAX
                try: ing[sid]["servo_move_timeout_s"]=float(ing[sid].get("servo_move_timeout_s",SERVO_MOVE_TIMEOUT_S_DEFAULT))
                except Exception: ing[sid]["servo_move_timeout_s"]=SERVO_MOVE_TIMEOUT_S_DEFAULT
                ing[sid]["servo_move_timeout_s"]=max(1.0,min(60.0,ing[sid]["servo_move_timeout_s"]))
                # ── precision dispensing fields (clamped to safe ranges) ──
                def _flt(key,default,lo,hi):
                    try: ing[sid][key]=float(ing[sid].get(key,default))
                    except Exception: ing[sid][key]=default
                    ing[sid][key]=max(lo,min(hi,ing[sid][key]))
                def _intc(key,default,lo,hi):
                    try: ing[sid][key]=int(ing[sid].get(key,default))
                    except Exception: ing[sid][key]=default
                    ing[sid][key]=max(lo,min(hi,ing[sid][key]))
                _flt("coarse_margin_g",        DEFAULT_COARSE_MARGIN_G,  0.0, 100.0)
                _flt("fine_margin_g",          DEFAULT_FINE_MARGIN_G,    0.0,  20.0)
                _flt("inflight_compensation_g",DEFAULT_INFLIGHT_COMP_G,  0.0,  20.0)
                _flt("learned_compensation_g", DEFAULT_LEARNED_COMP_G,
                      -PRECISION_LEARNED_CLAMP_G, PRECISION_LEARNED_CLAMP_G)
                _flt("stable_window_g",        DEFAULT_STABLE_WINDOW_G,  0.005, 1.0)
                _intc("settle_ms",             DEFAULT_SETTLE_MS,        0,    5000)
                _intc("stable_sample_count",   DEFAULT_STABLE_SAMPLE_CNT,2,    20)
                _intc("pulse_ms_large",        DEFAULT_PULSE_MS_LARGE,   5,    500)
                _intc("pulse_ms_medium",       DEFAULT_PULSE_MS_MEDIUM,  5,    500)
                _intc("pulse_ms_small",        DEFAULT_PULSE_MS_SMALL,   5,    500)
                _intc("pulse_ms_tiny",         DEFAULT_PULSE_MS_TINY,    5,    500)
                _intc("max_topup_pulses",      DEFAULT_MAX_TOPUP_PULSES, 1,    50)
                _flt("auto_micro_tail_g",      DEFAULT_AUTO_MICRO_TAIL_G,0.0, AUTO_MICRO_TAIL_MAX_G)
                _flt("final_micro_amount_g",   DEFAULT_FINAL_MICRO_AMOUNT_G,0.0, FINAL_MICRO_AMOUNT_MAX_G)
                _flt("settling_delay_s",       DEFAULT_SETTLING_DELAY_S,    0.0, SETTLING_DELAY_MAX_S)
        except Exception: pass
    return ing

def save_station_config(ingredients):
    with open(CONFIG_FILE,"w") as f:
        out={}
        for k,v in ingredients.items():
            out[str(k)]={
                "label":v.get("label",""),
                "ingredient_name":v.get("ingredient_name",v.get("label","")),
                "dispense_mode":v.get("dispense_mode",DEFAULT_DISPENSE_MODE),
                "micro_threshold_g":float(v.get("micro_threshold_g",DEFAULT_MICRO_THRESHOLD_G)),
                "accel_step":float(v.get("accel_step",ACCEL_STEP)),
                "decel_factor":float(v.get("decel_factor",DEFAULT_DECEL_FACTOR)),
                "ml_model_enabled":bool(v.get("ml_model_enabled",DEFAULT_ML_ENABLED)),
                "servo":_coerce_servo(v.get("servo")),
                "scale_tolerance_grams":float(v.get("scale_tolerance_grams",DEFAULT_SCALE_TOLERANCE_G)),
                "fall_delay_seconds":   float(v.get("fall_delay_seconds",   DEFAULT_FALL_DELAY_S)),
                "target_offset_g":      float(v.get("target_offset_g",      DEFAULT_TARGET_OFFSET_G)),
                "safe_angle_min":       float(v.get("safe_angle_min",       DEFAULT_SAFE_ANGLE_MIN)),
                "safe_angle_max":       float(v.get("safe_angle_max",       DEFAULT_SAFE_ANGLE_MAX)),
                "servo_move_timeout_s": float(v.get("servo_move_timeout_s", SERVO_MOVE_TIMEOUT_S_DEFAULT)),
                # precision dispensing
                "coarse_margin_g":         float(v.get("coarse_margin_g",        DEFAULT_COARSE_MARGIN_G)),
                "fine_margin_g":           float(v.get("fine_margin_g",          DEFAULT_FINE_MARGIN_G)),
                "inflight_compensation_g": float(v.get("inflight_compensation_g",DEFAULT_INFLIGHT_COMP_G)),
                "learned_compensation_g":  float(v.get("learned_compensation_g", DEFAULT_LEARNED_COMP_G)),
                "settle_ms":               int(v.get("settle_ms",                DEFAULT_SETTLE_MS)),
                "stable_window_g":         float(v.get("stable_window_g",        DEFAULT_STABLE_WINDOW_G)),
                "stable_sample_count":     int(v.get("stable_sample_count",      DEFAULT_STABLE_SAMPLE_CNT)),
                "pulse_ms_large":          int(v.get("pulse_ms_large",           DEFAULT_PULSE_MS_LARGE)),
                "pulse_ms_medium":         int(v.get("pulse_ms_medium",          DEFAULT_PULSE_MS_MEDIUM)),
                "pulse_ms_small":          int(v.get("pulse_ms_small",           DEFAULT_PULSE_MS_SMALL)),
                "pulse_ms_tiny":           int(v.get("pulse_ms_tiny",            DEFAULT_PULSE_MS_TINY)),
                "max_topup_pulses":        int(v.get("max_topup_pulses",         DEFAULT_MAX_TOPUP_PULSES)),
                "auto_micro_tail_g":       float(v.get("auto_micro_tail_g",      DEFAULT_AUTO_MICRO_TAIL_G)),
                "final_micro_amount_g":    float(v.get("final_micro_amount_g",   DEFAULT_FINAL_MICRO_AMOUNT_G)),
                "settling_delay_s":        float(v.get("settling_delay_s",       DEFAULT_SETTLING_DELAY_S)),
            }
        json.dump(out,f,indent=2)

INGREDIENTS=load_station_config()

def _persist_learned_comp(sid:int, value:float):
    """Update INGREDIENTS[sid]['learned_compensation_g'] in-memory and on disk.
    Called by the fill loop after each fill so per-station overshoot bias is
    learned across runs. Clamped to ±PRECISION_LEARNED_CLAMP_G for safety."""
    if sid not in INGREDIENTS: return
    v=max(-PRECISION_LEARNED_CLAMP_G,min(PRECISION_LEARNED_CLAMP_G,float(value)))
    INGREDIENTS[sid]["learned_compensation_g"]=v
    try: save_station_config(INGREDIENTS)
    except Exception: pass

# ─── INGREDIENT CATALOGUE ─────────────────────────────────────────────────────
def load_catalogue()->list:
    """Master list of ingredient names available in the Station Mgr dropdown.
    Seeded from default labels on first run; deduped and sorted on save."""
    seed=sorted({v["label"] for v in _DEFAULTS.values()})
    if os.path.exists(CATALOGUE_FILE):
        try:
            with open(CATALOGUE_FILE) as f: data=json.load(f)
            items=data.get("ingredients",[]) if isinstance(data,dict) else list(data)
            cleaned=sorted({str(s).strip() for s in items if str(s).strip()})
            # Always merge defaults so a missing seed never hides factory ingredients
            return sorted(set(cleaned)|set(seed))
        except Exception: pass
    return seed

def save_catalogue(items:list):
    cleaned=sorted({str(s).strip() for s in items if str(s).strip()})
    with open(CATALOGUE_FILE,"w") as f:
        json.dump({"ingredients":cleaned},f,indent=2)
    return cleaned

def add_ingredient_to_catalogue(name:str)->list:
    cur=load_catalogue()
    if name and name.strip() and name.strip() not in cur:
        cur.append(name.strip())
        cur=save_catalogue(cur)
    return cur

INGREDIENT_CATALOGUE=load_catalogue()

# ─── RECIPE MANAGEMENT ────────────────────────────────────────────────────────
_BASE_RECIPES={
    # 5 base teas (single ingredient)
    "Base: Strathspey BOPF":      {1:100.0},
    "Base: Laxapana Peko":        {2:100.0},
    "Base: Moray BOP":            {3:100.0},
    "Base: Silver Tips":          {4:100.0},
    "Base: Golden Tips":          {5:100.0},
    # Blended recipes
    "Ceylon Spiced Breakfast":    {1:85.0,6:10.0,7:5.0},
    "Citrus Earl Grey Style":     {2:92.0,8:5.0,9:3.0},
    "Ginger Lemongrass":          {3:88.0,7:7.0,10:5.0},
    "Silver Tips Rose & Citrus":  {4:94.0,11:4.0,9:2.0},
    "Golden Tips Jasmine & Citrus":{5:94.0,12:4.0,8:2.0},
}

def load_recipes():
    if os.path.exists(RECIPES_FILE):
        try:
            with open(RECIPES_FILE) as f:
                raw=json.load(f)
            # JSON keys are strings — convert ingredient keys back to int
            return {name:{int(k):float(v) for k,v in lanes.items()}
                    for name,lanes in raw.items()}
        except Exception: pass
    return dict(_BASE_RECIPES)

def save_recipes(recipes):
    with open(RECIPES_FILE,"w") as f:
        # Convert int keys → str for JSON
        serialisable={name:{str(k):v for k,v in lanes.items()}
                      for name,lanes in recipes.items()}
        json.dump(serialisable,f,indent=2)

RECIPES=load_recipes()

# ─── INVENTORY ────────────────────────────────────────────────────────────────
def _default_inventory():
    return {sid:{"capacity":DEFAULT_CAPACITY,"stock":DEFAULT_CAPACITY}
            for sid in _DEFAULTS}

def load_inventory():
    inv=_default_inventory()
    if os.path.exists(INVENTORY_FILE):
        try:
            with open(INVENTORY_FILE) as f: saved=json.load(f)
            for k,v in saved.items():
                sid=int(k)
                if sid in inv:
                    inv[sid]["capacity"]=float(v.get("capacity",DEFAULT_CAPACITY))
                    inv[sid]["stock"]=float(v.get("stock",DEFAULT_CAPACITY))
        except Exception: pass
    return inv

def save_inventory():
    with open(INVENTORY_FILE,"w") as f:
        serialisable={str(k):{"capacity":v["capacity"],"stock":round(v["stock"],3)}
                      for k,v in inventory.items()}
        json.dump(serialisable,f,indent=2)

inventory=load_inventory()

def refill_station(sid:int, weight_added:float):
    """Add weight to a container and log the refill."""
    if sid not in inventory: return
    inventory[sid]["stock"]=min(inventory[sid]["capacity"],
                                inventory[sid]["stock"]+weight_added)
    save_inventory()
    # Append to refill log
    ts=datetime.now().isoformat(timespec="seconds")
    needs_hdr=not os.path.exists(REFILL_LOG_FILE)
    with open(REFILL_LOG_FILE,"a",newline="") as f:
        w=csv.writer(f)
        if needs_hdr: w.writerow(["Timestamp","Station","Ingredient","Added_g","NewStock_g"])
        label=INGREDIENTS.get(sid,{}).get("label",f"S{sid}")
        w.writerow([ts,f"S{sid:02d}",label,f"{weight_added:.2f}",
                    f"{inventory[sid]['stock']:.2f}"])
    log_msg(f"S{sid:02d} REFILL +{weight_added:.0f}g → {inventory[sid]['stock']:.0f}g total","ok")

def deduct_stock(sid:int, actual_g:float):
    """Subtract actual dispensed weight from container stock."""
    if sid not in inventory: return
    inventory[sid]["stock"]=max(0.0, inventory[sid]["stock"]-actual_g)
    save_inventory()

def low_stock_stations()->list:
    """Returns list of station IDs whose stock is at or below LOW_STOCK_THRESH."""
    return [sid for sid,v in inventory.items() if v["stock"]<=LOW_STOCK_THRESH]

def check_stock(weights:dict)->list:
    """
    Validate that every station in weights has enough stock.
    Returns list of plain-English error strings (empty = all OK).
    Called BEFORE dispatch starts so the batch is blocked atomically.
    """
    errors=[]
    for sid,needed in weights.items():
        avail=inventory.get(sid,{}).get("stock",0.0)
        label=INGREDIENTS.get(sid,{}).get("label",f"S{sid}")
        if avail<needed:
            errors.append(
                f"Insufficient stock in Station {sid} ({label}): "
                f"need {needed:.0f}g, only {avail:.0f}g available."
            )
    return errors

# ─── ADAPTIVE TOLERANCE ────────────────────────────────────────────────────────
def tolerance(target:float)->float:
    """1% of target or 0.2g minimum."""
    return max(0.2, target*0.01)

# ─── LOGGING ──────────────────────────────────────────────────────────────────
_log_callbacks=[]; _next_oid=[1001]

def _load_last_oid():
    if not os.path.exists(LOG_FILE): return
    try:
        with open(LOG_FILE) as f: rows=list(csv.reader(f))
        for row in reversed(rows[1:]):
            if row:
                try: _next_oid[0]=int(row[0])+1; return
                except Exception: pass
    except Exception: pass

def next_oid():
    v=_next_oid[0]; _next_oid[0]+=1; return v

PRODUCTION_LOG_HEADER=["OrderID","Timestamp","Recipe","Station","Tea",
                       "Target_g","Actual_g","Delta_g","Tolerance_g","Status"]

def ensure_log_file():
    """Ensure production_log.csv exists with the correct header row.
    Called at import time AND each time log_production() runs, so the file
    is always present even before the first order completes — fixes the
    'Production Log is empty / not visible' complaint where the file was
    only created lazily on first write."""
    try:
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE,"w",newline="") as f:
                csv.writer(f).writerow(PRODUCTION_LOG_HEADER)
    except Exception as e:
        # Don't crash the app on a disk error; surface to the log strip.
        try: log_msg(f"production_log init failed: {e}","error")
        except Exception: pass

def log_production(oid,recipe,sid,label,tgt,actual,status_override=None):
    """Append one row per ingredient. `status_override` lets callers tag
    rows from aborted/failed runs (e.g. 'CANCELLED' / 'EJECTED' / 'ERROR')
    so the CSV still reflects what happened — previously, aborted recipes
    wrote NO rows, leaving the operator with a blank log."""
    delta=actual-tgt; tol=tolerance(tgt)
    ts=datetime.now().isoformat(timespec="seconds")
    ensure_log_file()
    if status_override:
        status=status_override
    else:
        status="OK" if abs(delta)<=tol else "WARN"
    try:
        with open(LOG_FILE,"a",newline="") as f:
            w=csv.writer(f)
            w.writerow([oid,ts,recipe,sid,label,f"{tgt:.2f}",f"{actual:.2f}",
                        f"{delta:+.2f}",f"±{tol:.2f}",status])
            f.flush()
            try: os.fsync(f.fileno())   # force flush to disk on RPi SD card
            except Exception: pass
    except Exception as e:
        log_msg(f"production_log write failed: {e}","error")

def log_msg(msg,level="ok"):
    ts=datetime.now().strftime("%H:%M:%S")
    for cb in _log_callbacks:
        try: cb((ts,msg,level))
        except Exception: pass

# ─── VIBRATION FREEZE ─────────────────────────────────────────────────────────
_vibe_until=0.0

def set_vibe_freeze(on:bool):
    global _vibe_until
    _vibe_until=time.monotonic()+VIBE_FREEZE_S if on else 0.0

def vibe_frozen(board_id:int)->bool:
    return time.monotonic()<_vibe_until and board_id in (3,4)

# ─── PREDICTIVE MAINTENANCE ───────────────────────────────────────────────────
_run_s={sid:0.0 for sid in range(1,14)}
_run_start:dict[int,float]={}

def motor_on(sid:int):
    _run_start[sid]=time.monotonic()

def motor_off(sid:int):
    if sid in _run_start:
        _run_s[sid]+=time.monotonic()-_run_start.pop(sid)

def motor_hours(sid:int)->float:
    return _run_s[sid]/3600.0

def maint_alert(sid:int)->bool:
    return motor_hours(sid)>=MAINT_WARN_HOURS

# ─── SERIAL BOARD ─────────────────────────────────────────────────────────────
class Board:
    def __init__(self,bid,port):
        self.bid=bid; self.port=port
        self.ser=None; self.lock=threading.Lock()
        self.connected=False; self.latency_ms=0.0
        self.weights={1:0.0,2:0.0,3:0.0,4:0.0}
        self._wd=0.0; self._run=True
        self._last_ok=time.monotonic()
        self._err={1:0,2:0,3:0,4:0}
        self._soft_stopped=False

    def connect(self):
        try:
            self.ser=serial.Serial(self.port,BAUD_RATE,timeout=0.15)
            self.ser.reset_input_buffer(); time.sleep(1.5)
            self.write(b"T:ALL\n"); time.sleep(0.8)
            # Stagger servo home / detach so 4 servos don't energise into the
            # shared 5 V rail simultaneously. Each goes to neutral (0° in the
            # new signed-angle mapping = ~1500 µs = no stress) and then is
            # detached (PWM off, gear train holds passively, zero current).
            for ch in range(1,5):
                self.write(f"S:{ch}:0\n".encode())
                time.sleep(0.25)
                self.write(f"S:{ch}:{SERVO_DETACH_SENTINEL}\n".encode())
                time.sleep(0.05)
            self.connected=True; self._last_ok=time.monotonic()
            log_msg(f"Board {self.bid} ONLINE ({self.port})","ok")
        except Exception as e:
            self.connected=False
            log_msg(f"Board {self.bid} OFFLINE: {e}","warn")

    def reconnect(self):
        try:
            if self.ser: self.ser.close()
        except Exception: pass
        self.connected=False
        log_msg(f"Refreshing B{self.bid} ({self.port}) ...","info")
        threading.Thread(target=self.connect,daemon=True).start()

    def write(self,data):
        if not self.ser: return
        try:
            with self.lock: self.ser.write(data)
        except Exception as e:
            log_msg(f"B{self.bid} write err: {e}","error")
            self.connected=False

    def _soft_stop(self):
        if self._soft_stopped: return
        self._soft_stopped=True
        log_msg(f"SOFT-STOP: Board {self.bid} no response >{HARD_WD_S}s","error")
        for b in boards.values(): b.write(b"X\n")

    def poll(self):
        while self._run:
            if self.connected and self.ser and not vibe_frozen(self.bid):
                try:
                    t0=time.monotonic()
                    with self.lock:
                        self.ser.reset_input_buffer()
                        self.ser.write(b"W:ALL\n")
                    line=""
                    try: line=self.ser.readline().decode(errors="replace").strip()
                    except Exception: pass
                    self.latency_ms=(time.monotonic()-t0)*1000
                    if line.startswith("WA:"):
                        parts=line.split(":")[1:]
                        for i in range(min(4,len(parts))):
                            p=parts[i]
                            if p=="ERR":
                                self._err[i+1]+=1
                                if self._err[i+1]>=ERR_RETRY:
                                    self.write(f"T:{i+1}\n".encode())
                                    log_msg(f"B{self.bid} Ch{i+1}: ERR→auto-tare","warn")
                                    self._err[i+1]=0
                            elif p not in ("OFF","BUSY",""):
                                self._err[i+1]=0
                                try: self.weights[i+1]=float(p)
                                except ValueError: pass
                        self._last_ok=time.monotonic()
                        self._soft_stopped=False
                    if time.monotonic()-self._last_ok>HARD_WD_S:
                        self._soft_stop()
                    elif time.monotonic()-self._wd>=WATCHDOG_INTERVAL_S:
                        self.write(b"W:ALL\n"); self._wd=time.monotonic()
                except Exception: pass
            time.sleep(POLL_INTERVAL_S)

    def stop(self): self._run=False

boards:dict[int,Board]={}
app_ref=None
conveyor_running=False

def init_boards():
    _load_last_oid()
    # Ensure production_log.csv exists with its header before the first
    # order so the Production Log tab can immediately read/display it
    # rather than showing nothing until a fill completes.
    ensure_log_file()
    for bid,port in BOARD_PORTS.items():
        b=Board(bid,port); boards[bid]=b
        threading.Thread(target=b.connect,daemon=True).start()
        threading.Thread(target=b.poll,   daemon=True).start()

def bwrite(bid,data):
    if bid in boards: boards[bid].write(data)

def set_motor(bid,ch,duty): bwrite(bid,f"D:{ch}:{duty}\n".encode())
def set_servo(bid,ch,angle): bwrite(bid,f"S:{ch}:{angle}\n".encode())

def estop_all():
    for b in boards.values(): b.write(b"X\n")
    log_msg("■ EMERGENCY STOP — all boards halted","error")

# ─── ML VOLTAGE MODEL ─────────────────────────────────────────────────────────
def solve_v(flow:float)->float:
    try:
        a,b,c=ML_A,ML_B,ML_C-flow
        d=b**2-4*a*c
        if d<0: return VOLT_MAX
        v1=(-b+math.sqrt(d))/(2*a); v2=(-b-math.sqrt(d))/(2*a)
        vs=[v for v in [v1,v2] if v>0]
        return max(VOLT_MIN,min(VOLT_MAX,min(vs))) if vs else VOLT_MIN
    except Exception: return VOLT_MIN

# ─── STATION ──────────────────────────────────────────────────────────────────
class Station:
    def __init__(self,sid):
        self.id=sid; info=INGREDIENTS[sid]
        self.board=info["board"]; self.ch=info["ch"]
        self.label=info.get("label",f"S{sid}")
        # Median buffer extended to MED_BUF_LEN for stronger spike rejection.
        self.ema=0.0; self.mbuf=deque(maxlen=MED_BUF_LEN); self.last_w=0.0
        # Spike-filter state: last accepted raw value + consecutive-spike counter
        self._last_raw_kept=0.0
        self._spike_streak=0
        self._last_raw_t=0.0
        self.running=False; self.ui_w=0.0; self.ui_v=0.0
        self.status="IDLE"; self.target=0.0; self.actual=0.0
        self.lbl_w=None; self.lbl_s=None; self.prog=None
        self.e_tgt=None; self.b_start=None; self.b_stop=None; self.b_drop=None
        self.card_frame=None
        # ── Servo health / safety state ──────────────────────────────────────
        # _servo_busy_lock prevents overlapping drop/eject sequences on the
        # same station (rapid-click protection). The other counters drive
        # rate-limiting, direction-change pause, and overheat cooldown.
        self._servo_busy_lock=threading.Lock()
        self._servo_last_cmd_t=0.0
        self._servo_last_angle=None
        self._servo_last_dir=0           # -1, 0, or +1
        self._servo_active_s=0.0         # cumulative active s in current window
        self._servo_window_t0=time.monotonic()
        self._servo_cooldown_until=0.0
        self.apply_behavior(info)

    def apply_behavior(self, info=None):
        """Refresh label + per-station dispense behavior from INGREDIENTS.
        Safe to call mid-session after the operator saves the Behavior tab."""
        if info is None: info=INGREDIENTS.get(self.id,{})
        self.label=info.get("label",getattr(self,"label",f"S{self.id}"))
        self.ingredient_name=info.get("ingredient_name",self.label)
        self.dispense_mode=info.get("dispense_mode",DEFAULT_DISPENSE_MODE)
        try: self.micro_threshold_g=float(info.get("micro_threshold_g",DEFAULT_MICRO_THRESHOLD_G))
        except Exception: self.micro_threshold_g=DEFAULT_MICRO_THRESHOLD_G
        try: self.accel_step=float(info.get("accel_step",ACCEL_STEP))
        except Exception: self.accel_step=ACCEL_STEP
        try: self.decel_factor=float(info.get("decel_factor",DEFAULT_DECEL_FACTOR))
        except Exception: self.decel_factor=DEFAULT_DECEL_FACTOR
        self.ml_model_enabled=bool(info.get("ml_model_enabled",DEFAULT_ML_ENABLED))
        self.servo=_coerce_servo(info.get("servo"))
        try: self.scale_tolerance_grams=float(info.get("scale_tolerance_grams",DEFAULT_SCALE_TOLERANCE_G))
        except Exception: self.scale_tolerance_grams=DEFAULT_SCALE_TOLERANCE_G
        try: self.fall_delay_seconds=float(info.get("fall_delay_seconds",DEFAULT_FALL_DELAY_S))
        except Exception: self.fall_delay_seconds=DEFAULT_FALL_DELAY_S
        try: self.target_offset_g=float(info.get("target_offset_g",DEFAULT_TARGET_OFFSET_G))
        except Exception: self.target_offset_g=DEFAULT_TARGET_OFFSET_G
        # Safety clamp — never let an offset drag the auger more than ±5 g off-target
        self.target_offset_g=max(-TARGET_OFFSET_MAX_G,min(TARGET_OFFSET_MAX_G,self.target_offset_g))
        # Per-station servo safety envelope + per-movement timeout
        try: self.safe_angle_min=float(info.get("safe_angle_min",DEFAULT_SAFE_ANGLE_MIN))
        except Exception: self.safe_angle_min=DEFAULT_SAFE_ANGLE_MIN
        try: self.safe_angle_max=float(info.get("safe_angle_max",DEFAULT_SAFE_ANGLE_MAX))
        except Exception: self.safe_angle_max=DEFAULT_SAFE_ANGLE_MAX
        self.safe_angle_min=max(SAFE_ANGLE_MIN_LIMIT,min(SAFE_ANGLE_MAX_LIMIT,self.safe_angle_min))
        self.safe_angle_max=max(SAFE_ANGLE_MIN_LIMIT,min(SAFE_ANGLE_MAX_LIMIT,self.safe_angle_max))
        if self.safe_angle_max<=self.safe_angle_min:
            self.safe_angle_min=DEFAULT_SAFE_ANGLE_MIN
            self.safe_angle_max=DEFAULT_SAFE_ANGLE_MAX
        try: self.servo_move_timeout_s=float(info.get("servo_move_timeout_s",SERVO_MOVE_TIMEOUT_S_DEFAULT))
        except Exception: self.servo_move_timeout_s=SERVO_MOVE_TIMEOUT_S_DEFAULT
        self.servo_move_timeout_s=max(1.0,min(60.0,self.servo_move_timeout_s))
        # ── Precision dispensing tuning (overshoot reduction) ────────────────
        def _gf(key,default):
            try: return float(info.get(key,default))
            except Exception: return default
        def _gi(key,default):
            try: return int(info.get(key,default))
            except Exception: return default
        self.coarse_margin_g        = max(0.0, min(100.0,_gf("coarse_margin_g",        DEFAULT_COARSE_MARGIN_G)))
        self.fine_margin_g          = max(0.0, min( 20.0,_gf("fine_margin_g",          DEFAULT_FINE_MARGIN_G)))
        self.inflight_compensation_g= max(0.0, min( 20.0,_gf("inflight_compensation_g",DEFAULT_INFLIGHT_COMP_G)))
        self.learned_compensation_g = max(-PRECISION_LEARNED_CLAMP_G,
                                           min(PRECISION_LEARNED_CLAMP_G,
                                               _gf("learned_compensation_g", DEFAULT_LEARNED_COMP_G)))
        self.settle_ms              = max(0,    min(5000,_gi("settle_ms",              DEFAULT_SETTLE_MS)))
        self.stable_window_g        = max(0.005,min( 1.0,_gf("stable_window_g",        DEFAULT_STABLE_WINDOW_G)))
        self.stable_sample_count    = max(2,    min(  20,_gi("stable_sample_count",    DEFAULT_STABLE_SAMPLE_CNT)))
        self.pulse_ms_large         = max(5,    min( 500,_gi("pulse_ms_large",         DEFAULT_PULSE_MS_LARGE)))
        self.pulse_ms_medium        = max(5,    min( 500,_gi("pulse_ms_medium",        DEFAULT_PULSE_MS_MEDIUM)))
        self.pulse_ms_small         = max(5,    min( 500,_gi("pulse_ms_small",         DEFAULT_PULSE_MS_SMALL)))
        self.pulse_ms_tiny          = max(5,    min( 500,_gi("pulse_ms_tiny",          DEFAULT_PULSE_MS_TINY)))
        self.max_topup_pulses       = max(1,    min(  50,_gi("max_topup_pulses",       DEFAULT_MAX_TOPUP_PULSES)))
        # (Legacy field — kept so older saved configs still load. New
        # fills use final_micro_amount_g + settling_delay_s below.)
        self.auto_micro_tail_g      = max(0.0, min(AUTO_MICRO_TAIL_MAX_G,
                                                    _gf("auto_micro_tail_g", DEFAULT_AUTO_MICRO_TAIL_G)))
        # PRE-STOP + SETTLE + FINAL-MICRO stage (operator-spec, applied to
        # EVERY fill). Behavior tab fields:
        #   final_micro_amount_g : bulk stops this many grams BEFORE target
        #   settling_delay_s     : motor-off wait between bulk and final-micro
        self.final_micro_amount_g   = max(0.0, min(FINAL_MICRO_AMOUNT_MAX_G,
                                                    _gf("final_micro_amount_g", DEFAULT_FINAL_MICRO_AMOUNT_G)))
        self.settling_delay_s       = max(0.0, min(SETTLING_DELAY_MAX_S,
                                                    _gf("settling_delay_s",     DEFAULT_SETTLING_DELAY_S)))

    @property
    def raw(self):
        return boards[self.board].weights.get(self.ch,0.0) if self.board in boards else 0.0

    def smooth(self):
        """Filtered weight read.
        Pipeline (FIX for HX711 spikes/random jumps with no real weight):
          1. read raw from board
          2. debounce: bail-out reuse of last EMA if called faster than
             SCALE_DEBOUNCE_S apart (caller sees a stable value, no extra cost)
          3. spike rejection: drop samples that jumped >MAX_JUMP_G from the
             last kept raw, unless we've seen SPIKE_TOLERATE_N in a row (real
             step change). Spikes never enter the median buffer.
          4. median filter over MED_BUF_LEN samples
          5. EMA filter at EMA_ALPHA
        Returns 0.0 when |EMA|<ZERO_RANGE (the existing dead-band).
        """
        now=time.monotonic()
        # (2) Debounce: reuse last EMA when called too frequently
        if (now-self._last_raw_t)<SCALE_DEBOUNCE_S and self.mbuf:
            return 0.0 if abs(self.ema)<ZERO_RANGE else self.ema
        r=self.raw
        # (3) Spike rejection — only after the buffer has primed; first
        # MED_BUF_LEN samples are always accepted so a freshly-tared scale
        # doesn't get its initial reading rejected.
        if len(self.mbuf)>=MED_BUF_LEN:
            if abs(r-self._last_raw_kept)>MAX_JUMP_G:
                self._spike_streak+=1
                if self._spike_streak<SPIKE_TOLERATE_N:
                    # Drop this sample but keep the EMA flowing.
                    self._last_raw_t=now
                    return 0.0 if abs(self.ema)<ZERO_RANGE else self.ema
                # Persistent jump → accept as real (e.g., operator added weight)
        self._spike_streak=0
        self._last_raw_kept=r
        self._last_raw_t=now
        # (4) Median
        self.mbuf.append(r)
        med=sorted(self.mbuf)[len(self.mbuf)//2]
        # (5) EMA
        self.ema=med if self.ema==0 else self.ema*(1-EMA_ALPHA)+med*EMA_ALPHA
        return 0.0 if abs(self.ema)<ZERO_RANGE else self.ema

    def _motor(self,v): set_motor(self.board,self.ch,int(max(0,min(12,v))/SUPPLY_VOLTAGE*65535))
    def _stop(self):    set_motor(self.board,self.ch,0)

    def _servo(self,a):
        """Rate-limited, range-clamped servo command.

        - `a == SERVO_DETACH_SENTINEL` is the DETACH sentinel and is ALWAYS
          allowed (it relieves the servo from holding load — never harmful,
          must never be blocked). The sentinel was changed from -1 to -9999
          because -1 collided with eject sweeps that step through it on the
          way to -70°, causing the servo to be detached mid-sweep then
          re-attached at a past-stop position (a known SG90/MG90S burn mode).
        - All other angles are clamped to the per-station safe envelope
          [safe_angle_min, safe_angle_max] (which is itself bounded by the
          absolute hardware limits SAFE_ANGLE_MIN_LIMIT/MAX_LIMIT).
        - Honors the per-station cooldown window so an overheating/stalling
          servo gets forced rest.
        - Inserts SERVO_DIRECTION_CHANGE_PAUSE_S when the motion reverses, so
          the servo isn't slammed into a direction change.
        - Rate-limited to SERVO_MIN_CMD_INTERVAL_S between consecutive writes
          to prevent flooding the serial bus and the servo controller."""
        # Detach: bypass all guards
        if a==SERVO_DETACH_SENTINEL:
            set_servo(self.board,self.ch,SERVO_DETACH_SENTINEL)
            self._servo_last_angle=None
            self._servo_last_dir=0
            self._servo_last_cmd_t=time.monotonic()
            return
        now=time.monotonic()
        # Forced cooldown gate (overheat/stall protection)
        if now<self._servo_cooldown_until:
            time.sleep(min(SERVO_COOLDOWN_DURATION_S,self._servo_cooldown_until-now))
            now=time.monotonic()
        # Clamp to per-station safe envelope (already bounded to hw limits at config-load)
        lo=getattr(self,"safe_angle_min",DEFAULT_SAFE_ANGLE_MIN)
        hi=getattr(self,"safe_angle_max",DEFAULT_SAFE_ANGLE_MAX)
        if hi<=lo: lo,hi=DEFAULT_SAFE_ANGLE_MIN,DEFAULT_SAFE_ANGLE_MAX
        try: a=int(round(max(lo,min(hi,float(a)))))
        except Exception: return
        # Rate limit
        dt=now-self._servo_last_cmd_t
        if dt<SERVO_MIN_CMD_INTERVAL_S:
            time.sleep(SERVO_MIN_CMD_INTERVAL_S-dt)
        # Direction-change pause: when sign of motion flips, insert a brief rest
        if self._servo_last_angle is not None:
            new_dir=1 if a>self._servo_last_angle else (-1 if a<self._servo_last_angle else 0)
            if (new_dir!=0 and self._servo_last_dir!=0
                and new_dir!=self._servo_last_dir):
                time.sleep(SERVO_DIRECTION_CHANGE_PAUSE_S)
            if new_dir!=0:
                self._servo_last_dir=new_dir
        # Send the actual command
        set_servo(self.board,self.ch,a)
        now=time.monotonic()
        self._servo_last_angle=a
        self._servo_last_cmd_t=now
        # Cooldown accounting (each write counts as a small active interval).
        # If the cumulative active time exceeds the threshold within the window,
        # mandate a SERVO_COOLDOWN_DURATION_S rest. Window resets on each
        # cooldown trigger so a continuously hammered servo eventually backs off.
        if (now-self._servo_window_t0)>SERVO_COOLDOWN_WINDOW_S:
            self._servo_window_t0=now
            self._servo_active_s=0.0
        self._servo_active_s+=SERVO_MIN_CMD_INTERVAL_S
        if self._servo_active_s>=SERVO_COOLDOWN_RUN_S:
            self._servo_cooldown_until=now+SERVO_COOLDOWN_DURATION_S
            self._servo_active_s=0.0
            self._servo_window_t0=now+SERVO_COOLDOWN_DURATION_S
            log_msg(f"S{self.id} servo cooldown {SERVO_COOLDOWN_DURATION_S:.1f}s "
                    f"— overheat/stall protection","warn")

    def _sweep(self,s,e,dt=0.015):
        """Smoothly sweep from angle `s` to `e` in 1° steps. Uses _servo
        internally so all rate-limit / cooldown / direction-change /
        clamping guards apply."""
        try: s=int(round(float(s))); e=int(round(float(e)))
        except Exception: return
        step=1 if s<=e else -1
        for a in range(s,e+step,step):
            self._servo(a); time.sleep(max(0.0,float(dt)))

    def _servo_release(self):
        """Settle, then DETACH the servo so it's not held under load.
        Called at the end of every drop/eject sequence (in `finally`) so
        even on an exception the servo never sits driving against the
        mechanical stop."""
        time.sleep(SERVO_DETACH_SETTLE_S)
        self._servo(SERVO_DETACH_SENTINEL)

    def tare(self):
        self.status="TARING"
        bwrite(self.board,f"T:{self.ch}\n".encode())
        self.mbuf.clear(); self.ema=0.0; time.sleep(0.4)
        self.status="IDLE"

    def start_fill(self,target=None):
        if target is None:
            try: target=float(self.e_tgt.get())
            except Exception: log_msg(f"S{self.id}: invalid target","error"); return
        if target<=0: return
        self.target=target; self.running=True; self.status="PLANNING"
        self._btns("filling"); motor_on(self.id)
        log_msg(f"S{self.id} fill {target:.1f}g [{self.label}]","ok")
        threading.Thread(target=self._fill_t,args=(target,),daemon=True).start()

    # ── Strict no-undershoot dispensing target ────────────────────────────────
    # The fill-loop guarantees the final weight is in [target, target + UPPER_TOL_G].
    # Negative deviation (undershoot) is corrected by the top-up loop below;
    # positive deviation (overshoot) is bounded by careful predictive cut +
    # one-pulse-at-a-time top-ups. UPPER_TOL_G is the only tolerance the loop
    # itself enforces; the orchestrator's WEIGHT_VALIDATION uses the per-station
    # scale_tolerance_grams and accepts ONLY non-negative delta.
    UPPER_TOL_G=0.05

    def _fill_t(self,tgt):
        """Precision two-phase fill: coarse → fine → settle → micro top-up.
        Bounds final error to ~0.1 g by:
          - splitting the fill: coarse phase at cruise voltage to
            (target − coarse_margin_g), fine phase at reduced voltage to
            (target − fine_margin_g − predicted_inflight);
          - measuring live flow rate during the fill and using
            predicted_inflight = flow * SYSTEM_DELAY_S
                               + inflight_compensation_g
                               + learned_compensation_g  (per-station, learned);
          - cutting the motor early, settling for settle_ms, then reading
            stable_sample_count consecutive samples within stable_window_g;
          - micro top-up: pulse → wait → measure with pulse duration adapted
            to the remaining gap (large/medium/small/tiny);
          - learning per-station residual overshoot via EMA and persisting it
            to station_config.json so subsequent fills cut even earlier.
        ML voltage model and per-station target_offset_g/fall_delay_seconds
        are preserved. PULSE mode (small targets) keeps predictive pulsing
        but uses adaptive pulse sizes."""
        UPPER_TOL_G=Station.UPPER_TOL_G
        start_t=time.monotonic()
        last_time=start_t; last_w=self.actual
        first_data_seen=False

        # Mode selection (unchanged)
        mode=self.dispense_mode
        thr=self.micro_threshold_g
        use_pulse=(mode=="micro") or (mode=="auto" and tgt<=thr)

        # Per-station tuning
        offset    =getattr(self,"target_offset_g",DEFAULT_TARGET_OFFSET_G)
        fall_s    =max(0.0,float(getattr(self,"fall_delay_seconds",DEFAULT_FALL_DELAY_S)))
        coarse_m  =max(0.0,float(getattr(self,"coarse_margin_g",        DEFAULT_COARSE_MARGIN_G)))
        fine_m    =max(0.0,float(getattr(self,"fine_margin_g",          DEFAULT_FINE_MARGIN_G)))
        inflight_c=max(0.0,float(getattr(self,"inflight_compensation_g",DEFAULT_INFLIGHT_COMP_G)))
        learned_c =max(-PRECISION_LEARNED_CLAMP_G,
                       min(PRECISION_LEARNED_CLAMP_G,
                           float(getattr(self,"learned_compensation_g", DEFAULT_LEARNED_COMP_G))))
        settle_s  =max(0.0,int(getattr(self,"settle_ms",DEFAULT_SETTLE_MS))/1000.0)
        stable_win=max(0.005,float(getattr(self,"stable_window_g",DEFAULT_STABLE_WINDOW_G)))
        stable_cnt=max(2,int(getattr(self,"stable_sample_count",DEFAULT_STABLE_SAMPLE_CNT)))
        pL=max(0.005,int(getattr(self,"pulse_ms_large", DEFAULT_PULSE_MS_LARGE))/1000.0)
        pM=max(0.005,int(getattr(self,"pulse_ms_medium",DEFAULT_PULSE_MS_MEDIUM))/1000.0)
        pS=max(0.005,int(getattr(self,"pulse_ms_small", DEFAULT_PULSE_MS_SMALL))/1000.0)
        pT=max(0.005,int(getattr(self,"pulse_ms_tiny",  DEFAULT_PULSE_MS_TINY))/1000.0)
        topup_max=max(1,int(getattr(self,"max_topup_pulses",DEFAULT_MAX_TOPUP_PULSES)))
        # PRE-STOP + SETTLE + FINAL-MICRO stage parameters (read live from
        # the per-station Behavior tab every fill — no restart required).
        final_micro=max(0.0,min(FINAL_MICRO_AMOUNT_MAX_G,
                                float(getattr(self,"final_micro_amount_g",DEFAULT_FINAL_MICRO_AMOUNT_G))))
        settle_delay=max(0.0,min(SETTLING_DELAY_MAX_S,
                                 float(getattr(self,"settling_delay_s",   DEFAULT_SETTLING_DELAY_S))))
        # FIX (target_offset_g semantics — operator-requested "stop early"):
        # POSITIVE offset now reduces the effective target. Example: tgt=5 g,
        # offset=0.5 → eff_tgt=4.5 g, so the fill stops at 4.5 g instead of
        # 5 g. The same eff_tgt is also used by the top-up and rescue
        # stop-checks below, so the whole fill loop aims for eff_tgt.
        eff_tgt=tgt-offset
        # Coarse must leave room for fine
        if coarse_m<fine_m+0.5: coarse_m=fine_m+0.5

        # ── ACTIVE BEHAVIOR LOG ───────────────────────────────────────────────
        # Single line printed at the start of every fill that prints EVERY
        # Behavior-tab value being used for this dispense. Lets the operator
        # confirm (from the system-log strip) that values just saved on the
        # Behavior tab are actually live without restarting the app. If you
        # tune a value on the Behavior tab and DON'T see it change here on
        # the very next dispense, the save → load → apply chain is broken.
        _tail_view=max(0.0,min(AUTO_MICRO_TAIL_MAX_G,
                               float(getattr(self,"auto_micro_tail_g",DEFAULT_AUTO_MICRO_TAIL_G))))
        # Pre-stop boundary (universal). Bulk dispense stops at this weight;
        # final-micro takes over after the settle wait. Clamped ≥0 so a
        # final_micro larger than tgt still produces a valid (degenerate)
        # bulk phase that immediately yields to the settle stage.
        bulk_stop=max(0.0, tgt - final_micro)
        log_msg(
            f"S{self.id} ACTIVE BEHAVIOR  "
            f"tgt={tgt:.2f}g  "
            f"mode={mode}  "
            f"micro_thr={thr:.2f}g  "
            f"tgt_offset={offset:+.2f}g → stop_at={eff_tgt:.2f}g  "
            f"final_micro={final_micro:.2f}g → bulk_stop={bulk_stop:.2f}g  "
            f"settle_delay={settle_delay:.2f}s  "
            f"tol=±{getattr(self,'scale_tolerance_grams',DEFAULT_SCALE_TOLERANCE_G):.2f}g  "
            f"fall={fall_s:.2f}s  "
            f"fineM={fine_m:.2f}g  inflight={inflight_c:.2f}g  learn={learned_c:+.2f}g  "
            f"pulses[L/M/S/T]={int(pL*1000)}/{int(pM*1000)}/{int(pS*1000)}/{int(pT*1000)}ms  "
            f"topup_max={topup_max}  ml={'on' if self.ml_model_enabled else 'off'}",
            "info")

        def _pulse_dur(gap):
            if gap>2.0: return pL
            if gap>0.8: return pM
            if gap>0.3: return pS
            return pT

        flow_buf=deque(maxlen=64)   # (t,w) samples for live flow
        def _push_flow(w):
            flow_buf.append((time.monotonic(),w))
        def _live_flow():
            if len(flow_buf)<2: return 0.0
            t1,w1=flow_buf[-1]; t0,w0=flow_buf[0]
            for t,w in flow_buf:
                if t1-t<=FLOW_RATE_WINDOW_S:
                    t0,w0=t,w; break
            dt=t1-t0
            return max(0.0,(w1-w0)/dt) if dt>0.05 else 0.0

        def _stable_read(timeout_s):
            """Wait settle_s then accept the weight once stable_cnt consecutive
            smooth() reads fall within stable_win. Returns the last reading."""
            time.sleep(settle_s)
            last=self.smooth(); count=1
            deadline=time.monotonic()+timeout_s
            while time.monotonic()<deadline and count<stable_cnt:
                time.sleep(0.04)
                cur=self.smooth()
                if abs(cur-last)<=stable_win: count+=1
                else: count=1
                last=cur
            self.ui_w=last
            return last

        def _no_data_check(elapsed_total):
            if elapsed_total>=DEFAULT_NO_DATA_TIMEOUT_S and not first_data_seen:
                log_msg(f"S{self.id} ERR: NO DATA from scale after {elapsed_total:.1f}s","error")
                self.status="ERROR"; return True
            if elapsed_total>=DEFAULT_NO_DATA_TIMEOUT_S and time.monotonic()-last_time>15.0:
                log_msg(f"S{self.id} ERR: NO FLOW TIMEOUT","error")
                self.status="ERROR"; return True
            return False

        flow_rate_obs=0.0
        stop_weight=eff_tgt
        pulses_run=0

        # ── Unified micro pulse-stop-wait-check loop ─────────────────────────
        # Used by BOTH:
        #   • PURE MICRO branch (tgt ≤ micro_threshold_g) — runs from 0g.
        #   • FINAL_MICRO phase after the normal-dispense settle (tgt > thr).
        # Loop per pulse:
        #   1) fire one micro pulse (PULSE_VOLTAGE × gap-adaptive ms);
        #      voltage drops + duration shrinks to TINY when gap ≤ 0.5 g.
        #   2) motor off.
        #   3) wait settle_delay seconds (motor OFF; respects pause / abort).
        #   4) read STABLE scale value (latest, not the pre-pulse reading).
        #   5) update avg gain + remaining; loop again until target reached.
        # Returns (final_weight, pulse_count).
        def _micro_loop(stop_at, label):
            pulses=0
            cur_w=self.smooth(); self.ui_w=cur_w
            if abs(cur_w)>ZERO_RANGE:
                # Mark that the scale is reporting real values
                pass
            # Generous pulse budget so a 5 g pure-micro target can always
            # finish even with very small per-pulse gains.
            est_grams=max(0.5, stop_at - cur_w)
            max_p=max(60, int(est_grams/0.04)+20)
            avg_gain=0.0
            stalled=0
            log_msg(f"S{self.id} {label} loop start  cur={cur_w:.3f}g  "
                    f"target={stop_at:.3f}g  remaining={max(0.0, stop_at-cur_w):.3f}g  "
                    f"max_pulses={max_p}","info")
            while self.running and pulses<max_p:
                if getattr(orch,"paused",False):
                    if self.status!="PAUSED": self._stop(); self.status="PAUSED"
                    time.sleep(0.1); continue
                if self.status=="PAUSED": self.status=label
                if cur_w>=stop_at: break
                # Predictive stop: next pulse would land at/above target
                if cur_w+avg_gain>=stop_at: break
                if _no_data_check(time.monotonic()-start_t): break
                gap=max(0.0, stop_at-cur_w)
                # Extra-careful regime: final MICRO_FINE_GAP_G grams use the
                # tiniest pulse + a softer voltage so the last 0.5 g approach
                # cannot overshoot.
                if gap<=MICRO_FINE_GAP_G:
                    pdur=pT
                    voltage=PULSE_VOLTAGE*MICRO_FINE_VOLT_FACTOR
                else:
                    pdur=_pulse_dur(gap)
                    voltage=PULSE_VOLTAGE
                self.status=label
                self._motor(voltage)
                time.sleep(pdur)
                self._stop()
                # Settle window — motor OFF for settle_delay seconds.
                # Bumps the deadline while paused so the full delay still
                # happens after resume.
                self.status="WAITING"
                _end=time.monotonic()+settle_delay
                while self.running and time.monotonic()<_end:
                    if getattr(orch,"paused",False):
                        _end+=0.05
                    time.sleep(0.05)
                # Stable scale read — LATEST stable value (not pre-pulse)
                new_w=_stable_read(timeout_s=max(1.5, settle_delay+0.5))
                self.ui_w=new_w
                gain=max(0.0, new_w-cur_w)
                avg_gain=gain if pulses==0 else avg_gain*0.6+gain*0.4
                pulses+=1
                log_msg(f"S{self.id}   {label} #{pulses}  "
                        f"pulse={int(pdur*1000)}ms@{voltage:.1f}V  "
                        f"pre={cur_w:.3f}g  post={new_w:.3f}g  gain={gain:+.3f}g  "
                        f"remaining={max(0.0, stop_at-new_w):.3f}g","info")
                cur_w=new_w
                if new_w>last_w_outer[0]:
                    last_w_outer[0]=new_w
                    last_time_outer[0]=time.monotonic()
                if gain<0.005: stalled+=1
                else: stalled=0
                if stalled>=3:
                    log_msg(f"S{self.id} {label} stall (3× <5mg) — exiting loop",
                            "warn")
                    break
            log_msg(f"S{self.id} {label} loop done  pulses={pulses}  "
                    f"final w={cur_w:.3f}g  err={cur_w-stop_at:+.3f}g","info")
            return cur_w, pulses

        # last_w / last_time are mutated by both the main bulk loops and the
        # micro loop; we wrap them in 1-element lists so the inner function
        # can update them without `nonlocal`.
        last_w_outer=[last_w]
        last_time_outer=[last_time]

        try:
            if use_pulse:
                # ════════════════════════════════════════════════════════════
                # TARGET ≤ MICRO_THRESHOLD → PURE MICRO (operator-spec)
                # No bulk phase. The pulse-stop-wait-check loop runs from the
                # start, targeting eff_tgt directly. Examples:
                #   tgt=3 g → pure-micro from 0 → 3 g
                #   tgt=5 g → pure-micro from 0 → 5 g
                # ════════════════════════════════════════════════════════════
                self.status="MICRO"
                log_msg(f"S{self.id} PURE MICRO  tgt={tgt:.2f}g eff={eff_tgt:.2f}g  "
                        f"(tgt≤micro_thr {thr:.1f}g) — pulse-check-wait loop "
                        f"from start","info")
                if abs(self.smooth())>ZERO_RANGE: first_data_seen=True
                settled_w,fm_pulses=_micro_loop(eff_tgt,"MICRO")
                pulses_run+=fm_pulses
                stop_weight=settled_w
            else:
                # ════════════════════════════════════════════════════════════
                # TARGET > MICRO_THRESHOLD → NORMAL DISPENSE + SETTLE + MICRO
                # PHASE 1 — NORMAL COARSE with ML-voltage + ACCELERATION +
                #           SLOWDOWN deceleration. Runs until bulk_stop =
                #           tgt − final_micro_amount_g (default 3 g before
                #           the recipe target).
                #   • ML voltage  : cruise = solve_v(flow_guess) when ML on
                #   • ACCEL       : cv = min(cruise, cv + accel_step) each tick
                #   • DECEL ramp  : last MICRO_TAIL_SLOWDOWN_G g of the coarse
                #                   band linearly decay cv from cruise to
                #                   cruise * 0.4 (status flips to SLOWDOWN)
                # ════════════════════════════════════════════════════════════
                ml_on=self.ml_model_enabled
                flow_guess=6.0 if tgt<15.0 else 9.0
                cruise=solve_v(flow_guess) if ml_on else VOLT_MAX
                accel=self.accel_step
                # PHASE-1 stop boundary (in grams). The micro loop below
                # picks up from here and runs to eff_tgt.
                coarse_stop=bulk_stop                 # = tgt − final_micro
                slow_start=max(0.0, coarse_stop - MICRO_TAIL_SLOWDOWN_G)
                log_msg(f"S{self.id} NORMAL COARSE  tgt={tgt:.2f}g eff={eff_tgt:.2f}g  "
                        f"bulk_stop={coarse_stop:.2f}g  slow_start={slow_start:.2f}g  "
                        f"cruise={cruise:.2f}V (ml={'on' if ml_on else 'off'})  "
                        f"accel={accel:.2f}V/tick","info")
                cv=VOLT_MIN
                self.status="FILLING"
                while self.running:
                    if getattr(orch,"paused",False):
                        if self.status!="PAUSED": self._stop(); self.status="PAUSED"
                        time.sleep(0.1); last_time_outer[0]=time.monotonic(); continue
                    w=self.smooth(); self.ui_w=w; _push_flow(w)
                    if abs(w)>ZERO_RANGE: first_data_seen=True
                    if w-last_w_outer[0]>=0.5:
                        last_w_outer[0]=w; last_time_outer[0]=time.monotonic()
                    if _no_data_check(time.monotonic()-start_t): break
                    if w>=coarse_stop: break
                    # ACCEL + SLOWDOWN deceleration ramp:
                    #   below slow_start  : accelerate toward cruise
                    #   above slow_start  : decelerate linearly toward cruise*0.4
                    if w>=slow_start and MICRO_TAIL_SLOWDOWN_G>0:
                        frac=max(0.0,min(1.0,(w-slow_start)/MICRO_TAIL_SLOWDOWN_G))
                        cv_target=cruise*(1.0-0.6*frac)
                        if cv>cv_target: cv=max(cv_target, cv-accel)
                        else:            cv=min(cv_target, cv+accel)
                        self.status="SLOWDOWN"
                    else:
                        cv=min(cruise, cv+accel)
                        self.status="FILLING"
                    self._motor(cv); self.ui_v=cv
                    time.sleep(0.02)
                self._stop(); self.ui_v=0.0
                flow_rate_obs=_live_flow()
                self.status="WAITING"

                # PHASE 2 — motor-off settle (settling_delay_s, e.g. 1 s)
                pre_stop_w=self.smooth(); self.ui_w=pre_stop_w
                if self.status not in ("ERROR","STOPPED"):
                    self.status="SETTLING"
                    log_msg(f"S{self.id} NORMAL STOP  motor OFF  "
                            f"w={pre_stop_w:.3f}g  (target {tgt:.2f}g, "
                            f"bulk_stop {coarse_stop:.2f}g) — "
                            f"settling {settle_delay:.2f}s","info")
                    _settle_end=time.monotonic()+settle_delay
                    while self.running and time.monotonic()<_settle_end:
                        if getattr(orch,"paused",False):
                            _settle_end+=0.05
                            time.sleep(0.05); continue
                        time.sleep(0.05)

                # PHASE 3 — stable read + remaining calculation
                # PHASE 4 — FINAL_MICRO pulse-check-wait loop to eff_tgt
                if self.status not in ("ERROR","STOPPED"):
                    settled_w=_stable_read(timeout_s=max(2.0, fall_s+1.5))
                    remaining=max(0.0, eff_tgt-settled_w)
                    log_msg(f"S{self.id} STABLE w={settled_w:.3f}g  "
                            f"remaining {remaining:.3f}g  to eff_tgt={eff_tgt:.3f}g  "
                            f"(target {tgt:.2f}g, offset {offset:+.2f}g)","info")
                    if settled_w<eff_tgt and self.running:
                        self.status="FINAL_MICRO"
                        log_msg(f"S{self.id} FINAL_MICRO engaged "
                                f"({remaining:.3f}g to deliver via micro pulses)",
                                "info")
                        settled_w,fm_pulses=_micro_loop(eff_tgt,"FINAL_MICRO")
                        pulses_run+=fm_pulses
                    stop_weight=settled_w

                # ── MICRO TOP-UP (pulse → wait → measure) ──────────────────────
                # Standard top-up loop: stop the moment we reach the EFFECTIVE
                # target (eff_tgt = tgt − target_offset_g). Using eff_tgt
                # honors the "stop early" offset throughout the fill (not
                # just at the coarse-cutoff). If consecutive pulses fail to
                # deliver material (auger empty, bridged hopper) break early.
                topup_used=0
                last_settled=settled_w
                stalled=0
                while topup_used<topup_max and self.running:
                    if settled_w>=eff_tgt: break
                    gap=eff_tgt-settled_w
                    pdur=_pulse_dur(gap)
                    self.status="PULSING"; self._motor(PULSE_VOLTAGE)
                    time.sleep(pdur); self._stop()
                    self.status="WAITING"
                    settled_w=_stable_read(timeout_s=1.5)
                    topup_used+=1
                    # Stall detection: 3 consecutive pulses delivering <0.005g
                    if (settled_w-last_settled)<0.005: stalled+=1
                    else: stalled=0
                    last_settled=settled_w
                    if stalled>=3:
                        log_msg(f"S{self.id} top-up stalled (3× <5mg) — engaging rescue","warn")
                        break
                # ── RESCUE PULSES (FIX for chronic 0.4–0.5 g undershoot) ──────
                # Same loop, against eff_tgt so the rescue ALSO respects the
                # stop-early offset.
                rescue_used=0
                rescue_stalled=0
                last=settled_w
                while (rescue_used<TOPUP_RESCUE_PULSES and self.running
                       and settled_w<eff_tgt
                       and (eff_tgt-settled_w)>=TOPUP_RESCUE_TRIGGER_G):
                    gap=eff_tgt-settled_w
                    pdur=max(_pulse_dur(gap),0.04)  # rescue pulses are never shorter than 40 ms
                    self.status="PULSING"; self._motor(TOPUP_RESCUE_VOLTAGE)
                    time.sleep(pdur); self._stop()
                    self.status="WAITING"
                    settled_w=_stable_read(timeout_s=1.5)
                    rescue_used+=1
                    if (settled_w-last)<0.005: rescue_stalled+=1
                    else: rescue_stalled=0
                    last=settled_w
                    if rescue_stalled>=3:
                        log_msg(f"S{self.id} rescue stalled — hopper likely empty","error")
                        break
                if (topup_used>=topup_max or rescue_used>=TOPUP_RESCUE_PULSES) and settled_w<eff_tgt:
                    log_msg(f"S{self.id} top-up cap hit (main={topup_used} rescue={rescue_used}) — "
                            f"actual {settled_w:.2f}g < eff_tgt {eff_tgt:.2f}g (tgt {tgt:.2f}g)","warn")
                pulses_run+=topup_used+rescue_used
        finally:
            self._stop(); self.ui_v=0.0
            time.sleep(0.1)
            motor_off(self.id)
            self.actual=self.smooth(); self.ui_w=self.actual; self.running=False
            if self.status not in ("STOPPED","ERROR"):
                self.status="READY TO DROP"
            d=self.actual-self.target
            # ── ADAPTIVE LEARNING: nudge learned_compensation_g toward
            # closing the residual error. Positive d (overshoot) → raise comp
            # so next fill cuts earlier; negative d (undershoot) → lower it.
            new_learn=learned_c
            try:
                if self.status not in ("ERROR","STOPPED") and not use_pulse:
                    new_learn=learned_c+LEARN_ALPHA*d
                    new_learn=max(-PRECISION_LEARNED_CLAMP_G,
                                  min(PRECISION_LEARNED_CLAMP_G,new_learn))
                    self.learned_compensation_g=new_learn
                    _persist_learned_comp(self.id,new_learn)
            except Exception: pass
            ok=(d>=0.0 and d<=UPPER_TOL_G)
            log_msg(f"S{self.id} FILL DONE tgt={self.target:.2f}g "
                    f"stop_w={stop_weight:.2f}g actual={self.actual:.2f}g "
                    f"err={d:+.3f}g flow={flow_rate_obs:.2f}g/s "
                    f"pulses={pulses_run} comp={learned_c:+.2f}g→{new_learn:+.2f}g",
                    "ok" if ok else "warn")
            if app_ref: app_ref.after(0,lambda: self._btns("ready_to_drop"))

    def drop(self):
        if not conveyor_running:
            log_msg(f"S{self.id} DROP BLOCKED — conveyor interlock not satisfied","error")
            return
        self._btns("dropping")
        threading.Thread(target=self._drop_t,daemon=True).start()

    def _drop_t(self):
        """Drop sequence — reads angles, speeds, and hold times from
        self.servo (per-station, configured via the Behavior tab).

        Hardened with:
          - Rapid-click protection via `_servo_busy_lock` (refused if busy).
          - Per-station movement timeout (deadline check at each phase).
          - Always-detach in `finally` (settle + send -1) so the servo never
            sits under holding load even if the sequence aborts."""
        if not self._servo_busy_lock.acquire(blocking=False):
            log_msg(f"S{self.id} drop refused — servo busy","warn"); return
        s=self.servo
        deadline=time.monotonic()+getattr(self,"servo_move_timeout_s",SERVO_MOVE_TIMEOUT_S_DEFAULT)
        def _expired():
            if time.monotonic()>deadline:
                log_msg(f"S{self.id} drop TIMEOUT — aborting sequence","error")
                self.status="ERROR"
                return True
            return False
        try:
            self.status="POURING"
            self._servo(s["drop_angle_start"]); time.sleep(0.2)
            if _expired(): return
            self._sweep(s["drop_angle_start"], s["drop_angle_end"], s["drop_speed_dt"])
            if _expired(): return
            time.sleep(s["drop_hold_s"])
            if s["drop_tap_count"]>0:
                self.status="TAPPING"
                for _ in range(int(s["drop_tap_count"])):
                    if _expired(): return
                    self._servo(s["drop_tap_low"]);  time.sleep(s["drop_tap_dt"])
                    self._servo(s["drop_tap_high"]); time.sleep(s["drop_tap_dt"])
                settle_from=s["drop_tap_high"]
            else:
                settle_from=s["drop_angle_end"]
            if _expired(): return
            self.status="RESETTING"
            self._sweep(settle_from, s["return_angle"], s["return_speed_dt"])
            self.status="DONE"
            log_msg(f"S{self.id} drop OK","ok")
        finally:
            # Always settle + detach so the servo isn't left holding under
            # load, even on exception/timeout/abort. This is the core of the
            # "do not keep servo under holding load after movement" requirement.
            try: self._servo_release()
            except Exception: pass
            try: self._servo_busy_lock.release()
            except Exception: pass
            if app_ref: app_ref.after(0,lambda: self._btns("idle"))

    def eject(self):
        """Fire-and-forget backflip (used by manual UI button)."""
        threading.Thread(target=self._eject_blocking,daemon=True).start()

    def _eject_blocking(self):
        """Synchronous backflip — reads angles/speeds from self.servo.
        Called by the orchestrator's lane worker after operator EJECT choice
        and by the Diagnostic tab's TEST EJECT button. Same hardening as
        `_drop_t`: busy-lock, movement timeout, always-detach in finally."""
        if not self._servo_busy_lock.acquire(blocking=False):
            log_msg(f"S{self.id} eject refused — servo busy","warn"); return
        s=self.servo
        deadline=time.monotonic()+getattr(self,"servo_move_timeout_s",SERVO_MOVE_TIMEOUT_S_DEFAULT)
        def _expired(phase):
            if time.monotonic()>deadline:
                log_msg(f"S{self.id} eject TIMEOUT ({phase})","error")
                self.status="ERROR"
                return True
            return False
        try:
            self.status="EJECTING"
            self._sweep(s["eject_angle_start"], s["eject_angle_end"], s["eject_speed_dt"])
            if _expired("post-eject-sweep"): return
            time.sleep(s["eject_hold_s"])
            if _expired("post-hold"): return
            self._sweep(s["eject_angle_end"], s["return_angle"], s["return_speed_dt"])
            self.status="EJECTED"
            log_msg(f"S{self.id} EJECT complete (out-of-tol batch routed to inside basket)","warn")
        finally:
            try: self._servo_release()
            except Exception: pass
            try: self._servo_busy_lock.release()
            except Exception: pass
            if app_ref: app_ref.after(0,lambda: self._btns("idle"))

    def stop(self):
        self.running=False; self._stop()
        self.status="STOPPED"; self.ui_v=0.0
        motor_off(self.id)
        if app_ref: app_ref.after(0,lambda: self._btns("idle"))

    # ── Manual diagnostics (called by the Diagnostics tab) ──────────────────
    def test_motor_pulse(self,volts=MANUAL_TEST_VOLTAGE,duration_s=MAX_MANUAL_MOTOR_S):
        """Run the auger at `volts` for at most `duration_s` seconds.
        Always issues a stop in finally — even if interrupted. Refuses if a
        recipe is in flight or if the board is offline."""
        if orch.running:
            return ("BUSY","recipe in progress")
        if self.board not in boards or not boards[self.board].connected:
            return ("OFFLINE",f"board {self.board}")
        d=max(0.0,min(MAX_MANUAL_MOTOR_S,float(duration_s)))
        v=max(0.0,min(SUPPLY_VOLTAGE,float(volts)))
        self.status="MANUAL_MOTOR"
        try:
            motor_on(self.id)
            self._motor(v); self.ui_v=v
            t0=time.monotonic()
            while time.monotonic()-t0<d:
                if orch.running: break  # something started a recipe; bail
                time.sleep(0.05)
            return ("OK",f"{v:.1f}V × {d:.1f}s")
        except Exception as e:
            return ("ERROR",str(e))
        finally:
            self._stop(); self.ui_v=0.0
            motor_off(self.id)
            self.status="IDLE"
            if app_ref: app_ref.after(0,lambda: self._btns("idle"))

    def test_dispense_blocking(self,target_g=MANUAL_DISP_TEST_TARGET_G,timeout_s=30.0):
        """Run a single fill of `target_g` grams using the regular _fill_t
        machinery, but DO NOT drop afterwards. Returns (level, msg)."""
        if orch.running:
            return ("BUSY","recipe in progress")
        if self.board not in boards or not boards[self.board].connected:
            return ("OFFLINE",f"board {self.board}")
        try:
            self.tare(); time.sleep(0.3)
            self.start_fill(float(target_g))
            t0=time.monotonic()
            while self.running and time.monotonic()-t0<timeout_s:
                time.sleep(0.05)
            if self.running:
                self.stop()
                return ("TIMEOUT",f"fill exceeded {timeout_s:.0f}s")
            actual=self.smooth()
            d=actual-target_g
            return ("OK",f"target {target_g:.1f}g → actual {actual:.2f}g  Δ{d:+.2f}g")
        except Exception as e:
            self._stop(); motor_off(self.id)
            return ("ERROR",str(e))

    def _btns(self,mode):
        m={"idle":(True,False,False),"filling":(False,True,False),
           "ready_to_drop":(False,False,True),"dropping":(False,False,False)}
        s,st,d=m.get(mode,(True,False,False))
        _DIM="#1a1c20"; _MUT="#8a8d94"
        def _cfg(btn,active,obg,ofg):
            if btn: btn.config(state="normal" if active else "disabled",
                               bg=obg if active else _DIM,
                               fg=ofg if active else _MUT)
        _cfg(self.b_start,s,"#00e676","#000")
        _cfg(self.b_stop, st,"#ff1744","#e8eaed")
        _cfg(self.b_drop, d, "#ffab00","#000")

BG_CARD2="#1a1c20"

stations:dict[int,Station]={sid:Station(sid) for sid in INGREDIENTS}

def refresh_station_behavior():
    """Re-apply behavior fields from INGREDIENTS into every live Station instance.
    Call this after save_station_config() so changes take effect immediately."""
    for sid, st in stations.items():
        st.apply_behavior(INGREDIENTS.get(sid,{}))

# ─── ORCHESTRATOR (parallel state machine) ────────────────────────────────────
class Orchestrator:
    """
    Coordinates a recipe across all dispensing lanes in PARALLEL with a
    per-lane state machine and an aggregate orchestrator state.

    Per-lane states (recorded on Station.status):
      DISPENSING → WEIGHING → WEIGHT_VALIDATION
        ├─► (in tolerance)        SERVO_DROP_TO_CONVEYOR → DONE_VALID
        └─► (out of tolerance)    WAITING_OPERATOR_DECISION
                                    ├─ EJECT   → SERVO_EJECT_TO_BASKET → DONE_EJECTED
                                    ├─ RETRY   → DISPENSING (re-enter)
                                    └─ CANCEL  → FAULT (whole recipe aborts)

    Aggregate orchestrator states (self.state):
      IDLE → RECIPE_STARTED → CONVEYOR_RUNNING → DISPENSING
        → MIXING (set on first valid drop) → FINAL_MIX_60_SECONDS
        → COMPLETE | FAULT

    Conveyor: turned on once at recipe start, off once at the very end.
    Mixer:    turned on at the first valid drop, off after FINAL_MIX_60_SECONDS.
    """
    def __init__(self):
        self.running=False
        self.state="IDLE"
        self.oid=None
        self.recipe=None
        self.lanes:list[int]=[]
        self.weights:dict[int,float]={}
        self.paused=False
        # Outcome reporting (read by GUI on_done)
        self.last_outcome="IDLE"            # "SUCCESS" | "PARTIAL" | "FAILED" | "IDLE"
        self.last_failed_lanes:list[int]=[]
        self.last_ejected_lanes:list[int]=[]
        self.lane_outcomes:dict[int,str]={} # sid -> "VALID" | "EJECTED" | "CANCELLED" | "ERROR"
        # Cross-lane coordination
        self._abort=threading.Event()
        self._modal_lock=threading.Lock()
        self._mixer_lock=threading.Lock()
        # FIX (partial-completion popup bug): the operator-decision modal must
        # NOT pop up while OTHER lanes are still actively dispensing. This
        # event is set by the main run loop once every lane has finished its
        # DISPENSE phase. Each lane worker now waits on this event before
        # entering WEIGHT_VALIDATION / operator decision. So one good lane
        # completing early can no longer halt the whole process; all actives
        # finish first, then validation+popups happen sequentially for any
        # lanes that ended out-of-tolerance.
        self._all_dispense_done=threading.Event()
        self._dispense_done_count=0
        self._dispense_done_lock=threading.Lock()
        self.mixer_started=False
        self.last_drop_t=None               # time.monotonic() of last valid drop
        # Operator-decision plumbing
        self._decision_event=threading.Event()
        self._decision_value=None
        self.pending_decision=None          # payload currently shown to operator

    # ── State setter ─────────────────────────────────────────────────────────
    def _set_state(self,s):
        self.state=s
        log_msg(f"STATE → {s}","info")

    # ── Conveyor / mixer single source of truth ──────────────────────────────
    def _conveyor_on(self):
        global conveyor_running
        set_motor(4,CONVEYOR_CH,CONVEYOR_DUTY)
        conveyor_running=True
        if app_ref: app_ref.after(0,lambda: app_ref.set_cv("RUNNING"))

    def _conveyor_off(self):
        global conveyor_running
        set_motor(4,CONVEYOR_CH,0)
        conveyor_running=False
        if app_ref: app_ref.after(0,lambda: app_ref.set_cv("IDLE"))

    def _mixer_on(self):
        set_vibe_freeze(True)
        set_motor(4,MIXER_CH,MIXER_DUTY)
        if app_ref: app_ref.after(0,lambda: app_ref.set_mx("RUNNING"))
        log_msg("Mixer ON — first valid drop landed","ok")

    def _mixer_off(self,label="COMPLETE"):
        set_motor(4,MIXER_CH,0)
        set_vibe_freeze(False)
        if app_ref: app_ref.after(0,lambda lb=label: app_ref.set_mx(lb))

    def _safe_stop(self,reason):
        self._abort.set()
        self._mixer_off("STOPPED")
        self._conveyor_off()
        self._set_state(reason)

    # ── Pause / resume ───────────────────────────────────────────────────────
    def pause(self):
        if self.running and not self.paused:
            self.paused=True
            log_msg("GLOBAL PAUSE — motors suspended","warn")
            set_motor(4,CONVEYOR_CH,0)
            set_motor(4,MIXER_CH,0)

    def resume(self):
        if self.running and self.paused:
            self.paused=False
            log_msg("GLOBAL RESUME","info")
            if conveyor_running: set_motor(4,CONVEYOR_CH,CONVEYOR_DUTY)
            if self.mixer_started and not self._abort.is_set():
                set_motor(4,MIXER_CH,MIXER_DUTY)

    # ── Dispatch (entry point) ───────────────────────────────────────────────
    def dispatch(self,recipe,weights:dict):
        if self.running:
            log_msg("Already running","warn"); return False
        errors=check_stock(weights)
        if errors:
            for e in errors: log_msg(e,"error")
            return False
        # Reset all per-recipe state
        self._abort.clear()
        self._decision_event.clear()
        self._decision_value=None
        self.pending_decision=None
        self.mixer_started=False
        self.last_drop_t=None
        self.lane_outcomes={}
        # Reset the all-fills-done barrier for this run
        self._all_dispense_done.clear()
        self._dispense_done_count=0
        self.last_failed_lanes=[]
        self.last_ejected_lanes=[]
        self.last_outcome="IDLE"
        self.running=True
        self.oid=next_oid()
        self.recipe=recipe
        self.weights=dict(weights)
        self.lanes=list(weights.keys())
        threading.Thread(target=self._run,args=(recipe,weights),daemon=True).start()
        return True

    # ── Operator-decision plumbing ───────────────────────────────────────────
    def _ask_operator(self,sid,target,actual):
        """Block the calling lane worker until the operator picks
        EJECT/RETRY/CANCEL. Defaults to CANCEL after the timeout."""
        delta=actual-target
        payload={"sid":sid,"target":float(target),"actual":float(actual),
                 "delta":float(delta),"label":stations[sid].label,
                 "tolerance_g":float(getattr(stations[sid],"scale_tolerance_grams",DEFAULT_SCALE_TOLERANCE_G))}
        self.pending_decision=payload
        self._decision_event.clear()
        self._decision_value=None
        if app_ref:
            app_ref.after(0,lambda p=payload: app_ref.show_drop_decision(p))
        log_msg(f"S{sid} OUT-OF-TOL Δ{delta:+.2f}g — waiting operator","warn")
        got=self._decision_event.wait(timeout=OPERATOR_DECISION_TIMEOUT_S)
        choice=self._decision_value if got and self._decision_value else "CANCEL"
        self.pending_decision=None
        if not got:
            log_msg(f"S{sid} operator timeout — defaulting to CANCEL","error")
        log_msg(f"S{sid} operator decision: {choice}","info")
        return choice

    def submit_decision(self,value):
        """Called by the GUI when the operator clicks a modal button."""
        if value not in ("EJECT","RETRY","CANCEL"):
            log_msg(f"Bad decision value: {value}","error"); return
        self._decision_value=value
        self._decision_event.set()

    # ── Per-lane worker ──────────────────────────────────────────────────────
    def _lane_worker(self,sid,target):
        """Two-phase parallel lane worker.

        Phase 1 (DISPENSE): all lanes run in parallel. As soon as a lane's
        _fill_t finishes (settled + topped-up), the lane signals the barrier
        and then BLOCKS until every other lane has also finished dispensing.
        This guarantees no popup interrupts an active dispense — addresses
        the "popup stops the whole process while others are still dispensing"
        complaint.

        Phase 2 (VALIDATE / DECIDE / DROP): once all lanes are out of phase 1,
        each lane sequentially (via _modal_lock) validates its weight and, if
        out-of-tolerance, raises the operator-decision modal. Lanes that are
        in tolerance drop straight to the conveyor without waiting.

        A RETRY decision re-enters phase 1 for that lane only — the barrier
        is NOT re-armed; other lanes have long-since dropped.
        """
        st=stations[sid]
        st.lane_outcome=None
        attempt=0
        # Snapshot how many lanes joined this run so the barrier completes
        # even if a lane errors out mid-fill.
        total_lanes=len(self.lanes) if self.lanes else 1
        # PHASE 1 — DISPENSE (first pass only barricades; retries skip it)
        attempt+=1
        st.status="DISPENSING"
        st.start_fill(target)
        while st.running and not self._abort.is_set():
            time.sleep(0.05)
        # Signal that THIS lane has finished phase 1 and wait for siblings.
        # Crucially, no operator modal can pop while any lane is still
        # actively dispensing.
        with self._dispense_done_lock:
            self._dispense_done_count+=1
            if self._dispense_done_count>=total_lanes:
                self._all_dispense_done.set()
        if not self._abort.is_set():
            # Wait for all sibling lanes — but check abort periodically.
            while not self._all_dispense_done.is_set() and not self._abort.is_set():
                self._all_dispense_done.wait(timeout=0.2)
        # Phase-2 loop — sequentially handles validation + decisions.
        while not self._abort.is_set():
            # WEIGHING — let EMA reflect fully-settled weight
            st.status="WEIGHING"
            time.sleep(0.3)
            actual=st.smooth(); st.actual=actual
            delta=actual-target
            st.status="WEIGHT_VALIDATION"
            tol_g=getattr(st,"scale_tolerance_grams",DEFAULT_SCALE_TOLERANCE_G)
            in_tol=(stations[sid].status not in ("ERROR","STOPPED")
                    and 0.0<=delta<=tol_g)
            if in_tol:
                st.status="SERVO_DROP_TO_CONVEYOR"
                st._drop_t()
                self._note_valid_drop()
                self.lane_outcomes[sid]="VALID"
                st.lane_outcome="VALID"
                return
            # Out of tolerance — operator modal (serialized across lanes).
            # Only reached once all phase-1 fills are complete; popping it
            # earlier (the old bug) interrupted other lanes.
            with self._modal_lock:
                if self._abort.is_set(): break
                st.status="WAITING_OPERATOR_DECISION"
                choice=self._ask_operator(sid,target,actual)
            if choice=="RETRY":
                attempt+=1
                log_msg(f"S{sid} RETRY (attempt {attempt}) — clear funnel first","info")
                st.tare()
                # Single-lane retry — other lanes have already dropped, so we
                # do NOT touch the barrier. Just re-run the fill for this lane.
                st.status="DISPENSING"
                st.start_fill(target)
                while st.running and not self._abort.is_set():
                    time.sleep(0.05)
                if self._abort.is_set(): break
                continue
            elif choice=="EJECT":
                st.status="SERVO_EJECT_TO_BASKET"
                st._eject_blocking()
                self.lane_outcomes[sid]="EJECTED"
                self.last_ejected_lanes.append(sid)
                st.lane_outcome="EJECTED"
                return
            else:  # CANCEL — only this cancel actually aborts the recipe.
                self._abort.set()
                self.lane_outcomes[sid]="CANCELLED"
                st.lane_outcome="CANCELLED"
                return
        # Aborted before completion
        if sid not in self.lane_outcomes:
            self.lane_outcomes[sid]="CANCELLED"
            st.lane_outcome="CANCELLED"

    def _note_valid_drop(self):
        with self._mixer_lock:
            self.last_drop_t=time.monotonic()
            if not self.mixer_started:
                self._mixer_on()
                self.mixer_started=True
                self._set_state("MIXING")

    # ── Main run loop ────────────────────────────────────────────────────────
    def _run(self,recipe,weights):
        try:
            log_msg(f"ORDER #{self.oid}  [{recipe}]","info")
            # RECIPE_STARTED → conveyor on → CONVEYOR_RUNNING
            self._set_state("RECIPE_STARTED")
            self._conveyor_on()
            self._set_state("CONVEYOR_RUNNING")
            time.sleep(1.5)
            if self._abort.is_set() or not self.running:
                self._safe_stop("FAULT"); self.last_outcome="FAILED"; return
            # DISPENSING (parallel) — spawn lane workers
            self._set_state("DISPENSING")
            workers=[threading.Thread(target=self._lane_worker,args=(s,w),daemon=True)
                     for s,w in weights.items()]
            for t in workers: t.start()
            for t in workers: t.join()
            # All lanes have settled
            if self._abort.is_set():
                self._mixer_off("STOPPED")
                self._conveyor_off()
                self._set_state("FAULT")
                self.last_outcome="FAILED"
                self.last_failed_lanes=[s for s,o in self.lane_outcomes.items()
                                        if o not in ("VALID",)]
                # FIX (Production Log): aborted recipes now ALSO record one
                # row per ingredient with status='CANCELLED' / actual outcome
                # so the log reflects what the machine actually did. Previously
                # CANCELLED orders wrote zero rows and the operator saw nothing.
                for s,tgt in weights.items():
                    outcome=self.lane_outcomes.get(s,"CANCELLED")
                    log_production(self.oid,recipe,s,stations[s].label,tgt,
                                   stations[s].actual,status_override=outcome)
                log_msg(f"ORDER #{self.oid} CANCELLED","error")
                return
            # FINAL_MIX_60_SECONDS — only if mixer ever started
            if self.mixer_started:
                self._set_state("FINAL_MIX_60_SECONDS")
                target_end=(self.last_drop_t or time.monotonic())+MIXER_DURATION_S
                while time.monotonic()<target_end and not self._abort.is_set():
                    time.sleep(0.1)
                self._mixer_off("COMPLETE")
            else:
                log_msg("Mixer never started — no valid ingredient drops","warn")
            # Conveyor off (single source of truth)
            self._conveyor_off()
            # Inventory deduction (ingredients left the hopper either way)
            for s in weights:
                deduct_stock(s, stations[s].actual)
            if app_ref: app_ref.after(0,lambda: app_ref._inv_refresh() if hasattr(app_ref,"_inv_refresh") else None)
            # CSV production log — write the actual per-lane outcome so EJECTED
            # / CANCELLED / VALID is visible to the operator instead of the
            # generic OK/WARN inferred only from tolerance.
            for s,tgt in weights.items():
                outcome=self.lane_outcomes.get(s)
                status_ov=outcome if outcome and outcome!="VALID" else None
                log_production(self.oid,recipe,s,stations[s].label,tgt,
                               stations[s].actual,status_override=status_ov)
            # Outcome
            valid=[s for s,o in self.lane_outcomes.items() if o=="VALID"]
            ejected=[s for s,o in self.lane_outcomes.items() if o=="EJECTED"]
            self.last_failed_lanes=ejected[:]
            self.last_ejected_lanes=ejected[:]
            if len(valid)==len(weights):
                self._set_state("COMPLETE"); self.last_outcome="SUCCESS"
                log_msg(f"ORDER #{self.oid} COMPLETE — all {len(valid)} ingredients valid","ok")
            elif valid:
                self._set_state("COMPLETE"); self.last_outcome="PARTIAL"
                log_msg(f"ORDER #{self.oid} PARTIAL — {len(valid)} valid, "
                        f"{len(ejected)} ejected to inside basket","warn")
            else:
                self._set_state("FAULT"); self.last_outcome="FAILED"
                log_msg(f"ORDER #{self.oid} FAILED — no valid drops","error")
        except Exception as e:
            log_msg(f"Recipe error: {e}","error")
            self._safe_stop("FAULT"); self.last_outcome="FAILED"
        finally:
            self.running=False
            if app_ref: app_ref.after(0,app_ref.on_done)

    def abort(self):
        """Hard abort: signal abort, unblock any pending operator decision,
        kill all motors, stop active station fills."""
        global conveyor_running
        self._abort.set()
        self.paused=False
        # Release the all-fills-done barrier so any waiting lane worker can
        # observe the abort and exit cleanly.
        self._all_dispense_done.set()
        if not self._decision_event.is_set():
            self._decision_value="CANCEL"; self._decision_event.set()
        set_motor(4,CONVEYOR_CH,0); set_motor(4,MIXER_CH,0)
        set_vibe_freeze(False)
        conveyor_running=False
        for st in stations.values(): st.stop()
        self.state="IDLE"; self.running=False

orch=Orchestrator()
