from zoneinfo import ZoneInfo, available_timezones

# A short curated list for the settings dropdown — the full IANA list has
# ~600 entries, most of which nobody hosting this would ever pick.
COMMON_TIMEZONES = [
    "Europe/Kyiv",
    "Europe/Warsaw",
    "Europe/Berlin",
    "Europe/London",
    "Europe/Lisbon",
    "Europe/Moscow",
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Asia/Dubai",
    "Asia/Almaty",
    "Asia/Kolkata",
    "Asia/Tokyo",
    "Australia/Sydney",
]

UTC = ZoneInfo("UTC")


def is_valid_timezone(name: str) -> bool:
    return name in available_timezones()


def format_local(dt, tz_name: str) -> str:
    """Formats a naive UTC datetime (as stored by datetime.utcnow()) in the
    given IANA timezone as "HH:MM DD.MM.YYYY"."""
    if not dt:
        return ""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = UTC
    local = dt.replace(tzinfo=UTC).astimezone(tz)
    return local.strftime("%H:%M %d.%m.%Y")
