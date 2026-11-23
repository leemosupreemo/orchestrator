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


def until(message: str, now: float | None = None) -> float:
    """When the tool can be asked again, read from its message; else an hour for a used-up allowance, minutes for a rate limit."""
    now = time.time() if now is None else now
    text = str(message or "")
    if (match := IN_TIME.search(text)) and (match.group(1) or match.group(2) or match.group(3)):
        seconds = int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60 + int(match.group(3) or 0)
        return now + min(seconds, MAX_HOLD)
    if match := AT_TIME.search(text):
        hour, minute, half = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower()
        if half == "pm" and hour < 12:
            hour += 12
        if half == "am" and hour == 12:
            hour = 0
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
