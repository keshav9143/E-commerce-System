"""
tests/test_checkout.py — Comprehensive tests for the checkout feature (Phase 3, Step 3).

Test coverage:
  1. Authentication enforcement (unauthenticated access redirects to login)
  2. Empty cart handling (redirects to cart with warning flash)
  3. Form validation (GET renders form, POST with invalid data shows errors)
  4. Successful checkout (creates order, order items, clears cart, updates reserved_quantity)
  5. Stock validation (insufficient stock rejected, inactive product rejected)
  6. Order isolation (user cannot view another user's confirmation)
  7. Duplicate submission prevention (empty cart after order redirects to cart)
  8. Order list page
"""

from decimal import Decimal
import pytest

from app.models import CartItem, Inventory, Order, OrderItem, Product, User, UserRole


# ─────────────────────────────────────────────────────────────────────────────
# Valid checkout form data
# ─────────────────────────────────────────────────────────────────────────────

VALID_FORM = {
    "full_name": "Test Customer",
    "email": "customer@example.com",
    "phone": "9876543210",
    "address_line1": "123 Test Street",
    "address_line2": "",
    "city": "Mumbai",
    "state": "Maharashtra",
    "pincode": "400001",
    "notes": "",
}


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def checkout_user(app, db):
    """Create a regular customer user and return credentials + id."""
    with app.app_context():
        from app.extensions import bcrypt

        user = User(
            username="checkout_customer",
            email="checkout@example.com",
            role=UserRole.CUSTOMER.value,
        )
        user.set_password("Password123!", bcrypt)
        db.session.add(user)
        db.session.commit()
        return {"id": user.id, "email": user.email, "password": "Password123!"}


@pytest.fixture
def checkout_user2(app, db):
    """Create a second customer for isolation tests."""
    with app.app_context():
        from app.extensions import bcrypt

        user = User(
            username="other_customer",
            email="other@example.com",
            role=UserRole.CUSTOMER.value,
        )
        user.set_password("Password123!", bcrypt)
        db.session.add(user)
        db.session.commit()
        return {"id": user.id, "email": user.email, "password": "Password123!"}


@pytest.fixture
def checkout_products(app, db):
    """Create two active products with inventory.

    p1: price=100.00, qty=10, reserved=0  → available=10
    p2: price=50.00,  qty=5,  reserved=0  → available=5
    """
    with app.app_context():
        p1 = Product(
            name="Test Widget A",
            description="A test product.",
            price=Decimal("100.00"),
            category="Test",
            sku="TEST-A-01",
            is_active=True,
        )
        inv1 = Inventory(product=p1, quantity=10, reserved_quantity=0)

        p2 = Product(
            name="Test Widget B",
            description="Another test product.",
            price=Decimal("50.00"),
            category="Test",
            sku="TEST-B-02",
            is_active=True,
        )
        inv2 = Inventory(product=p2, quantity=5, reserved_quantity=0)

        db.session.add_all([p1, inv1, p2, inv2])
        db.session.commit()
        return {"p1_id": p1.id, "p2_id": p2.id}


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _login(client, email, password):
    """Log in via the auth endpoint."""
    client.post(
        "/auth/login",
        data={"email": email, "password": password, "remember_me": False},
        follow_redirects=True,
    )


def _add_to_cart(client, product_id, quantity=1):
    """Add a product to the cart."""
    client.post(
        "/shop/cart/add",
        data={"product_id": product_id, "quantity": quantity},
        follow_redirects=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckoutAuthentication:

    def test_unauthenticated_checkout_redirects_to_login(self, client):
        """GET /orders/checkout without login redirects to login."""
        response = client.get("/orders/checkout", follow_redirects=False)
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_unauthenticated_confirmation_redirects_to_login(self, client):
        """GET /orders/confirmation/1 without login redirects to login."""
        response = client.get("/orders/confirmation/1", follow_redirects=False)
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]


class TestCheckoutEmptyCart:

    def test_checkout_with_empty_cart_redirects_to_cart(self, client, checkout_user):
        """Attempting checkout with empty cart flashes warning and redirects to cart."""
        _login(client, checkout_user["email"], checkout_user["password"])
        response = client.get("/orders/checkout", follow_redirects=True)
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        # Should be on the cart page (empty state or warning)
        assert "empty" in content.lower() or "cart" in content.lower()


class TestCheckoutFormValidation:

    def test_checkout_get_shows_form(self, client, checkout_user, checkout_products):
        """GET /orders/checkout renders the checkout form."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 1)

        response = client.get("/orders/checkout")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Checkout" in content
        assert "Place Order" in content
        assert "Full Name" in content

    def test_checkout_invalid_form_shows_errors(self, client, checkout_user, checkout_products):
        """POST with empty required fields fails validation and re-shows the form."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 1)

        # Submit with all fields empty
        response = client.post(
            "/orders/checkout",
            data={
                "full_name": "",
                "email": "",
                "phone": "",
                "address_line1": "",
                "address_line2": "",
                "city": "",
                "state": "",
                "pincode": "",
                "notes": "",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        # Form errors should appear; no order should be created
        assert "required" in content.lower() or "invalid" in content.lower() or "is-invalid" in content

    def test_checkout_invalid_pincode_rejected(self, client, checkout_user, checkout_products):
        """Pincode with fewer than 6 digits or non-numeric is rejected."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 1)

        bad_pincodes = ["12345", "1234567", "ABCDEF", "40000A"]
        for pin in bad_pincodes:
            data = {**VALID_FORM, "pincode": pin}
            response = client.post("/orders/checkout", data=data, follow_redirects=True)
            content = response.data.decode("utf-8")
            # Should not have created an order (stays on checkout or shows form errors)
            assert "Place Order" in content or "is-invalid" in content or "Pincode" in content


class TestCheckoutSuccess:

    def test_successful_checkout_creates_order(self, client, app, checkout_user, checkout_products):
        """A valid POST to /orders/checkout creates an Order row."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 2)

        response = client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)
        assert response.status_code == 200

        with app.app_context():
            from app.extensions import db
            order = db.session.execute(
                db.select(Order).where(Order.user_id == checkout_user["id"])
            ).scalar_one_or_none()
            assert order is not None
            assert order.status == "PENDING"

    def test_successful_checkout_creates_order_items_with_price_snapshot(
        self, client, app, checkout_user, checkout_products
    ):
        """Order items are created with the correct price snapshot."""
        _login(client, checkout_user["email"], checkout_user["password"])
        p1_id = checkout_products["p1_id"]
        _add_to_cart(client, p1_id, 3)

        client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)

        with app.app_context():
            from app.extensions import db
            order = db.session.execute(
                db.select(Order).where(Order.user_id == checkout_user["id"])
            ).scalar_one_or_none()
            assert order is not None
            assert len(order.items) == 1

            oi = order.items[0]
            assert oi.product_id == p1_id
            assert oi.quantity == 3
            # Price snapshot must equal the product price at checkout time
            assert Decimal(str(oi.unit_price)) == Decimal("100.00")

    def test_successful_checkout_clears_cart(self, client, app, checkout_user, checkout_products):
        """After a successful checkout, the user's cart is empty."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 1)
        _add_to_cart(client, checkout_products["p2_id"], 2)

        client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)

        with app.app_context():
            from app.extensions import db
            count = db.session.execute(
                db.select(CartItem).where(CartItem.user_id == checkout_user["id"])
            ).scalars().all()
            assert len(count) == 0

    def test_successful_checkout_updates_reserved_quantity(
        self, client, app, checkout_user, checkout_products
    ):
        """Checkout increments inventory.reserved_quantity by the cart quantity."""
        _login(client, checkout_user["email"], checkout_user["password"])
        p1_id = checkout_products["p1_id"]
        _add_to_cart(client, p1_id, 4)

        client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)

        with app.app_context():
            from app.extensions import db
            inv = db.session.execute(
                db.select(Inventory).where(Inventory.product_id == p1_id)
            ).scalar_one()
            # Started with reserved=0, added 4 → should be 4
            assert inv.reserved_quantity == 4
            # quantity (total stock) should be unchanged
            assert inv.quantity == 10

    def test_successful_checkout_redirects_to_confirmation(
        self, client, checkout_user, checkout_products
    ):
        """After checkout, the user is redirected to the confirmation page."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 1)

        response = client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Order Placed Successfully" in content or "confirmed" in content.lower() or "Order #" in content

    def test_confirmation_page_shows_order_details(
        self, client, app, checkout_user, checkout_products
    ):
        """The confirmation page renders order ID, items, and total."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 2)

        client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)

        with app.app_context():
            from app.extensions import db
            order = db.session.execute(
                db.select(Order).where(Order.user_id == checkout_user["id"])
            ).scalar_one()
            order_id = order.id

        response = client.get(f"/orders/confirmation/{order_id}")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert f"#{order_id}" in content
        assert "Test Widget A" in content
        assert "₹200.00" in content  # 2 * 100.00

    def test_successful_checkout_persists_shipping_details(
        self, client, app, checkout_user, checkout_products
    ):
        """Checkout persists trimmed customer and shipping details on the Order model."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 1)

        custom_form = {
            "full_name": "  Priya Sharma  ",
            "email": "  priya@example.com  ",
            "phone": "  9876543210  ",
            "address_line1": "  Flat 402, Lotus Towers  ",
            "address_line2": "  Near Metro Station  ",
            "city": "  Bengaluru  ",
            "state": "  Karnataka  ",
            "pincode": "  560001  ",
            "notes": "  Please call before delivery  ",
        }

        response = client.post("/orders/checkout", data=custom_form, follow_redirects=True)
        assert response.status_code == 200

        with app.app_context():
            from app.extensions import db
            order = db.session.execute(
                db.select(Order).where(Order.user_id == checkout_user["id"])
            ).scalar_one()

            assert order.shipping_name == "Priya Sharma"
            assert order.shipping_email == "priya@example.com"
            assert order.shipping_phone == "9876543210"
            assert order.shipping_address_line1 == "Flat 402, Lotus Towers"
            assert order.shipping_address_line2 == "Near Metro Station"
            assert order.shipping_city == "Bengaluru"
            assert order.shipping_state == "Karnataka"
            assert order.shipping_pincode == "560001"
            assert order.notes == "Please call before delivery"

    def test_confirmation_page_displays_shipping_details(
        self, client, app, checkout_user, checkout_products
    ):
        """Order confirmation page renders saved delivery details."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 1)

        custom_form = {
            "full_name": "Aarav Patel",
            "email": "aarav@example.com",
            "phone": "9123456780",
            "address_line1": "100 Marine Drive",
            "address_line2": "Floor 5",
            "city": "Mumbai",
            "state": "Maharashtra",
            "pincode": "400020",
            "notes": "Fragile items",
        }

        client.post("/orders/checkout", data=custom_form, follow_redirects=True)

        with app.app_context():
            from app.extensions import db
            order = db.session.execute(
                db.select(Order).where(Order.user_id == checkout_user["id"])
            ).scalar_one()
            order_id = order.id

        response = client.get(f"/orders/confirmation/{order_id}")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Delivery Details" in content
        assert "Aarav Patel" in content
        assert "9123456780" in content
        assert "aarav@example.com" in content
        assert "100 Marine Drive" in content
        assert "Floor 5" in content
        assert "Mumbai" in content
        assert "400020" in content


class TestCheckoutStockValidation:

    def test_checkout_insufficient_stock_rejected(
        self, client, app, checkout_user, checkout_products
    ):
        """If stock drops to 0 after adding to cart, checkout is rejected."""
        _login(client, checkout_user["email"], checkout_user["password"])
        p1_id = checkout_products["p1_id"]
        _add_to_cart(client, p1_id, 2)

        # Deplete stock so available = 0 (reserve all 10 units)
        with app.app_context():
            from app.extensions import db
            inv = db.session.execute(
                db.select(Inventory).where(Inventory.product_id == p1_id)
            ).scalar_one()
            inv.reserved_quantity = 10  # quantity=10, reserved=10 → available=0
            db.session.commit()

        response = client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)
        content = response.data.decode("utf-8")
        # Should flash an error about insufficient stock and NOT create an order
        assert "stock" in content.lower() or "unavailable" in content.lower() or "insufficient" in content.lower()

        with app.app_context():
            from app.extensions import db
            order = db.session.execute(
                db.select(Order).where(Order.user_id == checkout_user["id"])
            ).scalar_one_or_none()
            assert order is None

    def test_checkout_inactive_product_rejected(
        self, client, app, checkout_user, checkout_products
    ):
        """If a product becomes inactive after cart add, checkout is rejected."""
        _login(client, checkout_user["email"], checkout_user["password"])
        p2_id = checkout_products["p2_id"]
        _add_to_cart(client, p2_id, 1)

        # Deactivate the product
        with app.app_context():
            from app.extensions import db
            product = db.session.get(Product, p2_id)
            product.is_active = False
            db.session.commit()

        response = client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)
        content = response.data.decode("utf-8")
        assert "available" in content.lower() or "cart" in content.lower()

        with app.app_context():
            from app.extensions import db
            order = db.session.execute(
                db.select(Order).where(Order.user_id == checkout_user["id"])
            ).scalar_one_or_none()
            assert order is None


class TestCheckoutOrderIsolation:

    def test_user_cannot_view_other_users_confirmation(
        self, client, app, checkout_user, checkout_user2, checkout_products
    ):
        """User B gets 403 when trying to view User A's order confirmation."""
        # User A places an order
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 1)
        client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)

        with app.app_context():
            from app.extensions import db
            order = db.session.execute(
                db.select(Order).where(Order.user_id == checkout_user["id"])
            ).scalar_one()
            order_id = order.id

        # Log out User A, log in User B
        client.get("/auth/logout", follow_redirects=True)
        _login(client, checkout_user2["email"], checkout_user2["password"])

        response = client.get(f"/orders/confirmation/{order_id}")
        assert response.status_code == 403


class TestCheckoutDuplicatePrevention:

    def test_empty_cart_after_order_prevents_duplicate(
        self, client, checkout_user, checkout_products
    ):
        """After a successful checkout, cart is empty so a second checkout attempt redirects."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 1)

        # First checkout — should succeed
        response = client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)
        assert response.status_code == 200

        # Second attempt — cart is now empty, should redirect back to cart
        response2 = client.get("/orders/checkout", follow_redirects=True)
        content2 = response2.data.decode("utf-8")
        assert "empty" in content2.lower() or "cart" in content2.lower()


class TestOrderList:

    def test_order_list_shows_user_orders(
        self, client, app, checkout_user, checkout_products
    ):
        """GET /orders/ shows the user's orders after placing one."""
        _login(client, checkout_user["email"], checkout_user["password"])
        _add_to_cart(client, checkout_products["p1_id"], 1)
        client.post("/orders/checkout", data=VALID_FORM, follow_redirects=True)

        response = client.get("/orders/")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "My Orders" in content
        assert "PENDING" in content

    def test_order_list_unauthenticated_redirects(self, client):
        """GET /orders/ without login redirects to login."""
        response = client.get("/orders/", follow_redirects=False)
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]
