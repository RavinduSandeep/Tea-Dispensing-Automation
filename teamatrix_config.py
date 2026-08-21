import os
import csv
from datetime import datetime

# Hardware mappings
STATIONS = {
    1: {"name": "Strathspey BOPF", "board": "Board 1", "channel": 1},
    2: {"name": "Laxapana Peko", "board": "Board 1", "channel": 2},
    3: {"name": "Moray BOP", "board": "Board 1", "channel": 3},
    4: {"name": "Silver Tips", "board": "Board 1", "channel": 4},
    5: {"name": "Golden Tips", "board": "Board 2", "channel": 1},
    6: {"name": "Cinnamon chips", "board": "Board 2", "channel": 2},
    7: {"name": "Ginger", "board": "Board 2", "channel": 3},
    8: {"name": "Orange peel", "board": "Board 2", "channel": 4},
    9: {"name": "Lemon peel", "board": "Board 3", "channel": 1},
    10: {"name": "Lemongrass", "board": "Board 3", "channel": 2},
    11: {"name": "Rose petals", "board": "Board 3", "channel": 3},
    12: {"name": "Jasmine petals", "board": "Board 3", "channel": 4},
    13: {"name": "Bergamot", "board": "Board 4", "channel": 1},
}

CONVEYOR_CONFIG = {"board": "Board 4", "motor": 2} # Motor 14 mapping
MIXER_CONFIG = {"board": "Board 4", "motor": 3}    # Motor 15 mapping

LOG_FILE = "production_log.csv"

def get_next_order_id():
    """Reads production_log.csv to find the last ID and increments it."""
    if not os.path.exists(LOG_FILE):
        return 1001
    
    last_id = 1000
    try:
        with open(LOG_FILE, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    last_id = max(last_id, int(row.get("OrderID", 1000)))
                except ValueError:
                    pass
    except Exception:
        pass
    return last_id + 1

def log_order(order_id, data):
    """
    Saves Timestamp, OrderID, IngredientName, TargetWeight, ActualWeight
    data is a list of dicts: {"name": str, "target": float, "actual": float}
    """
    timestamp = datetime.now().isoformat()
    needs_header = not os.path.exists(LOG_FILE)
    
    with open(LOG_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        if needs_header:
            writer.writerow(["Timestamp", "OrderID", "IngredientName", "TargetWeight", "ActualWeight"])
        for item in data:
            writer.writerow([
                timestamp, 
                order_id, 
                item["name"], 
                f"{item['target']:.2f}", 
                f"{item['actual']:.2f}"
            ])
        f.flush() # Ensure it's saved immediately
