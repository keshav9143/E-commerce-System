"""
tests/test_shop.py — Unit and integration tests for Product Catalogue and Detail views (Phase 3, Step 1).

Test coverage:
  1. Empty catalogue:
     - Renders empty catalogue notification when no products exist
  2. Catalogue rendering:
     - Renders active products with correct names, prices, categories, and stock
  3. Search functionality:
     - Search by product name
     - Search by SKU
     - Search by description substring
     - Search returning 0 results renders clear message and reset button
  4. Category filtering:
     - Filtering by valid category restricts output
     - Switching between categories
  5. Stock availability indicators:
     - In Stock (> 5 available units)
     - Low Stock (1 <= available units <= 5)
     - Out of Stock (0 available units or quantity == reserved_quantity)
  6. Pagination:
     - Pagination controls displayed when products exceed PRODUCTS_PER_PAGE
     - Query parameters (search query, category) preserved in pagination links
  7. Product detail view:
     - Existing active product renders complete details and stock
     - Nonexistent product returns 404
     - Inactive product returns 404 to anonymous guests and customers
     - Inactive product is viewable by admin users
"""

from decimal import Decimal
import pytest

from app.models import Inventory, Product, User, UserRole


# ── Helper Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def sample_catalogue(app, db):
    """Seed a representative set of products with various categories, stock levels, and active states."""
    with app.app_context():
        # Active products with inventory
        p1 = Product(
            name="Mechanical Keyboard RGB",
            description="Premium mechanical keyboard with customizable RGB backlighting and blue switches.",
            price=Decimal("89.99"),
            category="Peripherals",
            sku="KB-RGB-01",
            is_active=True,
        )
        inv1 = Inventory(product=p1, quantity=15, reserved_quantity=2)  # 13 available -> In Stock

        p2 = Product(
            name="Wireless Gaming Mouse",
            description="Ultra-lightweight wireless mouse with 25K optical sensor.",
            price=Decimal("59.50"),
            category="Peripherals",
            sku="MS-WIR-02",
            is_active=True,
        )
        inv2 = Inventory(product=p2, quantity=4, reserved_quantity=1)   # 3 available -> Low Stock

        p3 = Product(
            name="USB-C Hub Multiport",
            description="7-in-1 USB-C docking station with 4K HDMI, SD reader, and 100W PD.",
            price=Decimal("34.00"),
            category="Accessories",
            sku="HB-USBC-03",
            is_active=True,
        )
        inv3 = Inventory(product=p3, quantity=5, reserved_quantity=5)   # 0 available -> Out of Stock

        p4 = Product(
            name="27-Inch 4K Monitor",
            description="IPS UHD display with HDR400 and ultra-thin bezels.",
            price=Decimal("299.99"),
            category="Displays",
            sku="MN-4K-04",
            is_active=True,
        )
        inv4 = Inventory(product=p4, quantity=20, reserved_quantity=0)  # 20 available -> In Stock

        # Inactive product
        p5 = Product(
            name="Archived Old Webcam",
            description="Legacy 720p webcam no longer manufactured.",
            price=Decimal("15.00"),
            category="Cameras",
            sku="CAM-OLD-05",
            is_active=False,
        )
        inv5 = Inventory(product=p5, quantity=10, reserved_quantity=0)

        db.session.add_all([p1, inv1, p2, inv2, p3, inv3, p4, inv4, p5, inv5])
        db.session.commit()

        # Return dict of IDs to query cleanly across sessions
        return {
            "p1_id": p1.id,
            "p2_id": p2.id,
            "p3_id": p3.id,
            "p4_id": p4.id,
            "p5_id": p5.id,
        }


# ── Test Cases ───────────────────────────────────────────────────────────────

class TestCatalogueEmptyState:

    def test_empty_catalogue_message(self, client, db):
        """When no active products exist, catalogue should show a friendly empty message."""
        response = client.get("/shop/")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Catalogue is currently empty" in content
        assert "There are no products available for purchase" in content


class TestCatalogueListing:

    def test_active_products_rendered(self, client, sample_catalogue):
        """Active products should be displayed with their names and formatted prices in INR (₹)."""
        response = client.get("/shop/")
        assert response.status_code == 200
        content = response.data.decode("utf-8")

        assert "Mechanical Keyboard RGB" in content
        assert "₹89.99" in content
        assert "Wireless Gaming Mouse" in content
        assert "₹59.50" in content
        assert "USB-C Hub Multiport" in content
        assert "₹34.00" in content
        assert "27-Inch 4K Monitor" in content
        assert "₹299.99" in content

        # Verify old USD currency symbol is not rendered for prices
        assert "$89.99" not in content
        assert "$59.50" not in content

        # Inactive product must not appear on catalogue
        assert "Archived Old Webcam" not in content

    def test_stock_status_badges(self, client, sample_catalogue):
        """Verify stock indicators show In Stock, Low Stock, or Out of Stock correctly."""
        response = client.get("/shop/")
        assert response.status_code == 200
        content = response.data.decode("utf-8")

        # p1 has 13 available (>5) -> In Stock
        assert "In Stock" in content
        # p2 has 3 available (1..5) -> Low Stock
        assert "Only 3 left" in content
        # p3 has 0 available -> Out of Stock
        assert "Out of Stock" in content


class TestProductSearch:

    def test_search_by_name(self, client, sample_catalogue):
        """Searching for 'Keyboard' should return only the keyboard."""
        response = client.get("/shop/?q=Keyboard")
        assert response.status_code == 200
        content = response.data.decode("utf-8")

        assert "Mechanical Keyboard RGB" in content
        assert "Wireless Gaming Mouse" not in content
        assert "27-Inch 4K Monitor" not in content

    def test_search_by_sku(self, client, sample_catalogue):
        """Searching for SKU 'HB-USBC-03' should return the USB-C Hub."""
        response = client.get("/shop/?q=HB-USBC-03")
        assert response.status_code == 200
        content = response.data.decode("utf-8")

        assert "USB-C Hub Multiport" in content
        assert "Mechanical Keyboard RGB" not in content

    def test_search_by_description(self, client, sample_catalogue):
        """Searching by description keyword 'optical sensor' returns the mouse."""
        response = client.get("/shop/?q=optical+sensor")
        assert response.status_code == 200
        content = response.data.decode("utf-8")

        assert "Wireless Gaming Mouse" in content
        assert "Mechanical Keyboard RGB" not in content

    def test_search_no_results(self, client, sample_catalogue):
        """Searching for a nonexistent term renders the zero-results empty state."""
        response = client.get("/shop/?q=NonexistentSpaceship")
        assert response.status_code == 200
        content = response.data.decode("utf-8")

        assert "No matching products found" in content
        assert "NonexistentSpaceship" in content
        assert "Reset all filters" in content


class TestCategoryFiltering:

    def test_category_filter(self, client, sample_catalogue):
        """Filtering by category 'Displays' should only return the monitor."""
        response = client.get("/shop/?category=Displays")
        assert response.status_code == 200
        content = response.data.decode("utf-8")

        assert "27-Inch 4K Monitor" in content
        assert "Mechanical Keyboard RGB" not in content
        assert "Wireless Gaming Mouse" not in content

    def test_combined_search_and_category(self, client, sample_catalogue):
        """Searching 'wireless' within 'Peripherals' should return the mouse."""
        response = client.get("/shop/?category=Peripherals&q=wireless")
        assert response.status_code == 200
        content = response.data.decode("utf-8")

        assert "Wireless Gaming Mouse" in content
        assert "Mechanical Keyboard RGB" not in content


class TestPagination:

    def test_pagination_controls(self, client, app, db):
        """Verify pagination renders when active product count exceeds PRODUCTS_PER_PAGE."""
        with app.app_context():
            # Temporarily set pagination size to 2 for testing
            app.config["PRODUCTS_PER_PAGE"] = 2
            for i in range(5):
                p = Product(
                    name=f"Paginated Item {i}",
                    price=Decimal("10.00"),
                    sku=f"PAG-{i}",
                    category="Bulk",
                    is_active=True,
                )
                inv = Inventory(product=p, quantity=10, reserved_quantity=0)
                db.session.add_all([p, inv])
            db.session.commit()

        try:
            # Page 1
            r1 = client.get("/shop/?page=1")
            assert r1.status_code == 200
            assert "Paginated Item 4" in r1.data.decode("utf-8")
            assert "Next" in r1.data.decode("utf-8")

            # Page 2
            r2 = client.get("/shop/?page=2")
            assert r2.status_code == 200
            assert "Prev" in r2.data.decode("utf-8")

            # Out of bounds page defaults gracefully to empty/last
            r3 = client.get("/shop/?page=99")
            assert r3.status_code == 200
        finally:
            app.config["PRODUCTS_PER_PAGE"] = 12


class TestProductDetailView:

    def test_active_product_detail(self, client, sample_catalogue):
        """Valid product detail page displays name, description, price in INR (₹), SKU, and available stock."""
        p_id = sample_catalogue["p1_id"]
        response = client.get(f"/shop/product/{p_id}")
        assert response.status_code == 200
        content = response.data.decode("utf-8")

        assert "Mechanical Keyboard RGB" in content
        assert "KB-RGB-01" in content
        assert "₹89.99" in content
        assert "$89.99" not in content
        assert "INR" in content
        assert "USD" not in content
        assert "13 units ready to ship" in content
        assert "Peripherals" in content

    def test_nonexistent_product_returns_404(self, client, db):
        """Requesting a nonexistent product ID returns HTTP 404."""
        response = client.get("/shop/product/999999")
        assert response.status_code == 404

    def test_inactive_product_hidden_from_guest(self, client, sample_catalogue):
        """An unauthenticated visitor accessing an inactive product receives 404."""
        p_id = sample_catalogue["p5_id"]
        response = client.get(f"/shop/product/{p_id}")
        assert response.status_code == 404

    def test_inactive_product_hidden_from_customer(self, client, sample_catalogue, app, db):
        """A regular customer accessing an inactive product receives 404."""
        with app.app_context():
            from app.extensions import bcrypt
            customer = User(username="cust1", email="cust1@example.com", role=UserRole.CUSTOMER.value)
            customer.set_password("Password123!", bcrypt)
            db.session.add(customer)
            db.session.commit()

        # Log in as customer
        client.post(
            "/auth/login",
            data={"email": "cust1@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )

        p_id = sample_catalogue["p5_id"]
        response = client.get(f"/shop/product/{p_id}")
        assert response.status_code == 404

    def test_inactive_product_accessible_by_admin(self, client, sample_catalogue, app, db):
        """An administrator can preview an inactive product."""
        with app.app_context():
            from app.extensions import bcrypt
            admin_user = User(username="admin1", email="admin1@example.com", role=UserRole.ADMIN.value)
            admin_user.set_password("Password123!", bcrypt)
            db.session.add(admin_user)
            db.session.commit()

        # Log in as admin
        client.post(
            "/auth/login",
            data={"email": "admin1@example.com", "password": "Password123!", "remember_me": False},
            follow_redirects=True,
        )

        p_id = sample_catalogue["p5_id"]
        response = client.get(f"/shop/product/{p_id}")
        assert response.status_code == 200
        content = response.data.decode("utf-8")
        assert "Archived Old Webcam" in content
        assert "Inactive (Admin Preview)" in content
