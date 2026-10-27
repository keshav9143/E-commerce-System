"""
app/models.py — CartOS database models (all 13 tables).

Table inventory
───────────────
 1. users                — registered customers and administrators
 2. products             — product catalogue entries
 3. inventory            — stock levels; separated from products so that
                           high-frequency stock updates do not contend with
                           catalogue reads (different row-lock granularity)
 4. cart_items           — persistent shopping-cart lines per user
 5. orders               — confirmed customer orders
 6. order_items          — individual line items within an order (price
                           snapshot at checkout time)
 7. fulfillment_tasks    — application-level job queue; persists the full
                           task lifecycle and execution history in MySQL
 8. workers              — registered worker threads; heartbeat column
                           enables crash detection
 9. task_execution_log   — append-only audit trail of every task-state
                           transition (who changed it, when, and why)
10. resources            — modeled limited resources (packing stations,
                           inspection stations); ordering_priority enforces
                           a consistent acquisition order to prevent deadlock
11. resource_allocations — tracks which task holds or is waiting for each
                           resource; enables resource-allocation graph (RAG)
                           reconstruction
12. task_metrics         — one row per completed task; stores scheduling
                           measurements (wait time, execution time, etc.)
                           per policy for dashboard comparison
13. scheduling_config    — singleton admin-controlled row that governs the
                           active scheduling policy and worker count

Enumerations
────────────
Python Enum subclasses are defined alongside the models they belong to.
Each Enum class is also used by the engine layer to enforce valid transitions
in application code; the MySQL ENUM column enforces them at the database level.

Design notes
────────────
- InnoDB is used for all tables (foreign keys, row-level locking, ACID).
- All timestamps are stored in UTC as naive datetimes.  The application
  converts to the user's timezone at display time (Phase 5).
- `FulfillmentTask.idempotency_key` prevents duplicate business effects when
  a task is retried (e.g., two NOTIFY_CUSTOMER executions for the same order).
- `FulfillmentTask.last_heartbeat` allows the recovery cron to detect tasks
  stuck in RUNNING state because their worker crashed.
- Inventory uses `reserved_quantity` rather than immediately decrementing
  `quantity` at checkout.  Full deduction happens in the INVENTORY_RESERVE
  task.  This two-phase approach lets the checkout transaction succeed quickly
  while reserving capacity, and simplifies cancellation (just decrement
  reserved_quantity).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from flask_login import UserMixin

from app.extensions import db, login_manager


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    """Return the current UTC time as a naive datetime (MySQL DATETIME)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    """Roles control which views and API endpoints a user may access."""
    CUSTOMER = "customer"
    ADMIN = "admin"


class OrderStatus(str, enum.Enum):
    """
    Order status progression:
      PENDING → CONFIRMED → PROCESSING → SHIPPED → DELIVERED
                          ↘ CANCELLED
      Any terminal state → REFUNDED (manual admin action)
    """
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class TaskType(str, enum.Enum):
    """
    The six task types generated for every order.  They are created in this
    order; a task depends on its predecessor completing successfully.

      PAYMENT_VERIFY    → Validate that payment is authorised.
      INVENTORY_RESERVE → Deduct reserved stock from physical inventory.
      PACK_ORDER        → Simulate physical packing; holds a packing-station
                          resource (demonstrates semaphore).
      QUALITY_CHECK     → Simulate inspection; holds an inspection-station
                          resource (enables deadlock scenario with PACK_ORDER).
      SHIP_ORDER        → Generate a shipment record.
      NOTIFY_CUSTOMER   → Create a notification record (idempotent via
                          idempotency_key).
    """
    PAYMENT_VERIFY = "PAYMENT_VERIFY"
    INVENTORY_RESERVE = "INVENTORY_RESERVE"
    PACK_ORDER = "PACK_ORDER"
    QUALITY_CHECK = "QUALITY_CHECK"
    SHIP_ORDER = "SHIP_ORDER"
    NOTIFY_CUSTOMER = "NOTIFY_CUSTOMER"


class TaskStatus(str, enum.Enum):
    """
    Task lifecycle states.

    Valid transitions (enforced by the engine, not MySQL):

      (start)  →  CREATED     Created inside the checkout transaction.
      CREATED  →  READY       Scheduler confirms all prerequisites are done.
      READY    →  RUNNING     A worker claims the task atomically via
                              SELECT … FOR UPDATE SKIP LOCKED.
      RUNNING  →  BLOCKED     Worker attempted to acquire a resource semaphore
                              and timed out; it releases any held resources and
                              re-enqueues the task.  BLOCKED is never set from
                              outside the worker that owns the task.
      BLOCKED  →  READY       The resource manager signals that capacity is
                              available; the task is eligible again.
      RUNNING  →  COMPLETED   Handler returned without raising an exception.
      RUNNING  →  FAILED      Handler raised an exception.
      FAILED   →  READY       Recovery cron resets the task for a retry when
                              retry_count < max_retries.
      FAILED   →  (terminal)  Max retries exhausted; task stays FAILED.

    Crash recovery:
      A background recovery scan runs every ENGINE_HEARTBEAT_TIMEOUT seconds.
      Any task in RUNNING whose last_heartbeat is older than
      ENGINE_HEARTBEAT_TIMEOUT is assumed abandoned by a crashed worker.
      The recovery scan transitions it to FAILED and increments retry_count.
    """
    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class WorkerStatus(str, enum.Enum):
    """
    Worker lifecycle states.

      IDLE     — Registered, waiting for tasks.
      BUSY     — Currently executing a task.
      STOPPED  — Cleanly shut down via WorkerManager.stop().
      CRASHED  — Detected by heartbeat timeout; recovery scan resets its tasks.
    """
    IDLE = "IDLE"
    BUSY = "BUSY"
    STOPPED = "STOPPED"
    CRASHED = "CRASHED"


class AllocationState(str, enum.Enum):
    """
    State of a resource allocation record.

      REQUESTED — Task has requested the resource but has not yet acquired it.
      HOLDING   — Task holds the resource (semaphore acquired).
      RELEASED  — Task has released the resource (semaphore released).

    The combination of REQUESTED and HOLDING records across tasks can be used
    to reconstruct the resource-allocation graph (RAG) for deadlock analysis.
    """
    REQUESTED = "REQUESTED"
    HOLDING = "HOLDING"
    RELEASED = "RELEASED"


class SchedulingPolicy(str, enum.Enum):
    """
    Available scheduling policies (implemented in app/engine/scheduler.py).

      FCFS     — First Come, First Served.  Tasks are selected by arrival_time
                 ascending.  Simple, fair, no starvation, but ignores urgency.
      PRIORITY — Highest-priority task first; ties broken by arrival_time.
                 Can cause starvation of low-priority tasks in a busy queue.
      DEADLINE — Earliest deadline first (EDF).  Tasks without a deadline are
                 treated as having an infinite deadline (lowest priority).
                 Optimal for minimising missed deadlines in a preemptive system;
                 in our non-preemptive implementation it is near-optimal.

    All three policies are non-preemptive: once a worker starts a task, it runs
    to completion (or failure).  Preemption would require checkpointing, which
    is out of scope for this prototype.
    """
    FCFS = "FCFS"
    PRIORITY = "PRIORITY"
    DEADLINE = "DEADLINE"


# ─────────────────────────────────────────────────────────────────────────────
# Table 1 — users
# ─────────────────────────────────────────────────────────────────────────────

class User(db.Model, UserMixin):
    """
    Registered users.  UserMixin provides the four attributes required by
    Flask-Login: is_authenticated, is_active, is_anonymous, get_id().
    """
    __tablename__ = "users"

    id: int = db.Column(db.Integer, primary_key=True)
    username: str = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email: str = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash: str = db.Column(db.String(128), nullable=False)
    role: str = db.Column(
        db.Enum(UserRole, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=UserRole.CUSTOMER.value,
    )
    is_active: bool = db.Column(db.Boolean, nullable=False, default=True)
    created_at: datetime = db.Column(db.DateTime, nullable=False, default=_now)
    updated_at: datetime = db.Column(
        db.DateTime, nullable=False, default=_now, onupdate=_now
    )

    # Relationships
    orders = db.relationship("Order", back_populates="user", lazy="dynamic")
    cart_items = db.relationship(
        "CartItem", back_populates="user", lazy="dynamic", cascade="all, delete-orphan"
    )

    def set_password(self, plain: str, bcrypt_instance) -> None:
        """Hash *plain* and store it.  Never store the plain-text password."""
        self.password_hash = bcrypt_instance.generate_password_hash(plain).decode("utf-8")

    def check_password(self, plain: str, bcrypt_instance) -> bool:
        """Return True if *plain* matches the stored hash."""
        return bcrypt_instance.check_password_hash(self.password_hash, plain)

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role}>"


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    """
    Flask-Login callback: given a user ID stored in the session cookie,
    return the corresponding User object (or None if not found).
    """
    return db.session.get(User, int(user_id))


# ─────────────────────────────────────────────────────────────────────────────
# Table 2 — products
# ─────────────────────────────────────────────────────────────────────────────

class Product(db.Model):
    """Product catalogue entries.  Price and description are managed here."""
    __tablename__ = "products"

    id: int = db.Column(db.Integer, primary_key=True)
    name: str = db.Column(db.String(200), nullable=False)
    description: str = db.Column(db.Text, nullable=True)
    # DECIMAL(10,2): supports prices up to 99,999,999.99
    price = db.Column(db.Numeric(10, 2), nullable=False)
    category: str = db.Column(db.String(100), nullable=True, index=True)
    image_url: str = db.Column(db.String(500), nullable=True)
    # SKU (Stock Keeping Unit): unique product code for catalogue management.
    sku: str = db.Column(db.String(100), unique=True, nullable=True)
    is_active: bool = db.Column(db.Boolean, nullable=False, default=True)
    created_at: datetime = db.Column(db.DateTime, nullable=False, default=_now)
    updated_at: datetime = db.Column(
        db.DateTime, nullable=False, default=_now, onupdate=_now
    )

    # Relationships
    inventory = db.relationship(
        "Inventory", back_populates="product", uselist=False, cascade="all, delete-orphan"
    )
    cart_items = db.relationship("CartItem", back_populates="product", lazy="dynamic")
    order_items = db.relationship("OrderItem", back_populates="product", lazy="dynamic")

    @property
    def available_stock(self) -> int:
        """Units currently available for purchase."""
        return self.inventory.available_quantity if self.inventory else 0

    def __repr__(self) -> str:
        return f"<Product id={self.id} name={self.name!r} price={self.price}>"


# ─────────────────────────────────────────────────────────────────────────────
# Table 3 — inventory
# ─────────────────────────────────────────────────────────────────────────────

class Inventory(db.Model):
    """
    Stock levels for one product.

    quantity            — total units physically in stock.
    reserved_quantity   — units reserved by confirmed but not-yet-fulfilled
                          orders.  available = quantity - reserved_quantity.

    Why separate from Product?
    At checkout, this row is locked with SELECT … FOR UPDATE (row-level lock).
    Because `inventory` is a separate table, the lock affects only stock data,
    not the product catalogue — other users can still read product details
    concurrently.

    Deterministic locking:
    When a checkout cart contains multiple products, inventory rows are always
    locked in ascending product_id order to prevent deadlock between concurrent
    checkouts (consistent lock ordering breaks the circular-wait condition).
    """
    __tablename__ = "inventory"

    id: int = db.Column(db.Integer, primary_key=True)
    product_id: int = db.Column(
        db.Integer, db.ForeignKey("products.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True,
    )
    quantity: int = db.Column(db.Integer, nullable=False, default=0)
    reserved_quantity: int = db.Column(db.Integer, nullable=False, default=0)
    updated_at: datetime = db.Column(
        db.DateTime, nullable=False, default=_now, onupdate=_now
    )

    # Relationships
    product = db.relationship("Product", back_populates="inventory")

    @property
    def available_quantity(self) -> int:
        """Units available for new orders (computed, not stored)."""
        return max(0, self.quantity - self.reserved_quantity)

    __table_args__ = (
        # Ensure stock cannot go negative at the database level.
        db.CheckConstraint("quantity >= 0", name="ck_inventory_quantity_non_negative"),
        db.CheckConstraint(
            "reserved_quantity >= 0", name="ck_inventory_reserved_non_negative"
        ),
        db.CheckConstraint(
            "quantity >= reserved_quantity",
            name="ck_inventory_reserved_le_quantity",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Inventory product_id={self.product_id} "
            f"qty={self.quantity} reserved={self.reserved_quantity}>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Table 4 — cart_items
# ─────────────────────────────────────────────────────────────────────────────

class CartItem(db.Model):
    """
    Persistent shopping-cart line items.

    One row per (user, product) pair.  Adding the same product again
    increments the quantity rather than creating a second row (enforced by
    the unique constraint and upsert logic in the shop blueprint).
    """
    __tablename__ = "cart_items"

    id: int = db.Column(db.Integer, primary_key=True)
    user_id: int = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: int = db.Column(
        db.Integer, db.ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    quantity: int = db.Column(db.Integer, nullable=False, default=1)
    added_at: datetime = db.Column(db.DateTime, nullable=False, default=_now)

    # Relationships
    user = db.relationship("User", back_populates="cart_items")
    product = db.relationship("Product", back_populates="cart_items")

    __table_args__ = (
        # A user can have at most one cart-item row per product.
        db.UniqueConstraint("user_id", "product_id", name="uq_cart_user_product"),
        db.CheckConstraint("quantity > 0", name="ck_cart_quantity_positive"),
    )

    @property
    def line_total(self):
        """Calculate line total using server-side Decimal price."""
        from decimal import Decimal
        if self.product and self.product.price is not None:
            return Decimal(str(self.product.price)) * Decimal(str(self.quantity))
        return Decimal("0.00")

    @property
    def available_stock(self) -> int:
        """Available stock for the product in this cart line."""
        return self.product.available_stock if self.product else 0

    @property
    def is_in_stock(self) -> bool:
        """Check if requested quantity is currently within available stock."""
        return self.product.is_active and (self.available_stock >= self.quantity)

    def __repr__(self) -> str:
        return f"<CartItem user={self.user_id} product={self.product_id} qty={self.quantity}>"


# ─────────────────────────────────────────────────────────────────────────────
# Table 5 — orders
# ─────────────────────────────────────────────────────────────────────────────

class Order(db.Model):
    """Confirmed customer orders.  Created inside the checkout transaction."""
    __tablename__ = "orders"

    id: int = db.Column(db.Integer, primary_key=True)
    user_id: int = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)
    status: str = db.Column(
        db.Enum(OrderStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=OrderStatus.PENDING.value,
        index=True,
    )
    notes: str = db.Column(db.Text, nullable=True)

    # Shipping / delivery address snapshot (captured at checkout)
    shipping_name: str | None = db.Column(db.String(100), nullable=True)
    shipping_email: str | None = db.Column(db.String(254), nullable=True)
    shipping_phone: str | None = db.Column(db.String(20), nullable=True)
    shipping_address_line1: str | None = db.Column(db.String(200), nullable=True)
    shipping_address_line2: str | None = db.Column(db.String(200), nullable=True)
    shipping_city: str | None = db.Column(db.String(100), nullable=True)
    shipping_state: str | None = db.Column(db.String(100), nullable=True)
    shipping_pincode: str | None = db.Column(db.String(10), nullable=True)

    created_at: datetime = db.Column(db.DateTime, nullable=False, default=_now, index=True)
    updated_at: datetime = db.Column(
        db.DateTime, nullable=False, default=_now, onupdate=_now
    )

    # Relationships
    user = db.relationship("User", back_populates="orders")
    items = db.relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan"
    )
    tasks = db.relationship(
        "FulfillmentTask", back_populates="order", lazy="dynamic"
    )

    def __repr__(self) -> str:
        return f"<Order id={self.id} user={self.user_id} status={self.status} total={self.total_amount}>"


# ─────────────────────────────────────────────────────────────────────────────
# Table 6 — order_items
# ─────────────────────────────────────────────────────────────────────────────

class OrderItem(db.Model):
    """
    Individual line items within an order.

    unit_price is a snapshot of the product price at the moment of checkout.
    Storing it here means that future product price changes do not alter
    historical order totals.
    """
    __tablename__ = "order_items"

    id: int = db.Column(db.Integer, primary_key=True)
    order_id: int = db.Column(
        db.Integer, db.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: int = db.Column(
        db.Integer, db.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: int = db.Column(db.Integer, nullable=False)
    # Price snapshot — do NOT reference Product.price; use this column.
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)

    # Relationships
    order = db.relationship("Order", back_populates="items")
    product = db.relationship("Product", back_populates="order_items")

    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        db.CheckConstraint("unit_price >= 0", name="ck_order_item_price_non_negative"),
    )

    @property
    def line_total(self):
        """Convenience property: quantity × unit_price."""
        return self.quantity * self.unit_price

    def __repr__(self) -> str:
        return (
            f"<OrderItem order={self.order_id} product={self.product_id} "
            f"qty={self.quantity} price={self.unit_price}>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Table 7 — fulfillment_tasks
# ─────────────────────────────────────────────────────────────────────────────

class FulfillmentTask(db.Model):
    """
    Application-level job queue.

    Each row represents a unit of work in the order-fulfillment pipeline.
    The scheduler reads READY rows and assigns them to workers.

    Key columns
    ───────────
    idempotency_key
        A unique key (typically "<order_id>:<task_type>") that prevents
        duplicate business effects on retry.  For example, if NOTIFY_CUSTOMER
        is retried, the handler checks for an existing notification row with
        this key before creating a new one.

    last_heartbeat
        Updated by the executing worker thread every ~10 seconds.  The
        recovery scan (WorkerManager) flags tasks as FAILED if this timestamp
        is older than ENGINE_HEARTBEAT_TIMEOUT seconds.

    priority
        Meaningful only when the PRIORITY scheduling policy is active.
        Ranges 1 (lowest) to 10 (highest).  Default priority is set per
        TaskType in task_handlers.py (Phase 4).

    deadline
        Meaningful only when the DEADLINE scheduling policy is active.
        NULL means no deadline (treated as ∞ in deadline-aware scheduling).
    """
    __tablename__ = "fulfillment_tasks"

    id: int = db.Column(db.Integer, primary_key=True)
    order_id: int = db.Column(
        db.Integer, db.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_type: str = db.Column(
        db.Enum(TaskType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    priority: int = db.Column(db.Integer, nullable=False, default=5)
    status: str = db.Column(
        db.Enum(TaskStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=TaskStatus.CREATED.value,
        index=True,
    )
    arrival_time: datetime = db.Column(db.DateTime, nullable=False, default=_now, index=True)
    deadline: datetime = db.Column(db.DateTime, nullable=True)
    estimated_duration_s: int = db.Column(db.Integer, nullable=True)
    assigned_worker_id: int = db.Column(
        db.Integer, db.ForeignKey("workers.id", ondelete="SET NULL"), nullable=True
    )
    retry_count: int = db.Column(db.Integer, nullable=False, default=0)
    max_retries: int = db.Column(db.Integer, nullable=False, default=3)
    error_message: str = db.Column(db.Text, nullable=True)
    # Unique key used for idempotent handler logic on retries.
    idempotency_key: str = db.Column(db.String(255), unique=True, nullable=False)
    last_heartbeat: datetime = db.Column(db.DateTime, nullable=True)
    started_at: datetime = db.Column(db.DateTime, nullable=True)
    completed_at: datetime = db.Column(db.DateTime, nullable=True)

    # Relationships
    order = db.relationship("Order", back_populates="tasks")
    assigned_worker = db.relationship("Worker", back_populates="tasks", foreign_keys=[assigned_worker_id])
    execution_logs = db.relationship(
        "TaskExecutionLog", back_populates="task", cascade="all, delete-orphan"
    )
    resource_allocations = db.relationship(
        "ResourceAllocation", back_populates="task", cascade="all, delete-orphan"
    )
    metric = db.relationship(
        "TaskMetric", back_populates="task", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.CheckConstraint("priority BETWEEN 1 AND 10", name="ck_task_priority_range"),
        db.CheckConstraint("retry_count >= 0", name="ck_task_retry_non_negative"),
        db.CheckConstraint("max_retries >= 0", name="ck_task_max_retries_non_negative"),
        # Composite index for the most common scheduler query:
        # WHERE status = 'READY' ORDER BY arrival_time (FCFS)
        db.Index("ix_tasks_status_arrival", "status", "arrival_time"),
        # Composite index for priority scheduling:
        # WHERE status = 'READY' ORDER BY priority DESC, arrival_time ASC
        db.Index("ix_tasks_status_priority", "status", "priority"),
    )

    def __repr__(self) -> str:
        return (
            f"<FulfillmentTask id={self.id} order={self.order_id} "
            f"type={self.task_type} status={self.status}>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Table 8 — workers
# ─────────────────────────────────────────────────────────────────────────────

class Worker(db.Model):
    """
    Registered worker threads managed by WorkerManager.

    A row is inserted when a worker thread starts and updated as it
    transitions between IDLE, BUSY, STOPPED, and CRASHED states.

    thread_ident: Python threading.get_ident() — useful for debugging but
    not guaranteed unique across restarts (OS thread IDs are recycled).

    last_heartbeat: updated every ~10 s by the worker thread.  If the main
    process crashes without cleanly stopping workers, the recovery scan
    detects stale heartbeats and marks those workers CRASHED.
    """
    __tablename__ = "workers"

    id: int = db.Column(db.Integer, primary_key=True)
    name: str = db.Column(db.String(100), nullable=False)
    status: str = db.Column(
        db.Enum(WorkerStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=WorkerStatus.IDLE.value,
        index=True,
    )
    thread_ident: int = db.Column(db.BigInteger, nullable=True)
    last_heartbeat: datetime = db.Column(db.DateTime, nullable=True)
    created_at: datetime = db.Column(db.DateTime, nullable=False, default=_now)

    # Relationships
    tasks = db.relationship(
        "FulfillmentTask", back_populates="assigned_worker",
        foreign_keys="FulfillmentTask.assigned_worker_id",
    )
    execution_logs = db.relationship("TaskExecutionLog", back_populates="worker")

    def __repr__(self) -> str:
        return f"<Worker id={self.id} name={self.name!r} status={self.status}>"


# ─────────────────────────────────────────────────────────────────────────────
# Table 9 — task_execution_log
# ─────────────────────────────────────────────────────────────────────────────

class TaskExecutionLog(db.Model):
    """
    Append-only audit trail of every task-state transition.

    One row is inserted each time a task moves from one status to another.
    This provides full traceability for debugging, grading, and viva demos.
    """
    __tablename__ = "task_execution_log"

    id: int = db.Column(db.Integer, primary_key=True)
    task_id: int = db.Column(
        db.Integer, db.ForeignKey("fulfillment_tasks.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    worker_id: int = db.Column(
        db.Integer, db.ForeignKey("workers.id", ondelete="SET NULL"), nullable=True
    )
    from_status: str = db.Column(
        db.Enum(TaskStatus, values_callable=lambda x: [e.value for e in x]), nullable=True
    )
    to_status: str = db.Column(
        db.Enum(TaskStatus, values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    changed_at: datetime = db.Column(db.DateTime, nullable=False, default=_now, index=True)
    notes: str = db.Column(db.Text, nullable=True)

    # Relationships
    task = db.relationship("FulfillmentTask", back_populates="execution_logs")
    worker = db.relationship("Worker", back_populates="execution_logs")

    def __repr__(self) -> str:
        return (
            f"<TaskExecutionLog task={self.task_id} "
            f"{self.from_status}→{self.to_status} at={self.changed_at}>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Table 10 — resources
# ─────────────────────────────────────────────────────────────────────────────

class Resource(db.Model):
    """
    Modeled limited resources (packing stations, inspection stations).

    ordering_priority defines the global acquisition order that all worker
    threads must respect.  A worker must acquire resources in ascending
    ordering_priority order.  This breaks the circular-wait condition that
    causes deadlock (Coffman condition 4).

    Example seed data:
      id=1  name='packing_station'    total_capacity=2  ordering_priority=1
      id=2  name='inspection_station' total_capacity=1  ordering_priority=2

    A task that needs both resources must always acquire id=1 first, then id=2.
    """
    __tablename__ = "resources"

    id: int = db.Column(db.Integer, primary_key=True)
    name: str = db.Column(db.String(100), unique=True, nullable=False)
    description: str = db.Column(db.Text, nullable=True)
    total_capacity: int = db.Column(db.Integer, nullable=False, default=1)
    # available_capacity is managed by ResourceManager in memory (semaphore)
    # and mirrored here for the dashboard.  In-memory semaphore is the
    # authoritative value; this column is for display only.
    available_capacity: int = db.Column(db.Integer, nullable=False, default=1)
    # Ascending ordering_priority enforces consistent acquisition order.
    ordering_priority: int = db.Column(db.Integer, nullable=False, default=0)

    # Relationships
    allocations = db.relationship(
        "ResourceAllocation", back_populates="resource", lazy="dynamic"
    )

    __table_args__ = (
        db.CheckConstraint("total_capacity > 0", name="ck_resource_capacity_positive"),
        db.CheckConstraint(
            "available_capacity >= 0", name="ck_resource_available_non_negative"
        ),
        db.CheckConstraint(
            "available_capacity <= total_capacity",
            name="ck_resource_available_le_total",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Resource id={self.id} name={self.name!r} "
            f"available={self.available_capacity}/{self.total_capacity}>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Table 11 — resource_allocations
# ─────────────────────────────────────────────────────────────────────────────

class ResourceAllocation(db.Model):
    """
    Tracks which task holds or is waiting for which resource.

    By querying rows in REQUESTED and HOLDING states, the ResourceManager
    can reconstruct the resource-allocation graph (RAG):
      - HOLDING  : task → resource edge (task holds resource)
      - REQUESTED: resource → task edge (task waiting for resource)

    A cycle in the RAG indicates deadlock.
    """
    __tablename__ = "resource_allocations"

    id: int = db.Column(db.Integer, primary_key=True)
    task_id: int = db.Column(
        db.Integer, db.ForeignKey("fulfillment_tasks.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    resource_id: int = db.Column(
        db.Integer, db.ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    state: str = db.Column(
        db.Enum(AllocationState, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=AllocationState.REQUESTED.value,
    )
    requested_at: datetime = db.Column(db.DateTime, nullable=False, default=_now)
    acquired_at: datetime = db.Column(db.DateTime, nullable=True)
    released_at: datetime = db.Column(db.DateTime, nullable=True)

    # Relationships
    task = db.relationship("FulfillmentTask", back_populates="resource_allocations")
    resource = db.relationship("Resource", back_populates="allocations")

    def __repr__(self) -> str:
        return (
            f"<ResourceAllocation task={self.task_id} "
            f"resource={self.resource_id} state={self.state}>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Table 12 — task_metrics
# ─────────────────────────────────────────────────────────────────────────────

class TaskMetric(db.Model):
    """
    One row per completed task; stores scheduling performance measurements.

    Formulae:
      wait_time_s        = started_at − arrival_time
      execution_time_s   = completed_at − started_at
      turnaround_time_s  = completed_at − arrival_time
      missed_deadline    = completed_at > deadline (if deadline is set)

    These rows power the dashboard comparison charts and the
    run_experiment.py script for fair policy comparisons.
    """
    __tablename__ = "task_metrics"

    id: int = db.Column(db.Integer, primary_key=True)
    task_id: int = db.Column(
        db.Integer, db.ForeignKey("fulfillment_tasks.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True,
    )
    scheduling_policy: str = db.Column(db.String(20), nullable=False, index=True)
    experiment_label: str = db.Column(db.String(100), nullable=True, index=True)
    wait_time_s: float = db.Column(db.Float, nullable=True)
    execution_time_s: float = db.Column(db.Float, nullable=True)
    turnaround_time_s: float = db.Column(db.Float, nullable=True)
    missed_deadline: bool = db.Column(db.Boolean, nullable=True, default=False)
    recorded_at: datetime = db.Column(db.DateTime, nullable=False, default=_now)

    # Relationships
    task = db.relationship("FulfillmentTask", back_populates="metric")

    def __repr__(self) -> str:
        return (
            f"<TaskMetric task={self.task_id} policy={self.scheduling_policy} "
            f"wait={self.wait_time_s:.2f}s turnaround={self.turnaround_time_s:.2f}s>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Table 13 — scheduling_config
# ─────────────────────────────────────────────────────────────────────────────

class SchedulingConfig(db.Model):
    """
    Administrator-controlled scheduling configuration (singleton table).

    Only one row should exist (id=1).  The admin dashboard reads and writes
    this row to change the active scheduling policy and worker count without
    restarting the application.

    Application design note:
    The WorkerManager checks this row at startup and on a configurable poll
    interval so that policy changes take effect without a server restart.
    Workers that are already RUNNING a task complete it under the old policy;
    the next READY task is picked using the updated policy.
    """
    __tablename__ = "scheduling_config"

    id: int = db.Column(db.Integer, primary_key=True)
    policy: str = db.Column(
        db.Enum(SchedulingPolicy, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=SchedulingPolicy.FCFS.value,
    )
    worker_count: int = db.Column(db.Integer, nullable=False, default=2)
    # Seconds: worker heartbeat older than this → worker presumed crashed.
    heartbeat_timeout_s: int = db.Column(db.Integer, nullable=False, default=60)
    # Seconds: task RUNNING for longer than this (even with heartbeat) → stuck.
    task_timeout_s: int = db.Column(db.Integer, nullable=False, default=300)
    updated_at: datetime = db.Column(
        db.DateTime, nullable=False, default=_now, onupdate=_now
    )
    updated_by_id: int = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    updated_by = db.relationship("User", foreign_keys=[updated_by_id])

    __table_args__ = (
        db.CheckConstraint("worker_count >= 1", name="ck_config_worker_count_positive"),
        db.CheckConstraint(
            "heartbeat_timeout_s > 0", name="ck_config_heartbeat_positive"
        ),
        db.CheckConstraint("task_timeout_s > 0", name="ck_config_task_timeout_positive"),
    )

    def __repr__(self) -> str:
        return (
            f"<SchedulingConfig policy={self.policy} "
            f"workers={self.worker_count}>"
        )
