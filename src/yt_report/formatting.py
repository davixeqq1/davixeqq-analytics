"""Human-readable number formatting shared by Discord, recommendations and the CLI.

Rounding rule (documented in docs/ARCHITECTURE.md, mirrored in web/static/app.js):
values are rounded half away from zero on their decimal representation, so
1,227 min = 20.45 h is shown as "20.5 h" (plain float formatting would give 20.4).
Rounding is applied only for display; stored and compared values are unrounded.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

NA = "n/a"


def round_half_up(value: float, digits: int = 0) -> float:
    """Round half away from zero using the shortest decimal repr of ``value``."""
    quantum = Decimal(1).scaleb(-digits)
    # "+ 0.0" turns -0.0 into 0.0 so tiny negative changes never print as "-0.0%".
    return float(Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP)) + 0.0


def fmt_int(value: float | None) -> str:
    return NA if value is None else f"{int(round_half_up(value)):,}"


def fmt_hours(value: float | None) -> str:
    if value is None:
        return NA
    digits = 1 if abs(value) >= 10 else 2
    return f"{round_half_up(value, digits):,.{digits}f} h"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return NA
    total = int(round_half_up(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_pct(value: float | None, digits: int = 1) -> str:
    return NA if value is None else f"{round_half_up(value, digits):.{digits}f}%"


def fmt_value(value: float | None, kind: str) -> str:
    return {
        "hours": fmt_hours,
        "duration": fmt_duration,
        "pct": fmt_pct,
    }.get(kind, fmt_int)(value)


def fmt_signed_int(value: float | None) -> str:
    if value is None:
        return NA
    v = int(round_half_up(value))
    return f"+{v:,}" if v > 0 else f"{v:,}"


def fmt_pp(value: float | None, digits: int = 1) -> str:
    """Signed difference in percentage points, e.g. '+3.2 pp'."""
    if value is None:
        return NA
    r = round_half_up(value, digits)
    return f"{'+' if r > 0 else ''}{r:.{digits}f} pp"


def fmt_change(value: float | None, digits: int = 1) -> str:
    """Signed percentage change, e.g. '+12.3%' / '-4.1%'; 'n/a' when undefined."""
    if value is None:
        return NA
    r = round_half_up(value, digits)
    return f"{'+' if r > 0 else ''}{r:.{digits}f}%"
