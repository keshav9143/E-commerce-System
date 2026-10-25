"""
tests/test_currency.py — Unit tests for Indian Rupee (INR) formatting and Jinja filter.
"""

from decimal import Decimal
from app.currency import format_inr


class TestFormatINR:

    def test_standard_amounts(self):
        """Standard two-digit decimal amounts."""
        assert format_inr(Decimal("79.99")) == "₹79.99"
        assert format_inr(Decimal("59.50")) == "₹59.50"
        assert format_inr(Decimal("299.99")) == "₹299.99"
        assert format_inr(0) == "₹0.00"
        assert format_inr(None) == "₹0.00"

    def test_indian_number_grouping(self):
        """Verify Indian numbering grouping: 1,000; 10,000; 1,00,000 (lakh); 1,00,00,000 (crore)."""
        # Thousands (3 digits grouped)
        assert format_inr(Decimal("1000.00")) == "₹1,000.00"
        assert format_inr(Decimal("1499.00")) == "₹1,499.00"
        assert format_inr(Decimal("9999.50")) == "₹9,999.50"

        # Ten thousands
        assert format_inr(Decimal("10000.00")) == "₹10,000.00"
        assert format_inr(Decimal("75500.00")) == "₹75,500.00"

        # One Lakh (1,00,000)
        assert format_inr(Decimal("100000.00")) == "₹1,00,000.00"

        # Ten Lakhs (10,00,000)
        assert format_inr(Decimal("1500000.75")) == "₹15,00,000.75"

        # One Crore (1,00,00,000)
        assert format_inr(Decimal("10000000.00")) == "₹1,00,00,000.00"

    def test_symbol_flag(self):
        """When symbol=False, the numeric string without ₹ is returned."""
        assert format_inr(Decimal("79.99"), symbol=False) == "79.99"
        assert format_inr(Decimal("100000.00"), symbol=False) == "1,00,000.00"

    def test_negative_amounts(self):
        """Negative amounts retain proper sign placement."""
        assert format_inr(Decimal("-50.25")) == "-₹50.25"
        assert format_inr(Decimal("-100000.00")) == "-₹1,00,000.00"

    def test_jinja_filter_rendering(self, app):
        """Verify the Jinja filter is registered and renders correctly inside templates."""
        with app.app_context():
            from flask import render_template_string
            rendered = render_template_string("{{ price|inr }}", price=Decimal("79.99"))
            assert rendered == "₹79.99"

            rendered_currency = render_template_string("{{ price|currency }}", price=Decimal("150000.00"))
            assert rendered_currency == "₹1,50,000.00"
