"""
app/engine/__init__.py — OS engine package (Phase 4).

Sub-modules:
  scheduler.py      — FCFS, Priority, Deadline scheduling policies.
  worker.py         — Worker thread class and lifecycle.
  worker_manager.py — Thread pool management and crash recovery.
  resource_manager.py — Semaphores, resource-allocation graph.
  task_handlers.py  — Business logic executed per task type.
  metrics.py        — Scheduling metric collection and reporting.

Nothing in this package is imported at app startup.  The engine is started
explicitly via `flask engine start`.  This prevents accidental thread creation
during migrations, tests, and development-server reloads.
"""
