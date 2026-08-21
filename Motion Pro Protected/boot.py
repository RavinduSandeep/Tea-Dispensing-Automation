# boot.py — Cytron Motion 2350 Pro
import supervisor
import microcontroller

# ── CHANGE THIS NUMBER PER BOARD (1, 2, 3, or 4) ──
BOARD_NUMBER = 1
# ──────────────────────────────────────────────────

microcontroller.nvm[0] = BOARD_NUMBER
supervisor.set_usb_identification(
    manufacturer="Cytron",
    product=f"Tea Matrix Worker {BOARD_NUMBER:02d}",
)

# CRITICAL: prevents protocol breaks during serial comms
supervisor.runtime.autoreload = False