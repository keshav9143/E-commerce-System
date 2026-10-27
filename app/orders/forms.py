"""
app/orders/forms.py — WTForms form class for order checkout.

Security notes
──────────────
- All fields are validated server-side; never trust browser-provided data.
- Pincode is validated with a Regexp validator (6 digits only).
- Flask-WTF automatically adds a hidden CSRF token.
- Email and full_name are pre-populated from current_user where available.
"""

from flask_wtf import FlaskForm
from wtforms import EmailField, StringField, SubmitField, TextAreaField
from wtforms.validators import (
    DataRequired,
    Email,
    Length,
    Optional,
    Regexp,
)


def _strip_filter(val):
    return val.strip() if isinstance(val, str) else val


class CheckoutForm(FlaskForm):
    """Shipping / delivery address form used during checkout."""

    full_name = StringField(
        "Full Name",
        filters=[_strip_filter],
        validators=[
            DataRequired(message="Full name is required."),
            Length(max=100, message="Full name must be at most 100 characters."),
        ],
        render_kw={"placeholder": "e.g. Priya Sharma", "autocomplete": "name"},
    )
    email = EmailField(
        "Email Address",
        filters=[_strip_filter],
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Please enter a valid email address."),
        ],
        render_kw={"placeholder": "you@example.com", "autocomplete": "email"},
    )
    phone = StringField(
        "Phone Number",
        filters=[_strip_filter],
        validators=[
            DataRequired(message="Phone number is required."),
            Length(max=20, message="Phone number must be at most 20 characters."),
        ],
        render_kw={"placeholder": "e.g. 9876543210", "autocomplete": "tel"},
    )
    address_line1 = StringField(
        "Address Line 1",
        filters=[_strip_filter],
        validators=[
            DataRequired(message="Address line 1 is required."),
            Length(max=200, message="Address line 1 must be at most 200 characters."),
        ],
        render_kw={"placeholder": "House / flat number, street name"},
    )
    address_line2 = StringField(
        "Address Line 2",
        filters=[_strip_filter],
        validators=[
            Optional(),
            Length(max=200, message="Address line 2 must be at most 200 characters."),
        ],
        render_kw={"placeholder": "Landmark, area (optional)"},
    )
    city = StringField(
        "City",
        filters=[_strip_filter],
        validators=[
            DataRequired(message="City is required."),
            Length(max=100, message="City must be at most 100 characters."),
        ],
        render_kw={"placeholder": "e.g. Mumbai"},
    )
    state = StringField(
        "State",
        filters=[_strip_filter],
        validators=[
            DataRequired(message="State is required."),
            Length(max=100, message="State must be at most 100 characters."),
        ],
        render_kw={"placeholder": "e.g. Maharashtra"},
    )
    pincode = StringField(
        "Pincode",
        filters=[_strip_filter],
        validators=[
            DataRequired(message="Pincode is required."),
            Regexp(
                r"^\d{6}$",
                message="Pincode must be exactly 6 digits.",
            ),
        ],
        render_kw={"placeholder": "6-digit PIN", "maxlength": "6", "inputmode": "numeric"},
    )
    notes = TextAreaField(
        "Order Notes (optional)",
        filters=[_strip_filter],
        validators=[
            Optional(),
            Length(max=500, message="Notes must be at most 500 characters."),
        ],
        render_kw={"placeholder": "Any special delivery instructions…", "rows": 3},
    )
    submit = SubmitField("Place Order")
