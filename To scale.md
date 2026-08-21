To scale your **TeaMatrix v2.0** to handle 13 specific ingredients plus automated conveyor and mixer control, you need a precise sequence of operations. This requires the software to coordinate 4 separate hardware controllers and a master timing logic.

Below are the success instructions, the optimized prompt for Antigravity, and a refined `README.md`.

---

### 1. Instructions for Success with Antigravity

* **Define the GPIO Expansion:** Specify that Board 4 (Stations 13–16) will handle the last ingredient (Station 13), the **Conveyor Motor** (Station 14), and the **Mixer Motor** (Station 15).
* **Sequential Logic:** The software must follow a strict "State Machine" logic: **Dispense** $\rightarrow$ **Verify Weights** $\rightarrow$ **Activate Conveyor** $\rightarrow$ **Drop Servos** $\rightarrow$ **Activate Mixer**.
* **Serial Heartbeat:** Ensure the AI generates a "Watchdog Packet" sent every 500ms to keep the Raspberry Pi 5 and the Cytron boards synchronized.
* **Threaded Polling:** Request that each of the 4 serial ports runs in its own background thread to prevent UI lag while monitoring 13 scales simultaneously.

---

### 2. The Perfect Prompt for Antigravity

**Copy and paste this into your AI coding tool:**

> **System Architect Task: TeaMatrix Industrial Blending Console (16-Channel)**
>
> **Objective:** Build a Python Tkinter GUI for Raspberry Pi 5 to control a 13-ingredient tea blending system with an integrated Conveyor and Mixer.
>
> **Hardware Mapping (4x Cytron Motion 2350 Pro):**
> * **Board 1 (/dev/ttyACM0):** Stations 1–4 (Strathspey BOPF, Laxapana Peko, Moray BOP, Silver Tips).
> * **Board 2 (/dev/ttyACM1):** Stations 5–8 (Golden Tips, Cinnamon chips, Ginger, Orange peel).
> * **Board 3 (/dev/ttyACM2):** Stations 9–12 (Lemon peel, Lemongrass, Rose petals, Jasmine petals).
> * **Board 4 (/dev/ttyACM3):** >    * **Station 13:** Bergamot.
>    * **Motor 14 (GP10):** Conveyor Motor control.
>    * **Motor 15 (GP13):** Mixer Motor control.
>
> **Operational Workflow (The "Dispatch" Sequence):**
> 1. **Fill Phase:** Activate DC motors for all ingredients in the recipe until target weights are reached.
> 2. **Check Phase:** Wait for all 13 scales to settle and verify within ±0.2g tolerance.
> 3. **Conveyor Start:** Activate Conveyor Motor (Board 4, Motor 14).
> 4. **Drop Phase:** Execute Servo sequence (0° to 70°) for all active ingredient stations.
> 5. **Mixing Phase:** Once drop is complete, activate Mixer Motor (Board 4, Motor 15) for a set duration (e.g., 30 seconds).
>
> **UI Requirements:**
> * **Dashboard:** 4x4 Grid. 13 Ingredient cards + 1 Conveyor Status + 1 Mixer Status.
> * **Real-time visibility:** Live weight updates at 25Hz using the `W:ALL` serial command.
> * **Manual Overrides:** Individual TARE, START, STOP, and EJECT (Servo -70°) buttons for every station.
> * **Data Management:** Auto-incrementing Order IDs saved to `production_log.csv` with precise weights and timestamps.
>
> **Safety:**
> * **Global E-STOP:** Immediately stops all motors (1-16) across all 4 boards.
> * **Watchdog:** Continuous serial heartbeat to prevent hardware freezing.

---

### 3. README.md Content

```markdown
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
```

---

### 4. Implementation Guidance for the Conveyor/Mixer

1.  **Motor Channels:** Since your `code.py` defines motors on `GP8, GP10, GP13, GP15`, you should map the Conveyor to `GP10` and the Mixer to `GP13` on the 4th board.
2.  **Order ID:** In Python, initialize your ID by checking the last entry of your `production_log.csv`. 
    * *Tip:* If the file doesn't exist, start at `ID: 1001`.
3.  **UI Feedback:** Ensure the "Conveyor" and "Mixer" status badges in the UI turn **GREEN** when running so the operator knows the system is in the "Moving/Mixing" phase and not just stuck.

Do you have a specific time duration in mind for the mixer to run after the tea has been dropped?