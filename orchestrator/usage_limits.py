"""An AI tool that has used up its allowance (a usage limit, a quota, a rate limit): when it can be asked again.

The tools say it in their own words ("Claude usage limit reached. Your limit will reset at 3pm", "You've hit your usage limit.
Try again in 2 hours 13 minutes", "429 Too Many Requests"). `detect` recognises it; `until` reads when it's back, or assumes
an hour (a few minutes for a plain rate limit) when the message doesn't say. The hold is recorded in the model checks
(project_chat.record_out_of_usage), so every job skips that tool until then and goes back to it after, by itself.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

USAGE = re.compile(r"usage limit|limit reached|hit your (?:usage )?limit|quota|out of credits|insufficient credits|credit balance|"
                   r"exceeded your (?:current )?(?:quota|plan)|limit will reset|resets? at", re.I)
RATE = re.compile(r"rate limit|too many requests|\b429\b|overloaded", re.I)
IN_TIME = re.compile(
    r"(?:try again|retry|resets?|available again)\s+in\s+"
    r"(?:(\d+)\s*h(?:ours?|rs?)?)?\s*"
    r"(?:(\d+)\s*m(?:in(?:ute)?s?)?)?\s*"
    r"(?:(\d+)\s*s(?:ec(?:ond)?s?)?)?",
    re.I,
)
AT_TIME = re.compile(r"(?:reset|resets|try again|available again)\D{0,20}?\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
ON_DATE = re.compile(  # a limit that ends another day: "try again at Nov 27th, 2026 1:41 AM", "resets Nov 30, 3pm"
    r"(?:resets?|try again|available again)(?:\s+(?:at|on))?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?:(\d{4}),?\s+)?(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.I,
)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DEFAULT_USAGE_HOLD = 60 * 60
DEFAULT_RATE_HOLD = 5 * 60
MAX_HOLD = 7 * 24 * 3600


def detect(message: str) -> str:
    """"usage" (an allowance used up), "rate" (asked too often just now), or "" for anything else."""
    text = str(message or "")
    if USAGE.search(text):
        return "usage"
    if RATE.search(text):
        return "rate"
    return ""


def _hour24(hour: int, half: str | None) -> int:
    half = (half or "").lower()
    if half == "pm" and hour < 12:
        return hour + 12
    if half == "am" and hour == 12:
        return 0
    return hour


def _on_date(match: re.Match[str], now: float) -> float | None:
    """The moment a dated reset names, or None when it isn't a real date still to come."""
    month_name, day, year, hour, minute, half = match.groups()
    month, local = MONTHS.index(month_name.lower()) + 1, datetime.fromtimestamp(now)
    # Without a year, a month long gone by means next year's ("Jan 2" read in December).
    full_year = int(year) if year else local.year + (month < local.month - 6)
    try:
        at = datetime(full_year, month, int(day), _hour24(int(hour), half), int(minute or 0)).timestamp()
    except ValueError:
        return None
    return at if at > now else None


def until(message: str, now: float | None = None) -> float:
    """When the tool can be asked again, read from its message; else an hour for a used-up allowance, minutes for a rate limit."""
    now = time.time() if now is None else now
    text = str(message or "")
    if (match := IN_TIME.search(text)) and (match.group(1) or match.group(2) or match.group(3)):
        seconds = int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60 + int(match.group(3) or 0)
        return now + min(seconds, MAX_HOLD)
    if (match := ON_DATE.search(text)) and (at := _on_date(match, now)):
        return min(at, now + MAX_HOLD)
    if match := AT_TIME.search(text):
        hour, minute = _hour24(int(match.group(1)), match.group(3)), int(match.group(2) or 0)
        if 0 <= hour < 24 and 0 <= minute < 60:
            local = datetime.fromtimestamp(now)
            at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if at.timestamp() <= now:
                at += timedelta(days=1)
            return at.timestamp()
    return now + (DEFAULT_RATE_HOLD if detect(text) == "rate" else DEFAULT_USAGE_HOLD)


def when(at: float) -> str:
    """"3:05 PM" today, else "Tue 3:05 PM"."""
    moment, today = datetime.fromtimestamp(at), datetime.now()
    clock = moment.strftime("%-I:%M %p")
    return clock if moment.date() == today.date() else f"{moment.strftime('%a')} {clock}"
