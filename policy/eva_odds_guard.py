# ORACLE EVA-only mandatory sports odds filter. Historical generator stays unchanged.
from decimal import Decimal, InvalidOperation
MIN_ODDS=Decimal("1.70")
MAX_ODDS=Decimal("3.50")
def allow_sports_odds(odds):
    try:
        if isinstance(odds,bool) or odds is None:return False
        d=Decimal(str(odds))
        return d.is_finite() and MIN_ODDS<=d<=MAX_ODDS
    except (InvalidOperation,TypeError,ValueError):return False
