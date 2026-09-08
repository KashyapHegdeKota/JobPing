import re
from datetime import datetime, timezone, timedelta
import calendar

def subtract_months(dt: datetime, months: int) -> datetime:
    year = dt.year - (months // 12)
    month = dt.month - (months % 12)
    if month <= 0:
        month += 12
        year -= 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)

def subtract_years(dt: datetime, years: int) -> datetime:
    year = dt.year - years
    day = min(dt.day, calendar.monthrange(year, dt.month)[1])
    return dt.replace(year=year, day=day)

def parse_source_posted_at(value: object, *, observed_at: datetime) -> datetime | None:
    """Parse Simplify's source age (e.g. 0d, 1w, 1mo, 1y, Aug 11, 2026-08-11)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
        
    observed_date = observed_at.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Check relative dates like 0d, 1w, 1mo, 1y, 1hr
    rel_match = re.match(r"^(\d+)\s*(d|w|mo|y|hr?s?|mins?)$", value, re.IGNORECASE)
    if rel_match:
        amount = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        if unit == 'd':
            return observed_date - timedelta(days=amount)
        elif unit == 'w':
            return observed_date - timedelta(weeks=amount)
        elif unit == 'mo':
            return subtract_months(observed_date, amount)
        elif unit == 'y':
            return subtract_years(observed_date, amount)
        elif unit.startswith('h') or unit.startswith('min'):
            return observed_date
            
    # Try ISO format
    try:
        dt = datetime.fromisoformat(value).astimezone(timezone.utc)
        result = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if result > observed_date:
            return None
        return result
    except ValueError:
        pass
        
    # Try month day (Aug 11)
    try:
        value_with_year = f"{value} {observed_date.year}"
        dt = datetime.strptime(value_with_year, "%b %d %Y")
        result = observed_date.replace(month=dt.month, day=dt.day)
        if result > observed_date:
            result = subtract_years(result, 1)
        return result
    except ValueError:
        pass
        
    return None
