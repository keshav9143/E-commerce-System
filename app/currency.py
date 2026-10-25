"""
app/currency.py — Currency formatting utilities for CartOS (Indian Rupees / INR).

Provides Indian numbering system grouping (lakhs, crores) and the ₹ symbol
for consistent monetary display across the application.
"""

from decimal import Decimal
from typing import Any


def format_inr(value: Any, symbol: bool = True) -> str:
    """
    Format a numeric amount or Decimal into Indian Rupees with Indian numbering grouping.

    Examples:
      79.99        -> "₹79.99"
      299.99       -> "₹299.99"
      1499.00      -> "₹1,499.00"
      100000.00    -> "₹1,00,000.00" (1 Lakh)
      12345678.90  -> "₹1,23,45,678.90" (1.23 Crore)
      0            -> "₹0.00"
      None         -> "₹0.00"

    Parameters:
      value: int, float, str, or Decimal amount.
      symbol: If True, prefix with '₹'. If False, return grouped number string only.
    """
    if value is None:
        return "₹0.00" if symbol else "0.00"

    try:
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
    except Exception:
        sym = "₹" if symbol else ""
        return f"{sym}{value}"

    is_negative = value < 0
    value = abs(value)

    # Format to two decimal places
    formatted = f"{value:.2f}"
    int_part, dec_part = formatted.split(".")

    # Indian numbering grouping:
    # Rightmost 3 digits group together, then remaining digits group in pairs of 2.
    if len(int_part) <= 3:
        grouped_int = int_part
    else:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        grouped_int = ",".join(groups) + "," + last3

    sign = "-" if is_negative else ""
    sym = "₹" if symbol else ""
    return f"{sign}{sym}{grouped_int}.{dec_part}"
