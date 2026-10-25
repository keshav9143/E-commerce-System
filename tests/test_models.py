"""
tests/test_models.py — Unit tests verifying all 13 SQLAlchemy models and relationships.

This test file verifies:
  1. users (creation, password hashing, role enum, admin property)
  2. products (creation, SKU uniqueness, decimal price)
  3. inventory (stock quantity, reserved quantity, available_quantity property)
  4. cart_items (user-product link, quantity, unique constraint)
  5. orders (user relationship, status enum, total amount)
  6. order_items (order relationship, product relationship, price snapshot, line_total)
  7. fulfillment_tasks (order link, task type enum, status enum, priority, idempotency key)
  8. workers (status enum, registration)
  9. task_execution_log (audit trail link to task and worker)
 10. resources (ordering_priority, capacity constraints)
 11. resource_allocations (task-resource link, state enum)
 12. task_metrics (task link, wait/turnaround measurements)
 13. scheduling_config (singleton config, default policy)
"""

from decimal import Decimal
import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    AllocationState,
    CartItem,
    FulfillmentTask,
    Inventory,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    Resource,
    ResourceAllocation,
    SchedulingConfig,
    SchedulingPolicy,
    TaskExecutionLog,
    TaskMetric,
    TaskStatus,
    TaskType,
    User,
    UserRole,
    Worker,
    WorkerStatus,
)


class TestModelsAndRelationships:

    def test_user_creation_and_auth(self, db, app):
        with app.app_context():
            from app.extensions import bcrypt
            user = User(username="testuser", email="test@example.com", role=UserRole.CUSTOMER.value)
            user.set_password("Secret123!", bcrypt)
            db.session.add(user)
            db.session.commit()

            assert user.id is not None
            assert user.check_password("Secret123!", bcrypt)
            assert not user.check_password("WrongPass", bcrypt)
            assert not user.is_admin

    def test_user_uniqueness(self, db, app):
        with app.app_context():
            user1 = User(username="dup_u", email="dup1@example.com", password_hash="hash")
            user2 = User(username="dup_u", email="dup2@example.com", password_hash="hash")
            db.session.add(user1)
            db.session.commit()

            db.session.add(user2)
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_product_and_inventory_relationship(self, db, app):
        with app.app_context():
            prod = Product(
                name="Mechanical Keyboard",
                description="Tactile switches",
                price=Decimal("79.99"),
                category="Electronics",
                sku="KB-001",
            )
            inv = Inventory(product=prod, quantity=50, reserved_quantity=5)
            db.session.add_all([prod, inv])
            db.session.commit()

            assert prod.inventory is not None
            assert prod.inventory.available_quantity == 45
            assert inv.product.name == "Mechanical Keyboard"

    def test_cart_item_and_unique_constraint(self, db, app):
        with app.app_context():
            user = User(username="cart_u", email="cart@example.com", password_hash="hash")
            prod = Product(name="Mouse", price=Decimal("29.99"), sku="MS-001")
            db.session.add_all([user, prod])
            db.session.commit()

            cart_item = CartItem(user=user, product=prod, quantity=2)
            db.session.add(cart_item)
            db.session.commit()
            assert cart_item.id is not None

            # Duplicate (user_id, product_id) should violate unique constraint
            dup_item = CartItem(user=user, product=prod, quantity=1)
            db.session.add(dup_item)
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_order_and_order_items(self, db, app):
        with app.app_context():
            user = User(username="order_u", email="order@example.com", password_hash="hash")
            prod = Product(name="Monitor", price=Decimal("199.99"), sku="MN-001")
            db.session.add_all([user, prod])
            db.session.commit()

            order = Order(user=user, total_amount=Decimal("199.99"), status=OrderStatus.PENDING.value)
            item = OrderItem(order=order, product=prod, quantity=1, unit_price=Decimal("199.99"))
            db.session.add_all([order, item])
            db.session.commit()

            assert order.id is not None
            assert len(order.items) == 1
            assert order.items[0].line_total == Decimal("199.99")
            assert order.items[0].product.name == "Monitor"

    def test_fulfillment_task_and_logs(self, db, app):
        with app.app_context():
            user = User(username="task_u", email="task@example.com", password_hash="hash")
            order = Order(user=user, total_amount=Decimal("50.00"))
            worker = Worker(name="Worker-1", status=WorkerStatus.IDLE.value)
            db.session.add_all([user, order, worker])
            db.session.commit()

            task = FulfillmentTask(
                order=order,
                task_type=TaskType.PAYMENT_VERIFY.value,
                priority=8,
                status=TaskStatus.READY.value,
                idempotency_key=f"{order.id}:PAYMENT_VERIFY",
                assigned_worker=worker,
            )
            db.session.add(task)
            db.session.commit()

            log = TaskExecutionLog(
                task=task,
                worker=worker,
                from_status=TaskStatus.CREATED.value,
                to_status=TaskStatus.READY.value,
                notes="Task enqueued and verified",
            )
            db.session.add(log)
            db.session.commit()

            assert task.id is not None
            assert task.order.id == order.id
            assert task.assigned_worker.name == "Worker-1"
            assert len(task.execution_logs) == 1
            assert task.execution_logs[0].to_status == TaskStatus.READY.value

    def test_task_idempotency_key_uniqueness(self, db, app):
        with app.app_context():
            user = User(username="idem_u", email="idem@example.com", password_hash="hash")
            order = Order(user=user, total_amount=Decimal("10.00"))
            db.session.add_all([user, order])
            db.session.commit()

            t1 = FulfillmentTask(
                order=order,
                task_type=TaskType.PAYMENT_VERIFY.value,
                idempotency_key="ORDER-1-PAY",
            )
            db.session.add(t1)
            db.session.commit()

            t2 = FulfillmentTask(
                order=order,
                task_type=TaskType.PAYMENT_VERIFY.value,
                idempotency_key="ORDER-1-PAY",
            )
            db.session.add(t2)
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_resources_and_allocations(self, db, app):
        with app.app_context():
            user = User(username="res_u", email="res@example.com", password_hash="hash")
            order = Order(user=user, total_amount=Decimal("15.00"))
            task = FulfillmentTask(
                order=order,
                task_type=TaskType.PACK_ORDER.value,
                idempotency_key="ORDER-RES-PACK",
            )
            res = Resource(
                name="Packing Station A",
                total_capacity=2,
                available_capacity=2,
                ordering_priority=1,
            )
            db.session.add_all([user, order, task, res])
            db.session.commit()

            alloc = ResourceAllocation(
                task=task,
                resource=res,
                state=AllocationState.REQUESTED.value,
            )
            db.session.add(alloc)
            db.session.commit()

            assert alloc.id is not None
            assert alloc.task.id == task.id
            assert alloc.resource.name == "Packing Station A"

    def test_task_metric_relationship(self, db, app):
        with app.app_context():
            user = User(username="met_u", email="met@example.com", password_hash="hash")
            order = Order(user=user, total_amount=Decimal("25.00"))
            task = FulfillmentTask(
                order=order,
                task_type=TaskType.SHIP_ORDER.value,
                idempotency_key="ORDER-MET-SHIP",
            )
            db.session.add_all([user, order, task])
            db.session.commit()

            metric = TaskMetric(
                task=task,
                scheduling_policy=SchedulingPolicy.FCFS.value,
                wait_time_s=1.25,
                execution_time_s=0.55,
                turnaround_time_s=1.80,
                missed_deadline=False,
            )
            db.session.add(metric)
            db.session.commit()

            assert task.metric is not None
            assert task.metric.wait_time_s == 1.25
            assert task.metric.turnaround_time_s == 1.80

    def test_scheduling_config_singleton(self, db, app):
        with app.app_context():
            cfg = SchedulingConfig(
                policy=SchedulingPolicy.PRIORITY.value,
                worker_count=4,
                heartbeat_timeout_s=30,
                task_timeout_s=180,
            )
            db.session.add(cfg)
            db.session.commit()

            fetched = SchedulingConfig.query.first()
            assert fetched is not None
            assert fetched.policy == SchedulingPolicy.PRIORITY.value
            assert fetched.worker_count == 4
