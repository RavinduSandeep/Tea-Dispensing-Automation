import time
import threading
import serial
import serial.tools.list_ports
from teamatrix_config import STATIONS, CONVEYOR_CONFIG, MIXER_CONFIG, log_order

def calculate_ml_voltage(flow_rate):
    """
    Quadratic ML voltage model for parallel dispensing:
    Voltage = -0.0677 * flow_rate^2 + 1.6538 * flow_rate + 0.5615
    """
    voltage = (-0.0677 * (flow_rate ** 2)) + (1.6538 * flow_rate) + 0.5615
    # Max bound limits for Cytron DC constraints
    return max(0.0, min(voltage, 12.0))

class HardwareCoordinator:
    def __init__(self, ui_update_callback=None):
        self.ports = {}  # "Board X": serial.Serial
        self.threads = []
        self.running = False
        self.lock = threading.Lock()
        
        # Track active weights
        self.weights = {i: 0.0 for i in range(1, 14)}
        self.statuses = {i: "IDLE" for i in range(1, 14)}
        
        self.conveyor_status = "IDLE"
        self.mixer_status = "IDLE"
        
        self.update_callback = ui_update_callback

    def trigger_ui_update(self):
        if self.update_callback:
            self.update_callback()

    def discover_boards(self):
        """Find Cytron boards by reading port names or sending an ID payload query."""
        available_ports = [p.device for p in serial.tools.list_ports.comports() if 'ACM' in p.name or 'USB' in p.name]
        
        for port in available_ports:
            try:
                s = serial.Serial(port, 115200, timeout=1)
                time.sleep(1) # wait for USB auto-reset delay
                s.write(b"ID?\n")
                resp = s.readline().decode('utf-8', errors='ignore').strip()
                
                # Assume board responds something like 'Board 1', 'Board 2' to avoid linux swapping
                if resp.startswith("Board"):
                    board_name = resp
                    self.ports[board_name] = s
                    print(f"[HW] Connected {board_name} on {port}")
                else:
                    s.close()
            except Exception as e:
                print(f"[HW] Error on {port}: {e}")
                
        # Fill missing UI slots just in case Boards aren't connected
        for i in range(1, 5):
            name = f"Board {i}"
            if name not in self.ports:
                print(f"[WARN] {name} not found. Running in headless/test mode for this board.")

    def start(self):
        self.running = True
        self.discover_boards()
        
        for board_name, s in self.ports.items():
            t = threading.Thread(target=self._board_reader, args=(board_name, s), daemon=True)
            self.threads.append(t)
            t.start()
            
        wt = threading.Thread(target=self._watchdog, daemon=True)
        self.threads.append(wt)
        wt.start()

    def stop(self):
        self.running = False
        self.e_stop()
        for s in self.ports.values():
            try:
                s.close()
            except: pass

    def e_stop(self):
        """Immediately stops all motors across all 4 boards via broadcasting X\n."""
        with self.lock:
            for s in self.ports.values():
                try:
                    s.write(b"X\n")
                    s.flush()
                except: pass
        for i in range(1, 14):
            if self.statuses[i] in ["FILLING", "READY"]:
                self.statuses[i] = "E-STOPPED"
        self.conveyor_status = "IDLE"
        self.mixer_status = "IDLE"
        self.trigger_ui_update()

    def send_command(self, board, cmd):
        with self.lock:
            s = self.ports.get(board)
            if s:
                try:
                    s.write(f"{cmd}\n".encode())
                except:
                    pass

    def _watchdog(self):
        """Continuous serial heartbeat every 500ms to prevent the 0.7s hardware timeout."""
        while self.running:
            for board, s in self.ports.items():
                self.send_command(board, "HBT")
            time.sleep(0.5)

    def _board_reader(self, board_name, s):
        """Background thread updating weights at 25Hz using W:ALL."""
        board_idx = int(board_name.split()[1]) # Extracts '1' from 'Board 1'
        
        while self.running:
            try:
                self.send_command(board_name, "W:ALL")
                line = s.readline().decode('utf-8', errors='ignore').strip()
                
                # Expecting format W:w1,w2,w3,w4 (e.g. W:5.1,-0.2,10.0,0.0)
                if line.startswith("W:"):
                    weights_str = line[2:].split(',')
                    if len(weights_str) >= 4:
                        for i in range(4):
                            station_num = (board_idx - 1) * 4 + i + 1
                            if station_num <= 13:
                                with self.lock:
                                    try:
                                        self.weights[station_num] = float(weights_str[i])
                                    except ValueError:
                                        pass
                    self.trigger_ui_update()
                time.sleep(1/25.0) # Maintains ~25Hz rate
            except Exception as e:
                time.sleep(0.5)

    def force_eject(self, station_num):
        board = STATIONS[station_num]["board"]
        chan = STATIONS[station_num]["channel"]
        self.send_command(board, f"SRV:{chan}:-70") # Eject angle backward
        self.statuses[station_num] = "EJECTED"
        self.trigger_ui_update()

    def force_tare(self, station_num):
        board = STATIONS[station_num]["board"]
        chan = STATIONS[station_num]["channel"]
        self.send_command(board, f"TARE:{chan}")

    # ===== AUTOMATION DISPATCH LOOP =====

    def dispatch_recipe(self, recipe, order_id):
        """Coordinates the ML dispensing, conveyor locking, and mixer sequences in background."""
        threading.Thread(target=self._run_dispatch_sequence, args=(recipe, order_id), daemon=True).start()

    def _run_dispatch_sequence(self, recipe, order_id):
        """
        recipe is a dict: { station_num (int) : target_weight_grams (float) }
        Only dispenses for items that have weight > 0
        """
        active_stations = [s for s, w in recipe.items() if w > 0]
        if not active_stations:
            return

        # 1. Fill Phase (Parallel Dispensing)
        for st in active_stations:
            self.statuses[st] = "FILLING"
        self.trigger_ui_update()

        for st, target in recipe.items():
            if target > 0:
                board = STATIONS[st]["board"]
                chan = STATIONS[st]["channel"]
                
                # We could send voltage direct using ML eq, treating target as flow rate for a fixed cycle?
                # or just set target weight. Let's assume standard protocol "DISP:chan:amount:voltage"
                # Using an arbitrary generic flow_rate of 5g/sec for ML eq demo calculation
                calculated_voltage = calculate_ml_voltage(flow_rate=5.0) 
                
                self.send_command(board, f"DISP:{chan}:{target:.2f}:{calculated_voltage:.2f}")

        # 2. Check Phase (Wait until settled within +- 0.2g tolerance)
        settled = False
        timeout_loops = 0
        while not settled and timeout_loops < 60: # 30s timeout
            settled = True
            for st in active_stations:
                actual = self.weights[st]
                target = recipe[st]
                if abs(actual - target) > 0.2:
                    settled = False
            if settled:
                break
            time.sleep(0.5)
            timeout_loops += 1

        for st in active_stations:
            self.statuses[st] = "READY"
        self.trigger_ui_update()
        
        # Log early so we have data if conveyor drops
        log_data = [{"name": STATIONS[st]["name"], "target": recipe[st], "actual": self.weights[st]} for st in active_stations]
        log_order(order_id, log_data)

        # 3. Transfer Phase (Conveyor + Drop)
        self.conveyor_status = "RUNNING"
        self.trigger_ui_update()
        self.send_command(CONVEYOR_CONFIG["board"], f"M:{CONVEYOR_CONFIG['motor']}:ON")
        time.sleep(2) # Give conveyor time to rev up
        
        for st in active_stations:
            board = STATIONS[st]["board"]
            chan = STATIONS[st]["channel"]
            self.send_command(board, f"SRV:{chan}:70") # Drop tea cleanly onto conveyor
            self.statuses[st] = "DROPPED"
        self.trigger_ui_update()
        
        time.sleep(3) # Time for tea to travel
        self.send_command(CONVEYOR_CONFIG["board"], f"M:{CONVEYOR_CONFIG['motor']}:OFF")
        self.conveyor_status = "IDLE"
        self.trigger_ui_update()

        # 4. Mixing Phase
        self.mixer_status = "RUNNING"
        self.trigger_ui_update()
        self.send_command(MIXER_CONFIG["board"], f"M:{MIXER_CONFIG['motor']}:ON")
        time.sleep(30) # 30 second timer
        self.send_command(MIXER_CONFIG["board"], f"M:{MIXER_CONFIG['motor']}:OFF")
        self.mixer_status = "DONE"
        
        for st in active_stations:
            self.statuses[st] = "IDLE"
        self.trigger_ui_update()
