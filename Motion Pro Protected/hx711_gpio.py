import time
import digitalio

class HX711:
    def __init__(self, pd_sck, dout, gain=128):
        self.p_sck = digitalio.DigitalInOut(pd_sck)
        self.p_sck.direction = digitalio.Direction.OUTPUT
        self.p_out = digitalio.DigitalInOut(dout)
        self.p_out.direction = digitalio.Direction.INPUT
        
        # [CRITICAL] Ensure Clock starts LOW
        self.p_sck.value = False
        self.GAIN = 0
        self.OFFSET = 0
        self.SCALE = 1
        self.set_gain(gain)

    def set_gain(self, gain):
        if gain == 128: self.GAIN = 1
        elif gain == 64: self.GAIN = 3
        elif gain == 32: self.GAIN = 2
        # Reset pulse
        self.p_sck.value = False
        self.read()

    def is_ready(self):
        return self.p_out.value == False

    def read(self):
        # 1. Wait for Ready (Timeout after 0.5s to prevent freezing)
        start = time.monotonic()
        while self.p_out.value:
            if (time.monotonic() - start) > 0.5:
                return None
            pass
            
        # 2. Read 24 bits
        data = 0
        for _ in range(24):
            self.p_sck.value = True
            time.sleep(0.000001) # Tiny delay for stability
            self.p_sck.value = False
            time.sleep(0.000001)
            data = (data << 1) | self.p_out.value
            
        # 3. Set Gain (Pulses 1, 2, or 3)
        for _ in range(self.GAIN):
            self.p_sck.value = True
            time.sleep(0.000001)
            self.p_sck.value = False
            time.sleep(0.000001)
            
        # 4. Convert 2's Complement
        if data & 0x800000:
            data -= 0x1000000
        return data

    def get_units(self, times=1):
        if times == 1:
            val = self.read()
            if val is None: return None
            return (val - self.OFFSET) / self.SCALE
            
        total = 0
        valid = 0
        for _ in range(times):
            val = self.read()
            if val is not None:
                total += val
                valid += 1
        if valid == 0: return None
        return ((total / valid) - self.OFFSET) / self.SCALE

    def tare(self, times=10):
        total = 0
        valid = 0
        for _ in range(times):
            val = self.read()
            if val is not None:
                total += val
                valid += 1
        if valid > 0:
            self.OFFSET = total / valid

    def set_scale(self, scale):
        self.SCALE = scale