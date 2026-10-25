"""
app/shop/routes.py — Product catalogue, detail views, and shopping cart operations.

Features:
  - Catalogue listing with search, category filtering, and pagination
  - Product detail page with live stock display and Add-to-Cart form
  - Shopping cart views and actions (view, add, update quantity, remove)
  - Strict customer-level cart isolation (only current_user.id cart items accessible)
  - Duplicate prevention via upsert logic conforming to uq_cart_user_product
  - Server-side stock boundary and positive quantity validation
  - Server-side Decimal price calculations (no client-trusted amounts)
"""

from decimal import Decimal
import logging

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from app.extensions import db
from app.models import CartItem, Inventory, Product
from app.shop import shop_bp
from app.shop.forms import AddToCartForm, RemoveCartItemForm, UpdateCartItemForm

logger = logging.getLogger(__name__)


@shop_bp.route("/")
def catalogue():
    """
    Render the product catalogue.

    Query parameters:
      q        — Search term (matches Product.name or Product.description)
      category — Category filter (exact match on Product.category)
      page     — Page number for pagination (defaults to 1)
    """
    search_query = request.args.get("q", "").strip()
    category_filter = request.args.get("category", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = current_app.config.get("PRODUCTS_PER_PAGE", 12)

    # Base query: only active products, eagerly joining inventory for stock calculation
    query = Product.query.filter_by(is_active=True).outerjoin(Inventory)

    # Apply search filter across name, description, and SKU if provided
    if search_query:
        like_pattern = f"%{search_query}%"
        query = query.filter(
            or_(
                Product.name.ilike(like_pattern),
                Product.description.ilike(like_pattern),
                Product.sku.ilike(like_pattern),
            )
        )

    # Apply category filter if provided
    if category_filter:
        query = query.filter(Product.category == category_filter)

    # Order newest first for consistent pagination
    query = query.order_by(Product.created_at.desc(), Product.id.desc())

    # Execute pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    products = pagination.items

    # Fetch all distinct categories for active products to populate the filter sidebar
    categories_query = (
        Product.query.with_entities(Product.category)
        .filter(Product.is_active == True, Product.category.isnot(None))  # noqa: E712
        .distinct()
        .order_by(Product.category)
        .all()
    )
    categories = [cat[0] for cat in categories_query if cat[0]]

    # Total active products in catalogue (for checking if catalogue is completely empty)
    total_active_products = Product.query.filter_by(is_active=True).count()

    return render_template(
        "shop/catalogue.html",
        products=products,
        pagination=pagination,
        categories=categories,
        current_category=category_filter,
        search_query=search_query,
        total_active_products=total_active_products,
        title="Product Catalogue",
    )


@shop_bp.route("/product/<int:product_id>")
def product_detail(product_id: int):
    """
    Render the detail page for a single product.

    Returns 404 if the product does not exist, or if the product is inactive
    and the user is not an administrator.
    """
    product = db.session.get(Product, product_id)
    if not product:
        abort(404)

    # Inactive products are not visible to regular customers or unauthenticated guests
    if not product.is_active:
        is_admin = current_user.is_authenticated and current_user.is_admin
        if not is_admin:
            abort(404)

    # Compute available stock from associated inventory record
    available_stock = product.available_stock
    form = AddToCartForm(product_id=product.id)

    return render_template(
        "shop/detail.html",
        product=product,
        available_stock=available_stock,
        form=form,
        title=product.name,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shopping Cart Routes (Phase 3, Step 2)
# ─────────────────────────────────────────────────────────────────────────────

@shop_bp.route("/cart", methods=["GET"])
@login_required
def view_cart():
    """
    Display the authenticated customer's shopping cart.

    Prices and subtotals are calculated strictly on the server using Decimal.
    """
    cart_items = (
        CartItem.query.filter_by(user_id=current_user.id)
        .order_by(CartItem.added_at.asc())
        .all()
    )

    # Server-side subtotal calculation
    subtotal = sum(item.line_total for item in cart_items) if cart_items else Decimal("0.00")

    update_form = UpdateCartItemForm()
    remove_form = RemoveCartItemForm()

    return render_template(
        "shop/cart.html",
        cart_items=cart_items,
        subtotal=subtotal,
        update_form=update_form,
        remove_form=remove_form,
        title="Shopping Cart",
    )


@shop_bp.route("/cart/add", methods=["POST"])
@login_required
def add_to_cart():
    """
    Add a product to the authenticated user's cart or increment its quantity.

    Validates:
      - Product existence and active state
      - Positive integer quantity
      - Stock availability (quantity <= available_stock)
      - Existing row upsert to prevent duplicate uq_cart_user_product rows
    """
    form = AddToCartForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{field.capitalize()}: {error}", "danger")
        # Redirect back to referring page or catalogue
        return redirect(request.referrer or url_for("shop.catalogue"))

    product_id = form.product_id.data
    try:
        product_id = int(product_id)
        quantity = int(form.quantity.data)
    except (TypeError, ValueError):
        flash("Invalid product ID or quantity.", "danger")
        return redirect(url_for("shop.catalogue"))

    if quantity < 1:
        flash("Quantity must be a positive integer.", "danger")
        return redirect(url_for("shop.product_detail", product_id=product_id))

    product = db.session.get(Product, product_id)
    if not product or not product.is_active:
        flash("The selected product is not available.", "danger")
        return redirect(url_for("shop.catalogue"))

    available = product.available_stock
    if available <= 0:
        flash(f"Sorry, '{product.name}' is currently out of stock.", "warning")
        return redirect(url_for("shop.product_detail", product_id=product_id))

    # Check for existing cart item for this user & product (upsert logic)
    cart_item = CartItem.query.filter_by(
        user_id=current_user.id, product_id=product.id
    ).first()

    if cart_item:
        new_quantity = cart_item.quantity + quantity
        if new_quantity > available:
            flash(
                f"Cannot add {quantity} more. You have {cart_item.quantity} in your cart, "
                f"and only {available} units are available in stock.",
                "warning",
            )
            return redirect(url_for("shop.product_detail", product_id=product_id))

        cart_item.quantity = new_quantity
        logger.info(
            "Updated cart item: user_id=%s product_id=%s new_quantity=%s",
            current_user.id, product.id, cart_item.quantity,
        )
        flash(f"Updated '{product.name}' quantity in your cart to {cart_item.quantity}.", "success")
    else:
        if quantity > available:
            flash(
                f"Cannot add {quantity} units. Only {available} units are available in stock.",
                "warning",
            )
            return redirect(url_for("shop.product_detail", product_id=product_id))

        cart_item = CartItem(
            user_id=current_user.id, product_id=product.id, quantity=quantity
        )
        db.session.add(cart_item)
        logger.info(
            "Created cart item: user_id=%s product_id=%s quantity=%s",
            current_user.id, product.id, quantity,
        )
        flash(f"Added '{product.name}' to your cart.", "success")

    db.session.commit()
    return redirect(url_for("shop.view_cart"))


@shop_bp.route("/cart/update", methods=["POST"])
@login_required
def update_cart_item():
    """
    Update the quantity of an item in the authenticated user's cart.

    Validates:
      - Ownership: item belongs to current_user.id
      - Positive integer quantity
      - Stock boundary
    """
    form = UpdateCartItemForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{field.capitalize()}: {error}", "danger")
        return redirect(url_for("shop.view_cart"))

    try:
        cart_item_id = int(form.cart_item_id.data)
        quantity = int(form.quantity.data)
    except (TypeError, ValueError):
        flash("Invalid cart item or quantity provided.", "danger")
        return redirect(url_for("shop.view_cart"))

    if quantity < 1:
        flash("Quantity must be at least 1. Use remove to delete an item.", "danger")
        return redirect(url_for("shop.view_cart"))

    # Strict customer isolation: query must match user_id
    cart_item = CartItem.query.filter_by(
        id=cart_item_id, user_id=current_user.id
    ).first()

    if not cart_item:
        flash("Cart item not found.", "danger")
        return redirect(url_for("shop.view_cart"))

    product = cart_item.product
    if not product or not product.is_active:
        flash("This product is no longer active and cannot be updated.", "danger")
        return redirect(url_for("shop.view_cart"))

    available = product.available_stock
    if quantity > available:
        flash(
            f"Cannot set quantity to {quantity}. Only {available} units are available in stock.",
            "warning",
        )
        return redirect(url_for("shop.view_cart"))

    cart_item.quantity = quantity
    db.session.commit()
    logger.info(
        "Cart item updated: id=%s user_id=%s quantity=%s",
        cart_item.id, current_user.id, quantity,
    )
    flash(f"Updated quantity for '{product.name}' to {quantity}.", "success")
    return redirect(url_for("shop.view_cart"))


@shop_bp.route("/cart/remove", methods=["POST"])
@login_required
def remove_cart_item():
    """
    Remove an item from the authenticated user's cart.

    Validates:
      - Ownership: item belongs to current_user.id
    """
    form = RemoveCartItemForm()
    if not form.validate_on_submit():
        flash("Invalid request token or parameters.", "danger")
        return redirect(url_for("shop.view_cart"))

    try:
        cart_item_id = int(form.cart_item_id.data)
    except (TypeError, ValueError):
        flash("Invalid cart item identifier.", "danger")
        return redirect(url_for("shop.view_cart"))

    # Strict customer isolation: query must match user_id
    cart_item = CartItem.query.filter_by(
        id=cart_item_id, user_id=current_user.id
    ).first()

    if not cart_item:
        flash("Cart item not found.", "danger")
        return redirect(url_for("shop.view_cart"))

    product_name = cart_item.product.name if cart_item.product else "Item"
    db.session.delete(cart_item)
    db.session.commit()
    logger.info(
        "Cart item removed: id=%s user_id=%s product=%s",
        cart_item_id, current_user.id, product_name,
    )
    flash(f"Removed '{product_name}' from your cart.", "info")
    return redirect(url_for("shop.view_cart"))
