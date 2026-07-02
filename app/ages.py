"""Age from a birth month + year. Pure date math (no DB, no imports from the app),
so the prompt builder, admin routers, and templates can all share one definition.

Students store `birth_month` (1–12) and `birth_year` instead of a hand-maintained
age, so the parent never has to remember to bump it — the age is derived on demand.
Only month granularity is kept (no day), so a birthday is treated as reached at the
start of the birth month.
"""

from datetime import date
from typing import Optional


def calc_age(birth_year: Optional[int], birth_month: Optional[int],
             on: Optional[date] = None) -> Optional[int]:
    """Age in whole years as of `on` (default: today, local). Returns None when the
    birth month/year isn't fully set, so callers can render "unknown" gracefully."""
    if not birth_year or not birth_month:
        return None
    on = on or date.today()
    age = on.year - birth_year - (1 if on.month < birth_month else 0)
    return age if age >= 0 else None
