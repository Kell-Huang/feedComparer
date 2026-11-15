"""Display formatting shared by the result views.

One place so the two surfaces that show a match rate cannot drift apart.
"""
from math import floor
from typing import Union


def floor_percent(value: Union[int, float, None], suffix: bool = True) -> str:
    """Format a 0-100 rate as a whole percent, rounded *down*.

    Rounding would turn 99.6% into 100%, which reads as "no differences" when
    there are some. Truncating keeps 100% meaning exactly 100%.
    """
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    whole = int(floor(max(0.0, min(100.0, number))))
    return f"{whole}%" if suffix else str(whole)
