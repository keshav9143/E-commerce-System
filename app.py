from flask import Flask, request, jsonify
from datetime import datetime
from threading import Lock

app = Flask(__name__)

# Temporary in-memory storage. Jobs will be lost when Flask restarts.
processes = []
process_lock = Lock()
next_process_id = 1

VALID_ALGORITHMS = {"FCFS", "SJF", "PRIORITY"}


@app.route("/", methods=["GET"])
def home():
    return "CartOS is running!"


# -------------------- ORDER PROCESSING + PCB --------------------

@app.route("/api/processes", methods=["POST"])
def create_process():
    """Create an OS-style job/PCB record for an existing e-commerce order."""
    global next_process_id

    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    burst_time = data.get("burst_time", 5)
    priority = data.get("priority", 3)

    if order_id is None:
        return jsonify({"error": "order_id is required"}), 400

    try:
        order_id = int(order_id)
        burst_time = int(burst_time)
        priority = int(priority)
    except (ValueError, TypeError):
        return jsonify({
            "error": "order_id, burst_time, and priority must be integers"
        }), 400

    if order_id <= 0:
        return jsonify({"error": "order_id must be positive"}), 400
    if burst_time <= 0:
        return jsonify({"error": "burst_time must be positive"}), 400
    if not 1 <= priority <= 5:
        return jsonify({"error": "priority must be from 1 (highest) to 5 (lowest)"}), 400

    with process_lock:
        process_id = f"P{next_process_id:03d}"
        next_process_id += 1

        pcb = {
            "process_id": process_id,
            "order_id": order_id,
            "process_state": "READY",
            "burst_time": burst_time,
            "remaining_time": burst_time,
            "priority": priority,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "waiting_time": 0,
            "turnaround_time": 0
        }
        processes.append(pcb)

    return jsonify({
        "message": "Order-processing job created successfully",
        "pcb": pcb.copy()
    }), 201


@app.route("/api/processes", methods=["GET"])
def get_processes():
    with process_lock:
        return jsonify([p.copy() for p in processes])


@app.route("/api/processes/<process_id>", methods=["GET"])
def get_process(process_id):
    with process_lock:
        for process in processes:
            if process["process_id"] == process_id:
                return jsonify(process.copy())
    return jsonify({"error": "Process not found"}), 404


# -------------------- CPU SCHEDULING --------------------

def calculate_schedule(jobs, algorithm):
    """Return a non-preemptive schedule without modifying stored PCB records."""
    if algorithm not in VALID_ALGORITHMS:
        raise ValueError("algorithm must be FCFS, SJF, or PRIORITY")

    jobs = [job.copy() for job in jobs]

    if algorithm == "FCFS":
        jobs.sort(key=lambda p: (p["created_at"], p["process_id"]))
    elif algorithm == "SJF":
        jobs.sort(key=lambda p: (
            p["burst_time"], p["created_at"], p["process_id"]
        ))
    else:  # PRIORITY: lower number means higher priority
        jobs.sort(key=lambda p: (
            p["priority"], p["created_at"], p["process_id"]
        ))

    elapsed_time = 0
    schedule = []

    for job in jobs:
        waiting_time = elapsed_time
        turnaround_time = waiting_time + job["burst_time"]

        schedule.append({
            "process_id": job["process_id"],
            "order_id": job["order_id"],
            "burst_time": job["burst_time"],
            "priority": job["priority"],
            "start_time": elapsed_time,
            "finish_time": turnaround_time,
            "waiting_time": waiting_time,
            "turnaround_time": turnaround_time
        })
        elapsed_time = turnaround_time

    count = len(schedule)
    average_waiting = (
        sum(item["waiting_time"] for item in schedule) / count if count else 0
    )
    average_turnaround = (
        sum(item["turnaround_time"] for item in schedule) / count if count else 0
    )

    return {
        "algorithm": algorithm,
        "jobs_scheduled": count,
        "average_waiting_time": round(average_waiting, 2),
        "average_turnaround_time": round(average_turnaround, 2),
        "schedule": schedule,
        "note": "Simulation only: stored process states are not changed."
    }


@app.route("/api/scheduler/run", methods=["POST"])
def run_scheduler():
    data = request.get_json(silent=True) or {}
    algorithm = str(data.get("algorithm", "FCFS")).upper()

    if algorithm not in VALID_ALGORITHMS:
        return jsonify({
            "error": "algorithm must be FCFS, SJF, or PRIORITY"
        }), 400

    with process_lock:
        ready_jobs = [
            p.copy() for p in processes
            if p["process_state"] == "READY"
        ]

    return jsonify(calculate_schedule(ready_jobs, algorithm))


@app.route("/api/scheduler/compare", methods=["GET"])
def compare_schedulers():
    """Compare all three algorithms on the same READY jobs."""
    with process_lock:
        ready_jobs = [
            p.copy() for p in processes
            if p["process_state"] == "READY"
        ]

    return jsonify({
        "jobs_available": len(ready_jobs),
        "comparisons": [
            calculate_schedule(ready_jobs, algorithm)
            for algorithm in ("FCFS", "SJF", "PRIORITY")
        ]
    })


# -------------------- DEADLOCK DEMONSTRATION --------------------

def find_deadlock_cycles(graph):
    """
    Detect cycles in a wait-for graph.
    Example: {"P1": ["P2"], "P2": ["P1"]}
    An edge A -> B means process A is waiting for process B.
    """
    visiting = set()
    visited = set()
    stack = []
    cycles = []
    seen_cycles = set()

    def dfs(node):
        if node in visiting:
            start = stack.index(node)
            cycle = stack[start:] + [node]
            canonical = tuple(sorted(set(cycle[:-1])))
            if canonical not in seen_cycles:
                seen_cycles.add(canonical)
                cycles.append(cycle)
            return

        if node in visited:
            return

        visiting.add(node)
        stack.append(node)

        for neighbor in graph.get(node, []):
            dfs(neighbor)

        stack.pop()
        visiting.remove(node)
        visited.add(node)

    all_nodes = set(graph.keys())
    for neighbors in graph.values():
        all_nodes.update(neighbors)

    for node in all_nodes:
        if node not in visited:
            dfs(node)

    return cycles


@app.route("/api/deadlocks/detect", methods=["POST"])
def detect_deadlocks():
    data = request.get_json(silent=True) or {}
    graph = data.get("graph")

    if not isinstance(graph, dict):
        return jsonify({
            "error": 'Send JSON with "graph", mapping process IDs to lists of process IDs'
        }), 400

    if any(not isinstance(neighbors, list) for neighbors in graph.values()):
        return jsonify({"error": "Each graph value must be a list"}), 400

    if any(
        not isinstance(node, str)
        for node, neighbors in graph.items()
        for node in [node, *neighbors]
    ):
        return jsonify({"error": "Process IDs in graph must be strings"}), 400

    cycles = find_deadlock_cycles(graph)
    return jsonify({
        "deadlock_detected": len(cycles) > 0,
        "cycles": cycles,
        "note": "This detects cycles in the supplied wait-for graph; it does not lock real inventory resources."
    })



# ---------------- INVENTORY SYNCHRONIZATION ----------------

from threading import Semaphore

# Demo inventory: product ID -> available stock
inventory = {
    1: 1,
    2: 5,
    3: 10
}

# Mutex: only one thread can modify inventory at a time
inventory_mutex = Lock()

# Semaphore: allow at most 2 inventory operations at once
inventory_semaphore = Semaphore(2)


@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    with inventory_mutex:
        return jsonify([
            {"product_id": product_id, "stock": stock}
            for product_id, stock in inventory.items()
        ])


@app.route("/api/inventory/purchase", methods=["POST"])
def purchase_inventory():
    data = request.get_json(silent=True) or {}

    try:
        product_id = int(data["product_id"])
        quantity = int(data["quantity"])
    except (KeyError, TypeError, ValueError):
        return jsonify({
            "error": "product_id and quantity must be integers"
        }), 400

    if quantity <= 0:
        return jsonify({"error": "quantity must be positive"}), 400

    # Semaphore limits concurrent operations.
    with inventory_semaphore:
        # Mutex protects the shared stock data.
        with inventory_mutex:
            if product_id not in inventory:
                return jsonify({"error": "Product not found"}), 404

            available_stock = inventory[product_id]

            if available_stock < quantity:
                return jsonify({
                    "message": "Purchase rejected: insufficient stock",
                    "available_stock": available_stock
                }), 409

            inventory[product_id] -= quantity

            return jsonify({
                "message": "Purchase successful",
                "product_id": product_id,
                "purchased_quantity": quantity,
                "remaining_stock": inventory[product_id]
            }), 200



if __name__ == "__main__":
    app.run(debug=True)
