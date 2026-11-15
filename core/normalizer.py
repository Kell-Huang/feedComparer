"""SKU normalization: NFKC, whitespace cleanup, numeric canonicalization.

Two entry points with identical results:

  - ``normalize(value)``          reference implementation, one value at a time.
  - ``normalize_series(series)``  vectorized for whole Polars columns.

``normalize_series`` deliberately does *not* call ``normalize`` per row. A
per-row ``map_elements`` callback forces Polars out of its native execution
engine and is roughly two orders of magnitude slower on large feeds, so the
common ASCII path is expressed as Polars string expressions instead. Only rows
that actually contain non-ASCII characters (NFKC / exotic dash handling) fall
back to the Python reference, which keeps the results bit-for-bit identical
while making the hot path native.

Numeric canonicalization rules (mirrored exactly by the vectorized path):

  - Only values whose characters are digits (plus an optional leading ``-``)
    are considered numeric. Anything containing letters, ``+``, spaces, or
    more than one dot is left untouched.
  - ``"0012"``   -> ``"12"``      (leading zeros stripped, more than 1 digit)
  - ``"12.0"``   -> ``"12"``      (integral decimal collapses to integer)
  - ``"12."``    -> ``"12"``
  - ``"12.50"``  -> ``"12.50"``   (non-integral decimals are preserved)
  - ``"1.2.3"``  -> ``"1.2.3"``   (not a valid number, untouched)
"""

import math
import re
import unicodedata

import polars as pl

# Applied by the reference implementation, in this order.
_EXOTIC_SPACES = ("\u00a0", "\u2007", "\u202f")
_EXOTIC_DASHES = ("\u2013", "\u2014", "\u2015")
_CONTROL_RE = re.compile(r"[\t\n\r\f\v]")
_WHITESPACE_RE = re.compile(r"\s+")

# Vectorized equivalents. ``[0-9]`` (not ``\d``) keeps the numeric rules
# ASCII-only; Rust's ``\d`` is Unicode-aware and would otherwise swallow
# full-width digits before the NFKC fallback could see them.
_CONTROL_PATTERN = r"[\t\n\r\f\v]"
_WHITESPACE_PATTERN = r"\s+"
_NON_ASCII_PATTERN = r"[^\x00-\x7F]"

_DECIMAL_ZERO_PATTERN = r"^(?<sign>-?)(?<num>[0-9]+)\.(?<zeros>0*)$"
_INTEGER_PATTERN = r"^(?<sign>-?)(?<num>[0-9]+)$"
_LEADING_ZEROS_PATTERN = r"^0+"


class SKUNormalizer:
    # ------------------------------------------------------------------
    # Reference implementation (single value)
    # ------------------------------------------------------------------

    def normalize(self, sku) -> str:
        if sku is None:
            return ""
        if isinstance(sku, float) and math.isnan(sku):
            return ""
        s = str(sku).strip()
        s = unicodedata.normalize("NFKC", s)
        # Collapse exotic space characters into a plain space.
        for c in _EXOTIC_SPACES:
            s = s.replace(c, " ")
        # Collapse exotic dash characters into an ASCII hyphen.
        for c in _EXOTIC_DASHES:
            s = s.replace(c, "-")
        s = _CONTROL_RE.sub("", s)
        s = _WHITESPACE_RE.sub(" ", s)
        # Canonicalize numeric-like SKUs (e.g. "0012.0" -> "12").
        cleaned = s.replace(".", "").replace("-", "").replace(" ", "")
        if cleaned.isdigit() and len(cleaned) > 1:
            try:
                if "." in s:
                    num = float(s)
                    if num == int(num):
                        s = str(int(num))
                else:
                    s = str(int(s))
            except (ValueError, OverflowError):
                pass
        return s

    # ------------------------------------------------------------------
    # Vectorized implementation (whole column)
    # ------------------------------------------------------------------

    def normalize_series(self, series: pl.Series) -> pl.Series:
        name = series.name
        if series.dtype != pl.String:
            series = series.cast(pl.String, strict=False)
        s = series.fill_null("")

        # Whitespace cleanup. Rust's ``\s`` is Unicode-aware, so NBSP and the
        # other exotic spaces collapse to ' ' here exactly as they do in the
        # reference implementation.
        out = (
            s.str.strip_chars()
            .str.replace_all(_CONTROL_PATTERN, "")
            .str.replace_all(_WHITESPACE_PATTERN, " ")
        )

        # The numeric rules are Expr-based, so evaluate them and materialize
        # back to a Series before the non-ASCII check below (an Expr has no
        # truth value).
        out = pl.select(
            self._canonicalize_ascii_numbers(pl.lit(out, dtype=pl.String))
        ).to_series()

        # NFKC and exotic dashes cannot be expressed as Polars expressions.
        # Restrict the Python fallback to the rows that actually need it.
        non_ascii = out.str.contains(_NON_ASCII_PATTERN)
        if bool(non_ascii.any()):
            # Only the rows that need the Python path are materialised: a feed
            # with three CJK titles used to convert the whole column.
            indices = non_ascii.arg_true()
            fixed = pl.Series(
                "v",
                [self.normalize(value) for value in out.gather(indices).to_list()],
                dtype=pl.String,
            )
            out = out.scatter(indices, fixed)

        return out.rename(name)

    @staticmethod
    def _canonicalize_ascii_numbers(col: pl.Expr) -> pl.Expr:
        """Apply the numeric canonicalization rules as Polars expressions."""
        # "12.0" / "12." / "0.0" -> integer part. Guarded by the same
        # "cleaned value must be longer than one character" rule the
        # reference implementation uses.
        decimal = col.str.extract_groups(_DECIMAL_ZERO_PATTERN)
        dec_num = decimal.struct.field("num")
        dec_zeros = decimal.struct.field("zeros")
        decimal_applies = (dec_num.str.len_chars() + dec_zeros.str.len_chars()) > 1
        decimal_result = pl.concat_str(
            [
                decimal.struct.field("sign"),
                dec_num.str.replace(_LEADING_ZEROS_PATTERN, ""),
            ]
        )

        # "0012" -> "12". Only when more than one digit is present.
        integer = col.str.extract_groups(_INTEGER_PATTERN)
        int_num = integer.struct.field("num")
        integer_applies = int_num.str.len_chars() > 1
        integer_result = pl.concat_str(
            [
                integer.struct.field("sign"),
                int_num.str.replace(_LEADING_ZEROS_PATTERN, ""),
            ]
        )

        decimal_applies = decimal_applies.fill_null(False)
        integer_applies = integer_applies.fill_null(False)
        applied = decimal_applies | integer_applies

        result = (
            pl.when(decimal_applies)
            .then(decimal_result)
            .when(integer_applies)
            .then(integer_result)
            .otherwise(col)
        )
        # All-zero inputs strip down to an empty string; the reference
        # implementation yields "0" for those. Guard on ``applied`` so a
        # genuinely empty SKU stays empty instead of becoming "0".
        return pl.when(applied & (result == "")).then(pl.lit("0")).otherwise(result)
