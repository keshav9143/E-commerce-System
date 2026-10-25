"""
app/shop/forms.py — WTForms for shopping cart operations.

All mutations use CSRF-protected forms with strict server-side validation.
"""

from flask_wtf import FlaskForm
from wtforms import HiddenField, IntegerField, SubmitField
from wtforms.validators import InputRequired, NumberRange


class AddToCartForm(FlaskForm):
    """Form used on the product detail page to add items to the cart."""

    product_id = HiddenField(
        "Product ID",
        validators=[InputRequired(message="Product ID is required.")],
    )
    quantity = IntegerField(
        "Quantity",
        default=1,
        validators=[
            InputRequired(message="Quantity is required."),
            NumberRange(min=1, message="Quantity must be at least 1."),
        ],
    )
    submit = SubmitField("Add to Cart")


class UpdateCartItemForm(FlaskForm):
    """Form used on the cart page to update an item's quantity."""

    cart_item_id = HiddenField(
        "Cart Item ID",
        validators=[InputRequired(message="Cart Item ID is required.")],
    )
    quantity = IntegerField(
        "Quantity",
        validators=[
            InputRequired(message="Quantity is required."),
            NumberRange(min=1, message="Quantity must be at least 1."),
        ],
    )
    submit = SubmitField("Update")


class RemoveCartItemForm(FlaskForm):
    """Form used on the cart page to remove an item from the cart."""

    cart_item_id = HiddenField(
        "Cart Item ID",
        validators=[InputRequired(message="Cart Item ID is required.")],
    )
    submit = SubmitField("Remove")
