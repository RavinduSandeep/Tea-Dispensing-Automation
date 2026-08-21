"""
hx711_gpio.py  —  Bit-bang HX711 driver for CircuitPython
==========================================================
Designed for Cytron Motion 2350 Pro (RP2350)
Compatible with CircuitPython 10+

Usage:
    from hx711_gpio import HX711
    hx = HX711(clk_pin, dat_pin, gain=128)
    hx.tare(times=10)
    weight = hx.get_units(3)   # grams, 3-sample average
"""

import time
import digitalio


class HX711:
    """
    Bit-bang HX711 24-bit ADC driver.

    Gain / channel selection:
        128  →  Channel A, gain 128  (1 extra pulse)   ← default, recommended
         64  →  Channel A, gain  64  (3 extra pulses)
         32  →  Channel B, gain  32  (2 extra pulses)
    """

    # Minimum high/low pulse width = 0.1 µs per HX711 datasheet.
    # 1 µs used here for RP2350 safety margin & CircuitPython overhead.
    _CLK_HALF_PERIOD = 0.000001  # 1 µs

    def __init__(self, pd_sck, dout, gain=128):
        """
        pd_sck : board pin for PD_SCK (clock)
        dout   : board pin for DOUT  (data output from HX711)
        gain   : 128 (default), 64, or 32
        """
        self._sck = digitalio.DigitalInOut(pd_sck)
        self._sck.direction = digitalio.Direction.OUTPUT
        self._sck.value = False          # Clock idles LOW

        self._dat = digitalio.DigitalInOut(dout)
        self._dat.direction = digitalio.Direction.INPUT
        self._dat.pull = digitalio.Pull.UP  # HX711 DOUT is open-drain

        self.OFFSET = 0
        self.SCALE  = 1
        self._gain_pulses = 1            # Default: channel A, gain 128
        self.set_gain(gain)

    # ------------------------------------------------------------------
    # Gain / channel selection
    # ------------------------------------------------------------------
    def set_gain(self, gain: int):
        """Set gain (128, 64, or 32).  Issues a dummy read to latch gain."""
        if   gain == 128: self._gain_pulses = 1
        elif gain ==  64: self._gain_pulses = 3
        elif gain ==  32: self._gain_pulses = 2
        else:
            raise ValueError(f"Unsupported gain: {gain}. Use 128, 64, or 32.")
        self._sck.value = False
        self.read()   # dummy read to commit gain setting to HX711

    # ------------------------------------------------------------------
    # Low-level read
    # ------------------------------------------------------------------
    def _pulse_clock(self):
        """Issue one SCK pulse and return the DOUT bit sampled on rising edge."""
        self._sck.value = True
        time.sleep(self._CLK_HALF_PERIOD)
        bit = self._dat.value
        self._sck.value = False
        time.sleep(self._CLK_HALF_PERIOD)
        return bit

    def is_ready(self) -> bool:
        """HX711 signals ready by pulling DOUT LOW."""
        return self._dat.value == False

    def read(self, timeout_s: float = 0.5) -> "int | None":
        """
        Perform one 24-bit read.  Returns raw 2's-complement signed integer,
        or None if the HX711 does not become ready within *timeout_s* seconds.
        """
        # ---- Wait for DOUT to go LOW (conversion complete) ----
        deadline = time.monotonic() + timeout_s
        while not self.is_ready():
            if time.monotonic() > deadline:
                return None            # sensor not responding
            # Yield to CircuitPython scheduler briefly
            time.sleep(0.001)

        # ---- Clock in 24 data bits (MSB first) ----
        raw = 0
        for _ in range(24):
            raw = (raw << 1) | self._pulse_clock()

        # ---- Extra pulses to set gain for NEXT conversion ----
        for _ in range(self._gain_pulses):
            self._pulse_clock()

        # ---- 2's complement sign extension ----
        if raw & 0x800000:
            raw -= 0x1000000

        return raw

    # ------------------------------------------------------------------
    # Averaging helpers
    # ------------------------------------------------------------------
    def read_average(self, times: int = 5) -> "float | None":
        """
        Return the mean of *times* valid raw readings.
        Outlier rejection: discards min and max when times >= 5.
        Returns None if fewer than 2 valid readings are obtained.
        """
        samples = []
        for _ in range(times):
            v = self.read()
            if v is not None:
                samples.append(v)

        if len(samples) < 2:
            return None

        # Drop min/max outliers when we have enough samples
        if len(samples) >= 5:
            samples.remove(max(samples))
            samples.remove(min(samples))

        return sum(samples) / len(samples)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def tare(self, times: int = 10):
        """
        Zero-out the scale by recording the current offset.
        *times* averaged reads are used for stability.
        """
        raw = self.read_average(times)
        if raw is not None:
            self.OFFSET = raw

    def set_scale(self, scale: float):
        """
        Set the calibration factor (REF_UNIT).
        Typical range: 1000–60000 depending on load cell.
        Calculate via:  scale = raw_reading_with_known_weight / known_weight_in_grams
        """
        if scale == 0:
            raise ValueError("Scale factor must not be zero.")
        self.SCALE = scale

    def get_units(self, times: int = 1) -> "float | None":
        """
        Return weight in calibrated units (grams if REF_UNIT set correctly).
        *times* = 1 for fastest read (25 Hz polling).
        *times* >= 3 for smoother GUI display.
        """
        if times == 1:
            raw = self.read()
            if raw is None:
                return None
            return (raw - self.OFFSET) / self.SCALE

        raw_avg = self.read_average(times)
        if raw_avg is None:
            return None
        return (raw_avg - self.OFFSET) / self.SCALE

    def power_down(self):
        """Put HX711 into power-down mode (SCK held HIGH > 60 µs)."""
        self._sck.value = False
        self._sck.value = True
        time.sleep(0.0001)

    def power_up(self):
        """Wake HX711 from power-down and reset gain."""
        self._sck.value = False
        time.sleep(0.001)
        self.set_gain(128)   # re-latch gain
