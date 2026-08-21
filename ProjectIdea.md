# ◈ TeaMatrix Industrial Console — Project Idea

## Brief Idea (read this first)

TeaMatrix is an automated tea-blending machine.
A Raspberry Pi 5 runs a touchscreen HMI (Tkinter) that talks to four
Cytron Motion 2350 Pro microcontroller boards over USB-serial. Together they
drive **13 ingredient stations**, a **conveyor belt**, and an **industrial
mixer**. The operator picks a recipe; the machine dispenses every ingredient
in parallel onto load cells, validates the weight of each one, drops the
in-tolerance lanes onto the conveyor, ejects the out-of-tolerance lanes
into an inside basket, and runs the mixer to finish the batch. Every order
is logged to `production_log.csv`. Technicians can tune per-station
dispensing behavior (mode, margins, pulse durations, micro-tail, servo
sequence, target offset) from a password-gated **Behavior** tab so the same
machine can dispense fine peko leaves and chunky cinnamon bark accurately
without code changes.

---

## 1. What the machine does

1. **Hold ingredients in 13 hoppers** (5 base teas + 8 additives) above
   independent augers and load cells.
2. **Run a recipe** (chosen from a JSON-backed library) — the orchestrator
   spawns one worker thread per ingredient and runs them in parallel.
3. **Dispense each ingredient precisely** to the recipe's target weight,
   adapting the auger speed and pulse strategy to the size of the target
   and the per-station tuning saved in `station_config.json`.
4. **Validate the weight** of each fill against a per-station tolerance
   band; in-tolerance lanes drop their material onto the moving conveyor,
   out-of-tolerance lanes ask the operator (EJECT / RETRY / CANCEL).
5. **Mix and serve** — once the first valid drop lands, the mixer starts;
   60 s after the last drop, it stops. The conveyor runs from the start
   of the order to the end.
6. **Log every order** to `production_log.csv` (10 columns, one row per
   ingredient: OrderID / Timestamp / Recipe / Station / Ingredient /
   Target / Actual / Δ / Tolerance / Status).

## 2. Why it is built this way

* **Parallel lane workers** make total batch time = max(individual fill
  times) instead of sum. The orchestrator uses a **dispense-done barrier**
  so the operator-decision modal only appears AFTER every lane finishes
  dispensing, never interrupting an active fill.
* **Two-phase fill (COARSE → MICRO)** balances speed against accuracy:
  - Small targets (≤ `micro_threshold_g`, default 5 g) → pulse-mode
    micro-dispensing for the entire fill.
  - Large targets → cruise voltage to within 3 g of target, brief
    voltage ramp-down (SLOWDOWN), then pulse-mode for the last 3 g
    (configurable per station via `auto_micro_tail_g`).
* **Top-up + rescue pulses** after the auger stops correct residual
  under/overshoot using stable-read feedback.
* **Per-station Behavior tab** exposes every tuning knob so a single code
  base handles ingredients with wildly different flow characteristics
  (loose petals vs. dense BOPF vs. sticky bergamot).
* **Tech-mode gating** locks editing tabs (Behavior, Recipe Change,
  Diagnostic) behind a PIN so floor operators can run the line without
  accidentally drifting calibration.

## 3. Hardware architecture

```
Raspberry Pi 5
├── /dev/module_01  →  Board 1  → Stations 1–4  (base teas)
├── /dev/module_02  →  Board 2  → Stations 5–8  (additives)
├── /dev/module_03  →  Board 3  → Stations 9–12 (botanicals)
└── /dev/module_04  →  Board 4  → Station 13   +  Conveyor (slot 2)  +  Mixer (slot 3)
```

Each station has:
* 12 V DC gear motor driving the auger (PWM through the Motion Pro
  H-bridge, 20 kHz).
* 5 V MG996R servo controlling the drop/eject linkage.
* 100 g load cell + HX711 ADC (bit-banged on the Motion Pro).

The boards run identical CircuitPython firmware (`code.py`) that exposes
an ASCII serial protocol: `W:ALL` reads all four channels, `D:ch:duty`
sets a motor's PWM, `S:ch:angle` moves a servo, `T:n` tares a scale,
`X` is the emergency-stop broadcast.

## 4. Software architecture

```
teamatrix_pro.py       Tkinter HMI (Dashboard, Inventory, Prod Log,
                       Health, Behavior, Recipe Change, Diagnostic)
teamatrix_backend.py   Board class (serial I/O), Station class (per-lane
                       fill state machine), Orchestrator class (recipe
                       state machine), CSV/JSON persistence
code.py / hx711_gpio.py  Motion Pro firmware (identical across 4 boards)
```

Persistence files (all next to the source, absolute paths used in code):
| File | Role |
|------|------|
| `station_config.json`  | Per-station Behavior tab values |
| `recipes.json`         | Recipe library (name → {station: grams}) |
| `inventory.json`       | Container stock + capacity per station |
| `ingredients_catalogue.json` | Available ingredient names |
| `production_log.csv`   | One row per ingredient per order |
| `refill_log.csv`       | One row per manual refill event |

## 5. Dispense strategy in detail

### Mode selection
Each station has a `dispense_mode` ∈ {`auto`, `micro`, `normal`}.
* **`auto`** (default): if target ≤ `micro_threshold_g` (default 5 g),
  the WHOLE fill is pulse-mode. Otherwise the COARSE → SLOWDOWN →
  MICRO_TAIL → SETTLE → TOP-UP → RESCUE flow is used.
* **`micro`**: pulse-mode for the entire fill, regardless of target size.
* **`normal`**: legacy COARSE → FINE → SETTLE → TOP-UP. No micro tail.

### Phases of a large-target auto fill (target = 10 g, tail = 3 g)
| Phase | Weight band | Action |
|-------|-------------|--------|
| COARSE | 0 → ~6 g | Auger ramps to cruise voltage (ML-model voltage) |
| SLOWDOWN | ~6 → 7 g | Voltage decays linearly to ~40 % of cruise |
| MICRO_TAIL | 7 → 10 g | Gap-adaptive pulses (110/60/35/20 ms at 2.8 V) |
| SETTLE | — | Wait `settle_ms` then read until `stable_sample_count` consecutive readings within `stable_window_g` |
| TOP-UP | settled < eff_tgt | Up to `max_topup_pulses` gap-adaptive pulses at 2.8 V |
| RESCUE | settled < eff_tgt − 0.10 g | Up to 8 stronger pulses at 3.4 V |

### `target_offset_g` — "stop early"
Positive `target_offset_g` reduces the effective target so the fill stops
short of the recipe target. Example: target = 5 g, offset = 0.5 g →
`eff_tgt = 4.5 g`, the fill lands at 4.5 g. The same `eff_tgt` is used
by COARSE, SLOWDOWN, MICRO_TAIL, TOP-UP, and RESCUE so the entire loop
respects the "land at tgt − offset" goal.

### Scale spike rejection
The HX711 channels occasionally emit huge transient values from EMI or
DAT-line glitches. The smoothing pipeline in `Station.smooth()` discards
samples that jump more than 25 g in a single tick (unless three
consecutive jumps insist on a real step change), then medians across the
last 5 samples, then EMA-filters at α=0.7. This stops phantom over-target
cuts during normal fills.

## 6. Recipe orchestration

```
IDLE
  │  operator presses START
  ▼
RECIPE_STARTED   ── conveyor on
  ▼
CONVEYOR_RUNNING ── 1.5 s ramp-up
  ▼
DISPENSING       ── PARALLEL lane workers
  │                 each lane: COARSE → SLOWDOWN → MICRO_TAIL →
  │                 SETTLE → TOP-UP → RESCUE
  ▼
ALL_FILLS_DONE   ── dispense-done barrier releases all lane workers
  ▼
VALIDATE / DROP / DECIDE   (serialized via _modal_lock)
  │      in tolerance  → SERVO_DROP_TO_CONVEYOR
  │      out of tol    → operator EJECT / RETRY / CANCEL
  ▼
FINAL_MIX_60_SECONDS  ── 60 s after the LAST valid drop
  ▼
COMPLETE | PARTIAL | FAILED
  │
  ▼  on_done() pops the order off the queue if SUCCESS or PARTIAL;
     FAILED keeps it at the queue head for operator retry.
```

### Safety
* **Top-bar ⬛ E-STOP** broadcasts `X` to every board → all motors off →
  servo signals released → recipe aborted.
* **Watchdog**: each board's firmware halts motors if it does not
  receive a serial command within 700 ms; the Pi sends `W:ALL` every
  500 ms as a heartbeat.
* **Servo cooldown**: cumulative active time > 30 s in a 60 s window →
  forced 5 s rest to prevent overheat/stall.
* **Operator modal timeout**: 10 min without a decision defaults to
  CANCEL so an unattended out-of-tolerance lane does not stall the line.

## 7. HMI tab layout

| Tab | Zone | What it does |
|-----|------|--------------|
| **Dashboard** | operator | Recipe picker, batch-weight scaler, order queue, START/PAUSE/STOP row, RESUME + REFRESH row, live 13-station grid, conveyor + mixer status |
| **Inventory** | operator | Container stock levels, refill entry per station, low-stock badges |
| **Prod Log** | operator | Production CSV viewer — newest first, row-count badge, auto-refresh on tab focus and on order completion |
| **Health** | operator | Board connectivity + latency, motor run hours, maintenance alerts |
| **Behavior** | TECH | Per-station tuning: mode, micro threshold, accel, decel, ML toggle, tolerance, fall delay, **stop-early offset**, precision-dispensing knobs (fine margin, inflight comp, max top-up pulses, **auto micro-tail (g)**, pulse-ms LRG/MED/SML/TINY), servo angles, servo safety envelope, per-movement timeout |
| **Recipe Change** | TECH | Recipe CRUD (rename without duplicates, overwrite confirmation, ingredient de-dup, list refresh) |
| **Diagnostic** | TECH | Single-station console + per-station manual motor / dispense / drop / eject test (auto-capped) |

## 8. Tunable defaults (current values)

| Constant | Default | Effect |
|----------|---------|--------|
| `DEFAULT_MICRO_THRESHOLD_G`   | 5.0 g  | Targets ≤ this → pulse-only fill |
| `DEFAULT_AUTO_MICRO_TAIL_G`   | 3.0 g  | Last N grams pulse-dispensed in AUTO mode |
| `MICRO_TAIL_SLOWDOWN_G`       | 1.0 g  | Linear voltage ramp-down window before the tail |
| `DEFAULT_COARSE_MARGIN_G`     | 5.0 g  | (Legacy mode) coarse stop margin |
| `DEFAULT_FINE_MARGIN_G`       | 0.30 g | Fine-fill stop margin |
| `DEFAULT_INFLIGHT_COMP_G`     | 0.15 g | Static in-flight gravity comp |
| `DEFAULT_MAX_TOPUP_PULSES`    | 30     | Pulse budget for the top-up loop |
| `TOPUP_RESCUE_PULSES`         | 8      | Extra stronger pulses after top-up |
| `TOPUP_RESCUE_VOLTAGE`        | 3.4 V  | Voltage for rescue pulses |
| `DEFAULT_PULSE_MS_LARGE`      | 110 ms | Gap > 2 g pulse duration |
| `DEFAULT_PULSE_MS_MEDIUM`     | 60 ms  | Gap > 0.8 g pulse duration |
| `DEFAULT_PULSE_MS_SMALL`      | 35 ms  | Gap > 0.3 g pulse duration |
| `DEFAULT_PULSE_MS_TINY`       | 20 ms  | Gap ≤ 0.3 g pulse duration |
| `DEFAULT_SCALE_TOLERANCE_G`   | 3.0 g  | Per-lane drop tolerance band |
| `DEFAULT_FALL_DELAY_S`        | 2.0 s  | Post-cutoff settle before stability read |
| `DEFAULT_TARGET_OFFSET_G`     | 0.0 g  | "Stop early" amount (positive = stop short) |
| `MAX_JUMP_G`                  | 25 g   | Scale spike threshold |
| `MED_BUF_LEN`                 | 5      | Median-filter buffer length |
| `MIXER_DURATION_S`            | 60 s   | Mixer time after last valid drop |
| `OPERATOR_DECISION_TIMEOUT_S` | 600 s  | Out-of-tol modal auto-CANCEL window |
| `TECH_PIN`                    | `2350` | Technician PIN |

## 9. Files in this project

```
teamatrix_pro.py            Tkinter HMI (entry point)
teamatrix_backend.py        Hardware coordinator, station logic, orchestrator
teamatrix_config.py         (legacy) standalone CSV helpers
teamatrix_hardware.py       (legacy) earlier hardware skeleton
code.py                     Motion Pro firmware
hx711_gpio.py               HX711 driver
99-tea-lanes.rules          udev rules for stable /dev/module_0X symlinks
station_config.json         Live per-station behavior (auto-saved)
recipes.json                Live recipe library (auto-saved)
inventory.json              Live container stock (auto-saved)
ingredients_catalogue.json  Available ingredient names
production_log.csv          Per-ingredient order log
refill_log.csv              Refill events
README.md                   Hardware + wiring + setup
ProjectIdea.md              THIS FILE — high-level project vision
update.md                   Older change-history notes
To scale.md                 Calibration notes
```

## 10. Running the system

```bash
# Install deps once
sudo apt install -y python3-serial python3-tk
sudo usermod -aG dialout $USER     # log out / back in

# Launch
python3 teamatrix_pro.py
```

Tech-mode PIN: **`2350`** (set in `teamatrix_backend.TECH_PIN`). Required
to access the Behavior, Recipe Change, and Diagnostic tabs.

## 11. Roadmap / open ideas

* Per-station auto-calibration sequence (place known weight → solve REF_UNIT).
* "Train auger" mode — pulse N times at different voltages, log gain, fit
  a per-ingredient ML curve, persist it.
* Optional barcode scanner integration for ingredient refill tracking.
* Network export of `production_log.csv` (daily roll-up to a shared
  Google Sheet / email).
* Multi-recipe queue scheduling with re-ordering by available stock.
