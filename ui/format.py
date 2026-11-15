"""Display formatting shared by the result views.

One place so the two surfaces that show a match rate cannot drift apart.
"""

from typing import Union


def format_percent(value: Union[int, float, None], suffix: bool = True) -> str:
    """Format a 0-100 rate with two decimals.

    Two decimals is the compromise: a whole number turns 99.6% into 100%,
    which reads as "no differences", while four is noise. The value is
    clamped, never rounded to a whole percent.
    """
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    number = max(0.0, min(100.0, number))
    return f"{number:.2f}%" if suffix else f"{number:.2f}"
