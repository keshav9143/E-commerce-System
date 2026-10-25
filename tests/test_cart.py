"""
tests/test_cart.py — Unit and integration tests for Shopping Cart operations (Phase 3, Step 2).

Test coverage:
  1. Adding an item to the cart (authenticated)
  2. Adding the same product again (increments existing row, prevents duplicates)
  3. Viewing the cart and server-side subtotal calculation
  4. Updating item quantities
  5. Removing an item from the cart
  6. Rejecting invalid quantities (zero, negative, non-numeric, exceeding available stock)
  7. Handling inactive and nonexistent products
  8. Enforcing stock limits (accounting for reserved inventory)
  9. Authentication enforcement and cross-user isolation
 10. Displaying prices and totals with the INR (₹) filter
"""

from decimal import Decimal
import pytest

from app.models import CartItem, Inventory, Product, User, UserRole


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def test_users(app, db):
    """Create two customers to test cart isolation."""
    with app.app_context():
        from app.extensions import bcrypt
        user1 = User(username="customer_one", email="one@example.com", role=UserRole.CUSTOMER.value)
        user1.set_password("Password123!", bcrypt)

        user2 = User(username="customer_two", email="two@example.com", role=UserRole.CUSTOMER.value)
        user2.set_password("Password123!", bcrypt)

        db.session.add_all([user1, user2])
        db.session.commit()

        return {
            "user1_id": user1.id,
            "user2_id": user2.id,
        }


@pytest.fixture
def test_products(app, db):
    """Create products with varying stock and active states."""
    with app.app_context():
        # p1: In stock (10 available)
        p1 = Product(
            name="Wireless Headphones",
            description="Noise-cancelling over-ear headphones.",
            price=Decimal("149.99"),
            category="Audio",
            sku="AU-HP-01",
            is_active=True,
        )
        inv1 = Inventory(product=p1, quantity=12, reserved_quantity=2)  # 10 available

        # p2: Low stock (3 available, physical 10, reserved 7)
        p2 = Product(
            name="Mechanical Numpad",
            description="Dedicated 21-key keypad.",
            price=Decimal("39.50"),
            category="Peripherals",
            sku="KB-NUM-02",
            is_active=True,
        )
        inv2 = Inventory(product=p2, quantity=10, reserved_quantity=7)  # 3 available

        # p3: Out of stock (0 available)
        p3 = Product(
            name="Sold Out Adapter",
            description="Currently unavailable adapter.",
            price=Decimal("19.99"),
            category="Accessories",
            sku="AC-ADP-03",
            is_active=True,
        )
        inv3 = Inventory(product=p3, quantity=5, reserved_quantity=5)   # 0 available

        # p4: Inactive product
        p4 = Product(
            name="Discontinued Item",
            description="Old product.",
            price=Decimal("9.99"),
            category="Legacy",
            sku="LG-DISC-04",
            is_active=False,
        )
        inv4 = Inventory(product=p4, quantity=10, reserved_quantity=0)

        db.session.add_all([p1, inv1, p2, inv2, p3, inv3, p4, inv4])
        db.session.commit()

        return {
            "p1_id": p1.id,
            "p2_id": p2.id,
            "p3_id": p3.id,
            "p4_id": p4.id,
        }


# ── Test Cases ───────────────────────────────────────────────────────────────

class TestCartAuthenticationAndAccess:

    def test_unauthenticated_cart_access_redirects_to_login(self, client):
        """Unauthenticated GET to /shop/cart redirects to login page."""
        response = client.get("/shop/cart", follow_redirects=False)
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_unauthenticated_add_to_cart_redirects_to_login(self, client, test_products):
        """Unauthenticated POST to /shop/cart/add redirects to login page."""
        response = client.post(
            "/shop/cart/add",
            data={"product_id": test_products["p1_id"], "quantity": 1},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_authenticated_customer_sees_empty_cart(self, client, test_users):
        """Authenticated customer with empty cart sees empty cart message."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        response = client.get("/shop/cart")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Your shopping cart is empty" in content
        assert "Explore Products" in content


class TestAddToCart:

    def test_add_item_to_cart_success(self, client, test_users, test_products):
        """Customer successfully adds an available product to cart."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        p1_id = test_products["p1_id"]
        response = client.post(
            "/shop/cart/add",
            data={"product_id": p1_id, "quantity": 2},
            follow_redirects=True,
        )
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Wireless Headphones" in content
        assert "AU-HP-01" in content
        assert "₹299.98" in content  # 2 * 149.99

        # Verify DB persistence
        item = CartItem.query.filter_by(user_id=test_users["user1_id"], product_id=p1_id).first()
        assert item is not None
        assert item.quantity == 2

    def test_add_same_product_increments_quantity(self, client, test_users, test_products):
        """Adding the same product again updates the existing CartItem row without creating duplicates."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        p1_id = test_products["p1_id"]

        # First add: 2 units
        client.post("/shop/cart/add", data={"product_id": p1_id, "quantity": 2}, follow_redirects=True)

        # Second add: 3 more units
        response = client.post("/shop/cart/add", data={"product_id": p1_id, "quantity": 3}, follow_redirects=True)
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Updated" in content and "Wireless Headphones" in content and "to 5" in content

        # Verify exactly one CartItem row exists with total quantity 5
        items = CartItem.query.filter_by(user_id=test_users["user1_id"], product_id=p1_id).all()
        assert len(items) == 1
        assert items[0].quantity == 5

    def test_add_exceeding_stock_rejected(self, client, test_users, test_products):
        """Attempting to add more than available stock is rejected."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        p2_id = test_products["p2_id"]  # 3 available

        # Request 4 units (exceeds 3 available)
        response = client.post(
            "/shop/cart/add",
            data={"product_id": p2_id, "quantity": 4},
            follow_redirects=True,
        )
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Only 3 units are available in stock" in content

        # Verify nothing was added
        assert CartItem.query.filter_by(user_id=test_users["user1_id"], product_id=p2_id).first() is None

    def test_add_cumulative_quantity_exceeding_stock_rejected(self, client, test_users, test_products):
        """Adding an item that pushes cumulative cart quantity above available stock is rejected."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        p2_id = test_products["p2_id"]  # 3 available

        # Add 2 units (valid)
        client.post("/shop/cart/add", data={"product_id": p2_id, "quantity": 2}, follow_redirects=True)

        # Try to add 2 more units (2 + 2 = 4 > 3 available)
        response = client.post("/shop/cart/add", data={"product_id": p2_id, "quantity": 2}, follow_redirects=True)
        content = response.data.decode("utf-8")
        assert "Cannot add 2 more. You have 2 in your cart, and only 3 units are available" in content

        # Quantity should remain 2
        item = CartItem.query.filter_by(user_id=test_users["user1_id"], product_id=p2_id).first()
        assert item.quantity == 2

    def test_add_out_of_stock_rejected(self, client, test_users, test_products):
        """Adding a product with 0 available stock is rejected."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        p3_id = test_products["p3_id"]  # 0 available
        response = client.post(
            "/shop/cart/add",
            data={"product_id": p3_id, "quantity": 1},
            follow_redirects=True,
        )
        content = response.data.decode("utf-8")
        assert "out of stock" in content.lower()
        assert CartItem.query.filter_by(user_id=test_users["user1_id"], product_id=p3_id).first() is None

    def test_add_inactive_or_nonexistent_product_rejected(self, client, test_users, test_products):
        """Adding an inactive or nonexistent product returns an error."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        # Nonexistent
        r1 = client.post("/shop/cart/add", data={"product_id": 99999, "quantity": 1}, follow_redirects=True)
        assert "not available" in r1.data.decode("utf-8").lower()

        # Inactive
        p4_id = test_products["p4_id"]
        r2 = client.post("/shop/cart/add", data={"product_id": p4_id, "quantity": 1}, follow_redirects=True)
        assert "not available" in r2.data.decode("utf-8").lower()

    def test_add_invalid_quantities_rejected(self, client, test_users, test_products):
        """Submitting 0, negative, or malformed quantities is rejected."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        p1_id = test_products["p1_id"]

        for invalid_qty in [0, -1, "abc", ""]:
            response = client.post(
                "/shop/cart/add",
                data={"product_id": p1_id, "quantity": invalid_qty},
                follow_redirects=True,
            )
            assert response.status_code == 200
            # Ensure no item was created
            assert CartItem.query.filter_by(user_id=test_users["user1_id"]).count() == 0


class TestCartViewAndSubtotal:

    def test_subtotal_calculation_and_inr_rendering(self, client, test_users, test_products):
        """Verify multiple cart items compute correct line totals and subtotal with INR formatting."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        # p1: 149.99 * 2 = 299.98
        client.post("/shop/cart/add", data={"product_id": test_products["p1_id"], "quantity": 2}, follow_redirects=True)
        # p2: 39.50 * 1 = 39.50
        client.post("/shop/cart/add", data={"product_id": test_products["p2_id"], "quantity": 1}, follow_redirects=True)

        # Expected subtotal: 299.98 + 39.50 = 339.48 -> ₹339.48
        response = client.get("/shop/cart")
        assert response.status_code == 200
        content = response.data.decode("utf-8")

        assert "₹149.99" in content
        assert "₹299.98" in content
        assert "₹39.50" in content
        assert "₹339.48" in content


class TestUpdateCartItem:

    def test_update_quantity_success(self, client, test_users, test_products):
        """Customer can update an item's quantity in their cart."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        p1_id = test_products["p1_id"]
        client.post("/shop/cart/add", data={"product_id": p1_id, "quantity": 1}, follow_redirects=True)

        item = CartItem.query.filter_by(user_id=test_users["user1_id"], product_id=p1_id).first()
        assert item.quantity == 1

        # Update to 4
        response = client.post(
            "/shop/cart/update",
            data={"cart_item_id": item.id, "quantity": 4},
            follow_redirects=True,
        )
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Wireless Headphones" in content and "to 4" in content

        # Check DB
        from app.extensions import db
        db_item = db.session.get(CartItem, item.id)
        assert db_item.quantity == 4

    def test_update_quantity_exceeding_stock_rejected(self, client, test_users, test_products):
        """Updating quantity beyond available stock is rejected."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        p2_id = test_products["p2_id"]  # 3 available
        client.post("/shop/cart/add", data={"product_id": p2_id, "quantity": 1}, follow_redirects=True)
        item = CartItem.query.filter_by(user_id=test_users["user1_id"], product_id=p2_id).first()

        response = client.post(
            "/shop/cart/update",
            data={"cart_item_id": item.id, "quantity": 10},
            follow_redirects=True,
        )
        assert "Only 3 units are available in stock" in response.data.decode("utf-8")
        from app.extensions import db
        assert db.session.get(CartItem, item.id).quantity == 1

    def test_update_quantity_invalid_values(self, client, test_users, test_products):
        """Updating quantity to 0 or negative is rejected."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        p1_id = test_products["p1_id"]
        client.post("/shop/cart/add", data={"product_id": p1_id, "quantity": 2}, follow_redirects=True)
        item = CartItem.query.filter_by(user_id=test_users["user1_id"], product_id=p1_id).first()

        # Update to 0
        response = client.post(
            "/shop/cart/update",
            data={"cart_item_id": item.id, "quantity": 0},
            follow_redirects=True,
        )
        assert "Quantity must be at least 1" in response.data.decode("utf-8")
        from app.extensions import db
        assert db.session.get(CartItem, item.id).quantity == 2


class TestRemoveCartItem:

    def test_remove_cart_item_success(self, client, test_users, test_products):
        """Customer can remove an item from their cart."""
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        p1_id = test_products["p1_id"]
        client.post("/shop/cart/add", data={"product_id": p1_id, "quantity": 2}, follow_redirects=True)
        item = CartItem.query.filter_by(user_id=test_users["user1_id"], product_id=p1_id).first()

        response = client.post(
            "/shop/cart/remove",
            data={"cart_item_id": item.id},
            follow_redirects=True,
        )
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Removed" in content and "Wireless Headphones" in content
        assert "Your shopping cart is empty" in content
        from app.extensions import db
        assert db.session.get(CartItem, item.id) is None


class TestCustomerCartIsolation:

    def test_user_cannot_view_or_modify_other_user_cart(self, client, test_users, test_products):
        """User A cannot view, update, or delete User B's cart items."""
        # 1. User 1 adds an item
        client.post(
            "/auth/login",
            data={"email": "one@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )
        client.post("/shop/cart/add", data={"product_id": test_products["p1_id"], "quantity": 2}, follow_redirects=True)
        user1_item = CartItem.query.filter_by(user_id=test_users["user1_id"]).first()
        assert user1_item is not None

        # Log out User 1
        client.get("/auth/logout", follow_redirects=True)

        # 2. User 2 logs in
        client.post(
            "/auth/login",
            data={"email": "two@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )

        # User 2 views their cart: should be empty, not showing User 1's items
        r_view = client.get("/shop/cart")
        assert "Your shopping cart is empty" in r_view.data.decode("utf-8")
        assert "Wireless Headphones" not in r_view.data.decode("utf-8")

        # User 2 attempts to maliciously update User 1's cart item ID
        r_update = client.post(
            "/shop/cart/update",
            data={"cart_item_id": user1_item.id, "quantity": 5},
            follow_redirects=True,
        )
        assert "Cart item not found" in r_update.data.decode("utf-8")
        # Ensure User 1's item quantity was NOT modified
        from app.extensions import db
        assert db.session.get(CartItem, user1_item.id).quantity == 2

        # User 2 attempts to maliciously delete User 1's cart item ID
        r_remove = client.post(
            "/shop/cart/remove",
            data={"cart_item_id": user1_item.id},
            follow_redirects=True,
        )
        assert "Cart item not found" in r_remove.data.decode("utf-8")
        # Ensure User 1's item still exists
        assert db.session.get(CartItem, user1_item.id) is not None
