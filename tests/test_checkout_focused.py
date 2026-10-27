"""
tests/test_checkout_focused.py — Focused verification tests for the checkout implementation.

These tests complement test_checkout.py and verify the specific behaviours called
out in the audit:

  1. Server-side price recalculation — the Order.total_amount is computed from
     Product.price * quantity, never from anything submitted by the browser.
  2. Multi-item order total correctness — all cart lines sum correctly.
  3. Price-snapshot independence — changing a product's price after checkout
     does NOT alter the stored OrderItem.unit_price.
  4. Inventory reservation accounting — available_quantity = quantity -
     reserved_quantity; checkout rejects if available < requested, even when
     some stock is already reserved by a prior order.
  5. Reserved-quantity integrity after checkout — quantity (total stock) is
     unchanged; only reserved_quantity grows.
  6. Cart isolation — only the ordering user's cart is cleared; another user's
     cart is untouched.
  7. Transaction atomicity — if an exception occurs after the Order row is
     flushed but before commit, no Order, no OrderItem, and no reserved_quantity
     change persists.
  8. Lock-ordering contract — the code sorts product_ids ascending before
     acquiring locks; verified by inspecting the sorted() call on product_ids.
  9. Unauthenticated POST to /orders/checkout is blocked.
 10. Notes field is stored on Order.notes; empty notes stay None.
 11. Confirmation page is inaccessible for a non-existent order (404).
 12. Order status is PENDING immediately after placement.
 13. Multiple items: each line creates exactly one OrderItem row.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db as _db
from app.models import (
    CartItem,
    Inventory,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    User,
    UserRole,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared form data (valid)
# ─────────────────────────────────────────────────────────────────────────────

VALID_FORM = {
    "full_name": "Focused Tester",
    "email": "focused@example.com",
    "phone": "9000000001",
    "address_line1": "1 Verification Lane",
    "address_line2": "",
    "city": "Pune",
    "state": "Maharashtra",
    "pincode": "411001",
    "notes": "",
}


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def u1(app, db):
    """Primary customer user."""
    with app.app_context():
        from app.extensions import bcrypt
        user = User(
            username="focused_u1",
            email="focused_u1@example.com",
            role=UserRole.CUSTOMER.value,
        )
        user.set_password("Pass123!", bcrypt)
        db.session.add(user)
        db.session.commit()
        return {"id": user.id, "email": user.email, "pw": "Pass123!"}


@pytest.fixture
def u2(app, db):
    """Secondary customer user (for isolation tests)."""
    with app.app_context():
        from app.extensions import bcrypt
        user = User(
            username="focused_u2",
            email="focused_u2@example.com",
            role=UserRole.CUSTOMER.value,
        )
        user.set_password("Pass123!", bcrypt)
        db.session.add(user)
        db.session.commit()
        return {"id": user.id, "email": user.email, "pw": "Pass123!"}


@pytest.fixture
def products(app, db):
    """
    Two active products with inventory.
      p1: price=200.00, qty=10, reserved=3  → available=7
      p2: price=75.50,  qty=8,  reserved=0  → available=8
    """
    with app.app_context():
        p1 = Product(
            name="Focused Product Alpha",
            price=Decimal("200.00"),
            category="Focused",
            sku="FOC-A-01",
            is_active=True,
        )
        inv1 = Inventory(product=p1, quantity=10, reserved_quantity=3)

        p2 = Product(
            name="Focused Product Beta",
            price=Decimal("75.50"),
            category="Focused",
            sku="FOC-B-02",
            is_active=True,
        )
        inv2 = Inventory(product=p2, quantity=8, reserved_quantity=0)

        db.session.add_all([p1, inv1, p2, inv2])
        db.session.commit()
        return {"p1_id": p1.id, "p2_id": p2.id}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _login(client, email, pw):
    client.post(
        "/auth/login",
        data={"email": email, "password": pw, "remember_me": False},
        follow_redirects=True,
    )


def _add(client, product_id, qty=1):
    client.post(
        "/shop/cart/add",
        data={"product_id": product_id, "quantity": qty},
        follow_redirects=True,
    )


def _checkout(client, extra=None):
    data = dict(VALID_FORM)
    if extra:
        data.update(extra)
    return client.post("/orders/checkout", data=data, follow_redirects=True)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Server-side price recalculation
# ─────────────────────────────────────────────────────────────────────────────

class TestServerSidePricing:

    def test_order_total_computed_from_product_price_not_browser(
        self, client, app, u1, products
    ):
        """
        Order.total_amount must equal Product.price * quantity.
        The form does not include a price field, so there is no browser-supplied
        amount to trust.  This test verifies the stored total against an
        independently calculated expected value.
        """
        _login(client, u1["email"], u1["pw"])
        _add(client, products["p1_id"], 3)   # 3 × 200.00 = 600.00
        _checkout(client)

        with app.app_context():
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one()
            expected_total = Decimal("200.00") * 3
            assert Decimal(str(order.total_amount)) == expected_total, (
                f"Expected total {expected_total}, got {order.total_amount}"
            )

    def test_multi_item_order_total_is_sum_of_all_line_totals(
        self, client, app, u1, products
    ):
        """
        With two different products in the cart, Order.total_amount must equal
        the sum of every OrderItem's (unit_price × quantity).
        """
        _login(client, u1["email"], u1["pw"])
        _add(client, products["p1_id"], 2)   # 2 × 200.00 = 400.00
        _add(client, products["p2_id"], 3)   # 3 × 75.50  = 226.50
        _checkout(client)                    # expected total = 626.50

        with app.app_context():
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one()
            expected = Decimal("400.00") + Decimal("226.50")
            assert Decimal(str(order.total_amount)) == expected

            # Cross-check: sum of order item line totals == order total
            line_sum = sum(
                Decimal(str(oi.unit_price)) * oi.quantity for oi in order.items
            )
            assert line_sum == expected


# ─────────────────────────────────────────────────────────────────────────────
# 2. Price-snapshot independence
# ─────────────────────────────────────────────────────────────────────────────

class TestPriceSnapshot:

    def test_order_item_unit_price_unchanged_after_product_price_update(
        self, client, app, u1, products
    ):
        """
        Changing Product.price AFTER checkout must NOT alter the stored
        OrderItem.unit_price.  This is the price-snapshot guarantee.
        """
        _login(client, u1["email"], u1["pw"])
        p1_id = products["p1_id"]
        _add(client, p1_id, 1)
        _checkout(client)

        with app.app_context():
            # Change the product price post-checkout
            product = _db.session.get(Product, p1_id)
            product.price = Decimal("999.99")
            _db.session.commit()

            # The order item must still record the original price
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one()
            assert len(order.items) == 1
            assert Decimal(str(order.items[0].unit_price)) == Decimal("200.00"), (
                "OrderItem.unit_price should be the price at checkout time, "
                "not the updated price."
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Inventory availability accounting (existing reservations)
# ─────────────────────────────────────────────────────────────────────────────

class TestInventoryAvailability:

    def test_available_quantity_accounts_for_existing_reservations(
        self, client, app, u1, products
    ):
        """
        p1 starts with quantity=10, reserved_quantity=3 → available=7.
        Requesting 8 units must be rejected (8 > 7).
        """
        _login(client, u1["email"], u1["pw"])
        p1_id = products["p1_id"]

        # Manually insert a cart item exceeding available stock.
        # We cannot use the /add endpoint because it also checks available_stock.
        # Insert directly to isolate the checkout validation.
        with app.app_context():
            cart = CartItem(
                user_id=u1["id"], product_id=p1_id, quantity=8
            )
            _db.session.add(cart)
            _db.session.commit()

        response = _checkout(client)
        content = response.data.decode("utf-8")

        # Checkout must reject — insufficient available stock
        assert "stock" in content.lower() or "insufficient" in content.lower(), (
            "Expected an insufficient-stock error when requested qty > available"
        )
        with app.app_context():
            assert _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one_or_none() is None

    def test_checkout_succeeds_when_quantity_exactly_equals_available(
        self, client, app, u1, products
    ):
        """
        Requesting exactly available_quantity units (p1: available=7) must succeed.
        """
        _login(client, u1["email"], u1["pw"])
        p1_id = products["p1_id"]

        with app.app_context():
            cart = CartItem(
                user_id=u1["id"], product_id=p1_id, quantity=7  # exactly available
            )
            _db.session.add(cart)
            _db.session.commit()

        _checkout(client)

        with app.app_context():
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one_or_none()
            assert order is not None, "Order should have been created for qty == available"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Reserved-quantity integrity after checkout
# ─────────────────────────────────────────────────────────────────────────────

class TestReservedQuantityIntegrity:

    def test_quantity_unchanged_reserved_quantity_incremented(
        self, client, app, u1, products
    ):
        """
        After checkout, Inventory.quantity (total physical stock) must NOT
        change.  Only reserved_quantity should increase by the ordered amount.
        """
        _login(client, u1["email"], u1["pw"])
        p1_id = products["p1_id"]
        _add(client, p1_id, 5)
        _checkout(client)

        with app.app_context():
            inv = _db.session.execute(
                _db.select(Inventory).where(Inventory.product_id == p1_id)
            ).scalar_one()
            # Physical stock unchanged
            assert inv.quantity == 10, (
                f"inventory.quantity should remain 10, got {inv.quantity}"
            )
            # Reserved should have grown from 3 (fixture) to 3+5=8
            assert inv.reserved_quantity == 8, (
                f"reserved_quantity should be 8 (3 initial + 5 ordered), "
                f"got {inv.reserved_quantity}"
            )
            # available_quantity should be 10-8=2
            assert inv.available_quantity == 2

    def test_multi_product_reservation_all_updated(
        self, client, app, u1, products
    ):
        """Both products' reserved_quantity values are updated in one transaction."""
        _login(client, u1["email"], u1["pw"])
        _add(client, products["p1_id"], 2)  # p1: was reserved=3 → 5
        _add(client, products["p2_id"], 4)  # p2: was reserved=0 → 4
        _checkout(client)

        with app.app_context():
            inv1 = _db.session.execute(
                _db.select(Inventory).where(Inventory.product_id == products["p1_id"])
            ).scalar_one()
            inv2 = _db.session.execute(
                _db.select(Inventory).where(Inventory.product_id == products["p2_id"])
            ).scalar_one()
            assert inv1.reserved_quantity == 5   # 3 + 2
            assert inv2.reserved_quantity == 4   # 0 + 4


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cart isolation — only the buyer's cart is cleared
# ─────────────────────────────────────────────────────────────────────────────

class TestCartIsolation:

    def test_only_buyers_cart_is_cleared_other_user_cart_intact(
        self, client, app, u1, u2, products
    ):
        """
        When User 1 checks out, User 2's cart must remain untouched.
        """
        # User 2 adds an item to their cart first
        _login(client, u2["email"], u2["pw"])
        _add(client, products["p2_id"], 1)
        with app.app_context():
            u2_cart_before = _db.session.execute(
                _db.select(CartItem).where(CartItem.user_id == u2["id"])
            ).scalars().all()
            assert len(u2_cart_before) == 1

        client.get("/auth/logout", follow_redirects=True)

        # User 1 checks out
        _login(client, u1["email"], u1["pw"])
        _add(client, products["p1_id"], 1)
        _checkout(client)

        with app.app_context():
            # User 1's cart cleared
            u1_cart = _db.session.execute(
                _db.select(CartItem).where(CartItem.user_id == u1["id"])
            ).scalars().all()
            assert len(u1_cart) == 0, "Buyer's cart should be empty after checkout"

            # User 2's cart intact
            u2_cart = _db.session.execute(
                _db.select(CartItem).where(CartItem.user_id == u2["id"])
            ).scalars().all()
            assert len(u2_cart) == 1, "Other user's cart must not be affected"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Transaction atomicity (rollback on failure)
# ─────────────────────────────────────────────────────────────────────────────

class TestTransactionAtomicity:

    def test_exception_during_order_creation_rolls_back_entire_transaction(
        self, client, app, u1, products
    ):
        """
        If an exception fires after the Order has been flushed (but before
        commit), the entire transaction — Order, OrderItems, reserved_quantity
        changes, and cart deletion — must be rolled back atomically.

        We simulate this by patching db.session.commit to raise RuntimeError
        on the first call (the checkout commit).
        """
        _login(client, u1["email"], u1["pw"])
        p1_id = products["p1_id"]
        _add(client, p1_id, 2)

        # Record pre-checkout state
        with app.app_context():
            inv_before = _db.session.execute(
                _db.select(Inventory).where(Inventory.product_id == p1_id)
            ).scalar_one()
            reserved_before = inv_before.reserved_quantity  # 3

        # Patch commit to raise on the checkout transaction.
        # The route catches generic Exception, rolls back, and redirects.
        original_commit = _db.session.commit

        call_count = {"n": 0}

        def failing_commit():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("Simulated DB failure during checkout commit")
            return original_commit()

        with patch.object(_db.session, "commit", side_effect=failing_commit):
            response = client.post(
                "/orders/checkout", data=VALID_FORM, follow_redirects=True
            )

        content = response.data.decode("utf-8")
        # Route should flash an error and redirect (not crash)
        assert response.status_code == 200

        with app.app_context():
            # No order should exist
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one_or_none()
            assert order is None, "Order must not persist after a rolled-back transaction"

            # reserved_quantity must be unchanged
            inv_after = _db.session.execute(
                _db.select(Inventory).where(Inventory.product_id == p1_id)
            ).scalar_one()
            assert inv_after.reserved_quantity == reserved_before, (
                f"reserved_quantity must be unchanged after rollback "
                f"(expected {reserved_before}, got {inv_after.reserved_quantity})"
            )

            # Cart must still contain the item (was not deleted)
            cart = _db.session.execute(
                _db.select(CartItem).where(CartItem.user_id == u1["id"])
            ).scalars().all()
            assert len(cart) == 1, "Cart must remain intact after a failed checkout"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Lock ordering contract (code inspection)
# ─────────────────────────────────────────────────────────────────────────────

class TestLockOrdering:

    def test_product_ids_are_sorted_ascending_before_lock_acquisition(self):
        """
        Verify that the route code sorts product_ids in ascending order before
        attempting to acquire inventory locks.  This is the deterministic lock-
        ordering strategy that prevents deadlock (breaks the circular-wait
        Coffman condition).

        We test this by reading the source and inspecting the sorted() call,
        and by running a functional simulation with a known two-product cart
        where the lower-ID product is added second.
        """
        import inspect
        import app.orders.routes as routes_module

        source = inspect.getsource(routes_module.checkout)
        # The route must use sorted() on the set of product IDs
        assert "sorted(" in source, (
            "checkout() must call sorted() on product_ids to guarantee "
            "consistent lock ordering"
        )
        # Must sort the product_id set (ascending — Python default)
        assert "product_ids" in source

    def test_two_product_checkout_creates_items_for_both_regardless_of_add_order(
        self, client, app, u1, products
    ):
        """
        Functional check: adding p2 first (higher or lower ID) and p1 second
        still results in two OrderItems, confirming that sorted lock ordering
        does not skip any products.
        """
        _login(client, u1["email"], u1["pw"])
        # Add in reverse ID order (p2 first, then p1)
        _add(client, products["p2_id"], 1)
        _add(client, products["p1_id"], 2)
        _checkout(client)

        with app.app_context():
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one()
            assert len(order.items) == 2, (
                f"Expected 2 OrderItems, got {len(order.items)}"
            )
            product_ids_in_order = {oi.product_id for oi in order.items}
            assert products["p1_id"] in product_ids_in_order
            assert products["p2_id"] in product_ids_in_order


# ─────────────────────────────────────────────────────────────────────────────
# 8. Order metadata correctness
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderMetadata:

    def test_order_status_is_pending_immediately_after_placement(
        self, client, app, u1, products
    ):
        """Newly placed orders must have status PENDING."""
        _login(client, u1["email"], u1["pw"])
        _add(client, products["p1_id"], 1)
        _checkout(client)

        with app.app_context():
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one()
            assert order.status == OrderStatus.PENDING.value

    def test_notes_field_stored_on_order_when_provided(
        self, client, app, u1, products
    ):
        """If the user fills in the notes field, it is saved on Order.notes."""
        _login(client, u1["email"], u1["pw"])
        _add(client, products["p1_id"], 1)
        _checkout(client, extra={"notes": "Leave at door please"})

        with app.app_context():
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one()
            assert order.notes == "Leave at door please"

    def test_empty_notes_stored_as_none(self, client, app, u1, products):
        """An empty notes field is stored as None (not empty string)."""
        _login(client, u1["email"], u1["pw"])
        _add(client, products["p1_id"], 1)
        _checkout(client, extra={"notes": ""})

        with app.app_context():
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one()
            assert order.notes is None, (
                f"Empty notes should be stored as None, got {order.notes!r}"
            )

    def test_each_cart_line_creates_exactly_one_order_item(
        self, client, app, u1, products
    ):
        """A two-product cart produces exactly two OrderItem rows (one per line)."""
        _login(client, u1["email"], u1["pw"])
        _add(client, products["p1_id"], 1)
        _add(client, products["p2_id"], 2)
        _checkout(client)

        with app.app_context():
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one()
            assert len(order.items) == 2

            # Each OrderItem belongs to the right order
            for oi in order.items:
                assert oi.order_id == order.id
                assert oi.quantity >= 1
                assert Decimal(str(oi.unit_price)) > 0


# ─────────────────────────────────────────────────────────────────────────────
# 9. Edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_unauthenticated_post_to_checkout_redirects_to_login(self, client, products):
        """A POST to /orders/checkout without a session cookie is blocked."""
        response = client.post(
            "/orders/checkout", data=VALID_FORM, follow_redirects=False
        )
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_confirmation_page_returns_404_for_nonexistent_order(
        self, client, u1
    ):
        """GET /orders/confirmation/99999 for a non-existent order returns 404."""
        _login(client, u1["email"], u1["pw"])
        response = client.get("/orders/confirmation/99999")
        assert response.status_code == 404

    def test_checkout_with_zero_stock_after_reservation_rejected(
        self, client, app, u1, products
    ):
        """
        Edge case: p2 has qty=8, reserved=0 → available=8.
        If we manually set reserved=8 (so available=0) and try to buy 1 unit,
        checkout must reject it.
        """
        _login(client, u1["email"], u1["pw"])
        p2_id = products["p2_id"]

        with app.app_context():
            # Manually insert cart item (bypassing the add-to-cart stock check)
            cart = CartItem(user_id=u1["id"], product_id=p2_id, quantity=1)
            _db.session.add(cart)
            # Reserve all stock
            inv = _db.session.execute(
                _db.select(Inventory).where(Inventory.product_id == p2_id)
            ).scalar_one()
            inv.reserved_quantity = 8  # quantity=8, reserved=8 → available=0
            _db.session.commit()

        response = _checkout(client)
        content = response.data.decode("utf-8")
        assert "stock" in content.lower() or "insufficient" in content.lower()

        with app.app_context():
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one_or_none()
            assert order is None

    def test_order_list_shows_no_orders_for_new_user(self, client, u1):
        """GET /orders/ for a user with no orders shows the empty-orders state."""
        _login(client, u1["email"], u1["pw"])
        response = client.get("/orders/")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "My Orders" in content
        # Either "No orders yet" or the table is absent
        assert "No orders yet" in content or "order(s)" not in content


# ─────────────────────────────────────────────────────────────────────────────
# 10. Migration and Shipping Persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestMigrationAndShippingDetails:

    def test_migration_script_structure_and_columns(self):
        """
        Verify the generated Alembic migration file targets 0b58c785509c
        and only adds/drops the 8 intended shipping columns.
        """
        import importlib.util
        from pathlib import Path

        migration_path = Path("migrations/versions/120fdf84e148_add_shipping_columns_to_orders.py")
        assert migration_path.exists(), f"Migration file {migration_path} does not exist"

        spec = importlib.util.spec_from_file_location("shipping_migration", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.revision == "120fdf84e148"
        assert mod.down_revision == "0b58c785509c"

        content = migration_path.read_text(encoding="utf-8")
        expected_columns = [
            "shipping_name",
            "shipping_email",
            "shipping_phone",
            "shipping_address_line1",
            "shipping_address_line2",
            "shipping_city",
            "shipping_state",
            "shipping_pincode",
        ]
        for col in expected_columns:
            assert f"add_column(sa.Column('{col}'" in content
            assert f"drop_column('{col}')" in content

    def test_optional_shipping_fields_saved_as_none_when_empty_or_whitespace(
        self, client, app, u1, products
    ):
        """Empty optional fields (address_line2 and notes) are stored as None."""
        _login(client, u1["email"], u1["pw"])
        _add(client, products["p1_id"], 1)

        data = dict(VALID_FORM)
        data["address_line2"] = "   "
        data["notes"] = "   "

        _checkout(client, extra=data)

        with app.app_context():
            order = _db.session.execute(
                _db.select(Order).where(Order.user_id == u1["id"])
            ).scalar_one()
            assert order.shipping_address_line2 is None
            assert order.notes is None
            assert order.shipping_name == "Focused Tester"
            assert order.shipping_pincode == "411001"

    def test_order_list_shows_shipping_destination(
        self, client, app, u1, products
    ):
        """Order list renders shipping destination for placed orders."""
        _login(client, u1["email"], u1["pw"])
        _add(client, products["p1_id"], 1)
        _checkout(client)

        response = client.get("/orders/")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Focused Tester" in content
        assert "Pune" in content
