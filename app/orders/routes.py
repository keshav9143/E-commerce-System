"""
app/orders/routes.py — Checkout and order-history routes.

Route summary
─────────────
  GET/POST /orders/checkout      — render the checkout form; on POST create an order
  GET      /orders/confirmation/<id> — order confirmation page (owner only)
  GET      /orders/             — list the current user's orders

Checkout transaction design
────────────────────────────
1. Load cart items; abort if empty.
2. Lock inventory rows in ascending product_id order (MySQL/PostgreSQL only).
   SQLite does not support SELECT … FOR UPDATE, so locking is skipped for it.
3. Validate stock server-side for each item.
4. Create Order (status=PENDING) + OrderItems (price snapshot).
5. Increment inventory.reserved_quantity by cart quantity (two-phase approach).
6. Delete all cart items for the user.
7. Single db.session.commit() — fully atomic.
8. POST-Redirect-GET to confirmation page prevents duplicate submissions.
"""

from decimal import Decimal

from flask import abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import CartItem, Inventory, Order, OrderItem, OrderStatus, Product

from . import orders_bp
from .forms import CheckoutForm


# ─────────────────────────────────────────────────────────────────────────────
# /orders/checkout
# ─────────────────────────────────────────────────────────────────────────────

@orders_bp.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    """Render and handle the checkout form."""
    # 1. Load the current user's cart items (with products eager-loaded).
    cart_items = (
        db.session.execute(
            db.select(CartItem)
            .where(CartItem.user_id == current_user.id)
            .order_by(CartItem.id)
        )
        .scalars()
        .all()
    )

    if not cart_items:
        flash("Your cart is empty. Please add items before checking out.", "warning")
        return redirect(url_for("shop.view_cart"))

    # 2. Build checkout form; pre-populate name/email from user profile.
    form = CheckoutForm()
    if not form.is_submitted():
        form.email.data = current_user.email
        # Use username as fallback for full_name (users table has no first/last name).
        form.full_name.data = current_user.username

    # 3. Compute order summary for the template.
    subtotal = sum(item.line_total for item in cart_items)

    if form.validate_on_submit():
        # ── Server-side stock validation + order creation ─────────────────
        try:
            # Sort product IDs ascending for consistent lock ordering (deadlock
            # prevention: every concurrent checkout acquires locks in the same
            # order, breaking the circular-wait Coffman condition).
            product_ids = sorted({item.product_id for item in cart_items})

            # Lock inventory rows (MySQL/PostgreSQL only; SQLite falls back).
            inventory_map: dict[int, Inventory] = {}
            for pid in product_ids:
                if db.engine.dialect.name != "sqlite":
                    inv = db.session.execute(
                        db.select(Inventory)
                        .where(Inventory.product_id == pid)
                        .with_for_update()
                    ).scalar_one_or_none()
                else:
                    # SQLite: no FOR UPDATE support — skip locking.
                    inv = db.session.get(Inventory, pid)
                if inv is None:
                    # No inventory row — treat as zero stock.
                    inv = Inventory(product_id=pid, quantity=0, reserved_quantity=0)
                inventory_map[pid] = inv

            # Validate stock for each cart item.
            for item in cart_items:
                product = db.session.get(Product, item.product_id)
                inv = inventory_map.get(item.product_id)

                if product is None or not product.is_active:
                    db.session.rollback()
                    flash(
                        f"Product is no longer available. Please update your cart.",
                        "danger",
                    )
                    return redirect(url_for("shop.view_cart"))

                available = inv.available_quantity if inv else 0
                if available < item.quantity:
                    db.session.rollback()
                    flash(
                        f"Insufficient stock for '{product.name}'. "
                        f"Only {available} unit(s) available. Please update your cart.",
                        "danger",
                    )
                    return redirect(url_for("shop.view_cart"))

            # ── All checks passed — create the order ──────────────────────
            # Calculate total (Decimal arithmetic, never trust browser values).
            total = Decimal("0.00")
            for item in cart_items:
                product = db.session.get(Product, item.product_id)
                total += Decimal(str(product.price)) * Decimal(str(item.quantity))

            # Prepare shipping address snapshot (trimmed, optional fields None if blank)
            shipping_name = (
                form.full_name.data.strip()
                if form.full_name.data and form.full_name.data.strip()
                else None
            )
            shipping_email = (
                form.email.data.strip()
                if form.email.data and form.email.data.strip()
                else None
            )
            shipping_phone = (
                form.phone.data.strip()
                if form.phone.data and form.phone.data.strip()
                else None
            )
            shipping_address_line1 = (
                form.address_line1.data.strip()
                if form.address_line1.data and form.address_line1.data.strip()
                else None
            )
            shipping_address_line2 = (
                form.address_line2.data.strip()
                if form.address_line2.data and form.address_line2.data.strip()
                else None
            )
            shipping_city = (
                form.city.data.strip()
                if form.city.data and form.city.data.strip()
                else None
            )
            shipping_state = (
                form.state.data.strip()
                if form.state.data and form.state.data.strip()
                else None
            )
            shipping_pincode = (
                form.pincode.data.strip()
                if form.pincode.data and form.pincode.data.strip()
                else None
            )
            order_notes = (
                form.notes.data.strip()
                if form.notes.data and form.notes.data.strip()
                else None
            )

            order = Order(
                user_id=current_user.id,
                total_amount=total,
                status=OrderStatus.PENDING.value,
                notes=order_notes,
                shipping_name=shipping_name,
                shipping_email=shipping_email,
                shipping_phone=shipping_phone,
                shipping_address_line1=shipping_address_line1,
                shipping_address_line2=shipping_address_line2,
                shipping_city=shipping_city,
                shipping_state=shipping_state,
                shipping_pincode=shipping_pincode,
            )
            db.session.add(order)
            db.session.flush()  # populate order.id without committing

            # Create order items (price snapshot).
            for item in cart_items:
                product = db.session.get(Product, item.product_id)
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                    unit_price=product.price,  # snapshot at checkout time
                )
                db.session.add(order_item)

                # Increment reserved_quantity (two-phase inventory approach).
                inv = inventory_map[item.product_id]
                inv.reserved_quantity += item.quantity

            # Delete all cart items for this user.
            db.session.execute(
                db.delete(CartItem).where(CartItem.user_id == current_user.id)
            )

            db.session.commit()

        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            flash(
                "An error occurred while placing your order. Please try again.",
                "danger",
            )
            # Re-raise in debug mode for easier troubleshooting.
            if db.engine.echo:
                raise
            return redirect(url_for("orders.checkout"))

        flash(f"Order #{order.id} placed successfully!", "success")
        return redirect(url_for("orders.confirmation", order_id=order.id))

    return render_template(
        "orders/checkout.html",
        form=form,
        cart_items=cart_items,
        subtotal=subtotal,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /orders/confirmation/<id>
# ─────────────────────────────────────────────────────────────────────────────

@orders_bp.route("/confirmation/<int:order_id>")
@login_required
def confirmation(order_id: int):
    """Display the order confirmation page.  Only the order owner may view it."""
    order = db.session.get(Order, order_id)
    if order is None:
        abort(404)
    if order.user_id != current_user.id:
        abort(403)

    return render_template("orders/confirmation.html", order=order)


# ─────────────────────────────────────────────────────────────────────────────
# /orders/
# ─────────────────────────────────────────────────────────────────────────────

@orders_bp.route("/")
@login_required
def order_list():
    """List all orders for the current user, most recent first."""
    orders = (
        db.session.execute(
            db.select(Order)
            .where(Order.user_id == current_user.id)
            .order_by(Order.created_at.desc())
        )
        .scalars()
        .all()
    )
    return render_template("orders/order_list.html", orders=orders)
