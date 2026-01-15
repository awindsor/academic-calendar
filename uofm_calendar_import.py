#!/usr/bin/env python3
"""
University of Memphis term calendar importer.

What it does
- Scrapes:
  1) Academic year calendar:
     https://preview.memphis.edu/registrar/calendars/academic/ay{yy}{yy+1}.php
  2) Dates & Deadlines calendar (tries common URL patterns):
     https://preview.memphis.edu/registrar/calendars/dates/{lowercase semester}{yy}-dates.php
     https://preview.memphis.edu/registrar/calendars/dates/{yy}{f|s}-dates.php
- Creates all-day events on the target Google Calendar:
    61147m13qjm5liln3f318l3a08@group.calendar.google.com

Events added
- First day of classes
- Last day of classes
- Study Day
- Exams (date range)
- Spring/Fall break (if present) (date range)
- Holidays that occur during the term (e.g., Labor Day, Thanksgiving, MLK Day, etc.)
- Deadlines:
    - Last day to drop with no grade assigned (end of Drop Period window, FULL)
    - Last day to drop with a "W" grade assigned (end of Withdrawal Period window, FULL)
- Week labels (instructional weeks):
    - For each Monday–Friday week intersecting the instructional period (first..last day of classes):
      - Skip week entirely if there are 0 instructional weekdays (typically Spring Break week).
      - Otherwise, create an all-day multi-day event spanning the instructional weekdays in that week.
      - Title: "Week {i}" if 5 instructional weekdays, else "Week {i} (short)".
      - Week counter increments only for weeks that have at least 1 instructional weekday.

Notes / assumptions
- “Instructional weekdays” are Mon–Fri days that are within first..last day of classes (inclusive)
  AND not inside any break/holiday date ranges detected from the academic-year calendar.
- Holidays listed as a single day (e.g., Labor Day) are treated as non-instructional.
- Multi-day closures (e.g., Thanksgiving Holidays Wed–Sun) are treated as non-instructional for any weekdays within them.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Tuple, Dict

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/calendar"]
TARGET_CALENDAR_ID = "61147m13qjm5liln3f318l3a08@group.calendar.google.com"

ACADEMIC_BASE = "https://preview.memphis.edu/registrar/calendars/academic"
DATES_BASE = "https://preview.memphis.edu/registrar/calendars/dates"


# -----------------------------
# Utilities: dates & ranges
# -----------------------------

@dataclasses.dataclass(frozen=True)
class DateRange:
    start: date
    end: date  # inclusive

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    def intersects(self, other: "DateRange") -> bool:
        return not (self.end < other.start or other.end < self.start)

    def clamp(self, lo: date, hi: date) -> "DateRange":
        return DateRange(start=max(self.start, lo), end=min(self.end, hi))


def daterange_inclusive(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def to_google_allday(start_d: date, end_inclusive: date) -> Dict[str, Dict[str, str]]:
    """
    Google all-day events use an *exclusive* end date.
    """
    return {
        "start": {"date": start_d.isoformat()},
        "end": {"date": (end_inclusive + timedelta(days=1)).isoformat()},
    }


# -----------------------------
# Scraping helpers
# -----------------------------

def fetch_html(url: str, timeout: int = 30) -> str:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def normalize_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_month_day_year(text: str) -> date:
    """
    Parses strings like:
      "August 25, 2025"
      "January 20, 2026"
    """
    dt = dateparser.parse(text, fuzzy=True).date()
    return dt


def parse_range(text: str) -> DateRange:
    """
    Parses strings like:
      "October 11-14, 2025"
      "November 26-30, 2025"
      "March 9-15, 2026"
      "December 5-11, 2025"
      "May 1-7, 2026"
    Also tolerates:
      "March 9 - 15, 2026"
      "March 9–15, 2026" (en dash)
    """
    t = text.replace("–", "-")
    t = normalize_whitespace(t)
    # Example: "October 11-14, 2025"
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2}),\s*(\d{4})$", t)
    if m:
        mon, d1, d2, yyyy = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        start = dateparser.parse(f"{mon} {d1}, {yyyy}").date()
        end = dateparser.parse(f"{mon} {d2}, {yyyy}").date()
        return DateRange(start=start, end=end)

    # Example: "December 5-11, 2025 / Friday-Thursday" (strip trailing after year)
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2}),\s*(\d{4}).*$", t)
    if m:
        mon, d1, d2, yyyy = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        start = dateparser.parse(f"{mon} {d1}, {yyyy}").date()
        end = dateparser.parse(f"{mon} {d2}, {yyyy}").date()
        return DateRange(start=start, end=end)

    # If it's a single date, treat as a 1-day range.
    d = parse_month_day_year(t)
    return DateRange(start=d, end=d)


def academic_year_slug_for_term(year: int, semester: str) -> str:
    """
    Academic-year page is ay{yy}{yy+1}.php where yy is the *fall* year.
    - For fall YEAR: fall year == YEAR
    - For spring YEAR: fall year == YEAR-1
    """
    semester = semester.lower()
    if semester not in {"fall", "spring"}:
        raise ValueError("semester must be 'fall' or 'spring'")

    fall_year = year if semester == "fall" else year - 1
    yy = fall_year % 100
    yy2 = (fall_year + 1) % 100
    return f"ay{yy:02d}{yy2:02d}.php"


def candidate_dates_deadlines_urls(year: int, semester: str) -> List[str]:
    """
    The user-provided template uses:
      {lowercase semester}{two-digit year}-dates.php
    Example observed:
      spring26-dates.php
    Another observed pattern:
      25f-dates.php, 25s-dates.php
    We'll try both.
    """
    semester = semester.lower()
    yy = year % 100
    urls = []

    # Template-style (e.g., spring26-dates.php, fall25-dates.php)
    urls.append(f"{DATES_BASE}/{semester}{yy:02d}-dates.php")

    # Compact code-style (e.g., 25f-dates.php, 25s-dates.php)
    code = "f" if semester == "fall" else "s"
    urls.append(f"{DATES_BASE}/{yy:02d}{code}-dates.php")

    return urls


def extract_term_block_text(academic_html: str, year: int, semester: str) -> str:
    """
    Extracts the relevant portion of the academic-year page for the requested term.
    The academic-year page contains headings like:
      "## Spring 2026"
    and earlier for fall it is near the top (e.g., bullets without a "Fall 2025" header).
    Approach:
    - Parse text, then:
      - For spring: find heading containing "Spring {year}" and take text until next heading (##)
      - For fall: take from start until "## Spring {year+1}" (or until "## Spring")
    """
    soup = BeautifulSoup(academic_html, "html.parser")
    full_text = soup.get_text("\n")
    lines = [normalize_whitespace(l) for l in full_text.splitlines()]
    lines = [l for l in lines if l]

    semester = semester.lower()
    if semester == "spring":
        key = f"Spring {year}"
        start_idx = next((i for i, l in enumerate(lines) if key in l), None)
        if start_idx is None:
            raise RuntimeError(f"Could not find '{key}' section on academic-year page.")
        # take until next major heading marker "Summer" or another term header
        end_idx = next((i for i in range(start_idx + 1, len(lines)) if re.search(r"\b(Summer|Fall|Spring)\s+\d{4}\b", lines[i]) and key not in lines[i]), len(lines))
        return "\n".join(lines[start_idx:end_idx])

    # fall
    # Use the next spring heading as a boundary (spring is in next calendar year)
    key_next_spring = f"Spring {year + 1}"
    boundary = next((i for i, l in enumerate(lines) if key_next_spring in l), None)
    if boundary is None:
        # fallback: first occurrence of any "Spring ####"
        boundary = next((i for i, l in enumerate(lines) if re.search(r"\bSpring\s+\d{4}\b", l)), None)
    if boundary is None:
        boundary = len(lines)
    return "\n".join(lines[:boundary])


def find_bullet_date(term_text: str, label: str) -> Optional[date]:
    """
    Finds a line like:
      "* First Day of Classes: August 25, 2025 / Monday"
    Returns the date.
    """
    # Match "Label: <Month> <day>, <year>"
    pat = re.compile(rf"{re.escape(label)}\s*:\s*([A-Za-z]+\s+\d{{1,2}},\s+\d{{4}})")
    m = pat.search(term_text)
    if not m:
        return None
    return parse_month_day_year(m.group(1))


def find_bullet_range(term_text: str, label: str) -> Optional[DateRange]:
    """
    Finds a line like:
      "* Exams: December 5-11, 2025 / Friday-Thursday"
    Returns DateRange.
    """
    # Match "Label: <Month> <d>-<d>, <year>"
    pat = re.compile(rf"{re.escape(label)}\s*:\s*([A-Za-z]+\s+\d{{1,2}}\s*[-–]\s*\d{{1,2}},\s*\d{{4}})")
    m = pat.search(term_text)
    if not m:
        return None
    return parse_range(m.group(1))


def find_any_labeled_ranges(term_text: str, labels: List[str]) -> List[Tuple[str, DateRange]]:
    found = []
    for lab in labels:
        rng = find_bullet_range(term_text, lab)
        if rng:
            found.append((lab, rng))
    return found


def parse_deadlines_drop_withdraw(dates_html: str) -> Tuple[Optional[date], Optional[date]]:
    """
    From Dates & Deadlines page, extract FULL:
      - Drop Period (no grade) -> end date of FULL range
      - Withdrawal Period ("W") -> end date of FULL range

    Returns:
      (last_day_drop_no_grade, last_day_withdraw_W)
    """
    soup = BeautifulSoup(dates_html, "html.parser")
    txt = soup.get_text("\n")

    raw_lines = [normalize_whitespace(l) for l in txt.splitlines()]
    raw_lines = [l for l in raw_lines if l]

    def strip_bullets(s: str) -> str:
        # Remove common bullet/list prefixes while preserving content
        return re.sub(r"^[\s\-\*\u2022\u00B7]+", "", s).strip()

    lines = [strip_bullets(l) for l in raw_lines]

    # Identify sections by scanning text
    in_drop = False
    in_withdraw = False
    full_drop_line = None
    full_withdraw_line = None

    for l in lines:
        # Section toggles (these strings appear on the page)
        if re.search(r"Drop Period", l, re.IGNORECASE):
            in_drop = True
            in_withdraw = False
            continue
        if re.search(r"Withdrawal Period", l, re.IGNORECASE):
            in_drop = False
            in_withdraw = True
            continue

        # Grab the first FULL line in each section (ignore WIN, 1ST, 2ND, TN eCampus, etc.)
        if in_drop and full_drop_line is None and re.match(r"^FULL\b", l):
            full_drop_line = l
            continue
        if in_withdraw and full_withdraw_line is None and re.match(r"^FULL\b", l):
            full_withdraw_line = l
            continue

        if full_drop_line and full_withdraw_line:
            break

    def extract_end_date_from_full_line(line: Optional[str]) -> Optional[date]:
        if not line:
            return None

        # Remove leading "FULL" and separators
        # Examples:
        #   "FULL  -  January 20 - February 2, 2026"
        #   "FULL  -  February 3 - April 11, 2026"
        rest = re.sub(r"^FULL\b", "", line).strip()
        rest = re.sub(r"^[\s\-–:]+", "", rest).strip()
        rest = rest.replace("–", "-")
        rest = normalize_whitespace(rest)

        # Case A: single date "Month d, yyyy"
        if re.fullmatch(r"[A-Za-z]+\s+\d{1,2},\s*\d{4}", rest):
            return parse_month_day_year(rest)

        # Case B: range with two months "January 20 - February 2, 2026"
        m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})\s*-\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", rest)
        if m:
            end_mon, end_day, end_year = m.group(3), int(m.group(4)), int(m.group(5))
            return dateparser.parse(f"{end_mon} {end_day}, {end_year}").date()

        # Case C: range same month "March 16-29, 2026" or "March 16 - 29, 2026"
        m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2}),\s*(\d{4})", rest)
        if m:
            mon, end_day, end_year = m.group(1), int(m.group(3)), int(m.group(4))
            return dateparser.parse(f"{mon} {end_day}, {end_year}").date()

        # Fallback: pick the last explicit "Month d, yyyy" if present
        candidates = re.findall(r"[A-Za-z]+\s+\d{1,2},\s*\d{4}", rest)
        if candidates:
            return parse_month_day_year(candidates[-1])

        return None

    last_drop = extract_end_date_from_full_line(full_drop_line)
    last_withdraw = extract_end_date_from_full_line(full_withdraw_line)
    return last_drop, last_withdraw


# -----------------------------
# Google Calendar helpers
# -----------------------------

def get_calendar_service(credentials_path: str = "credentials.json", token_path: str = "token.json"):
    creds = None
    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception:
        creds = None

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
        creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def upsert_event(service, calendar_id: str, summary: str, start_d: date, end_inclusive: date, description: str = ""):
    """
    Idempotency strategy:
    - Use a deterministic "iCalUID" based on calendar_id + summary + start date.
    - If the same script is re-run, we try to locate by iCalUID and update; otherwise insert.

    Note: Google Calendar API lets you set "iCalUID" on insert.
    """
    ical_uid = f"uofm-{calendar_id}-{summary}-{start_d.isoformat()}@local".replace(" ", "_")

    # Search by iCalUID is not directly supported; we approximate by timeMin/timeMax and matching iCalUID.
    time_min = datetime.combine(start_d - timedelta(days=1), datetime.min.time()).isoformat() + "Z"
    time_max = datetime.combine(end_inclusive + timedelta(days=2), datetime.min.time()).isoformat() + "Z"
    events = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        maxResults=50,
    ).execute().get("items", [])

    existing = next((e for e in events if e.get("iCalUID") == ical_uid), None)

    body = {
        "summary": summary,
        "description": description,
        "transparency": "transparent",
        "visibility": "default",
        "eventType": "default",
        "iCalUID": ical_uid,
        **to_google_allday(start_d, end_inclusive),
    }

    if existing:
        service.events().update(calendarId=calendar_id, eventId=existing["id"], body=body).execute()
        print(f"Updated: {summary} ({start_d}..{end_inclusive})")
    else:
        service.events().insert(calendarId=calendar_id, body=body).execute()
        print(f"Inserted: {summary} ({start_d}..{end_inclusive})")


# -----------------------------
# Week labeling logic
# -----------------------------

def monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday=0


def instructional_week_events(
    first_class: date,
    last_class: date,
    blackout_ranges: List[DateRange],
) -> List[Tuple[str, date, date]]:
    """
    Returns list of (title, start_date, end_inclusive) for Week events.
    """
    term_range = DateRange(first_class, last_class)

    def is_blackout(day: date) -> bool:
        return any(r.contains(day) for r in blackout_ranges)

    # Iterate weeks from the week containing first_class through week containing last_class
    wk_start = monday_of_week(first_class)
    wk_end_boundary = monday_of_week(last_class)

    week_num = 1
    events = []

    cur = wk_start
    while cur <= wk_end_boundary:
        mon = cur
        fri = cur + timedelta(days=4)
        week_range = DateRange(mon, fri).clamp(term_range.start, term_range.end)

        # Collect instructional weekdays within this Mon–Fri window.
        instructional_days = []
        for d in daterange_inclusive(week_range.start, week_range.end):
            if d.weekday() >= 5:  # weekend
                continue
            if is_blackout(d):
                continue
            instructional_days.append(d)

        if len(instructional_days) == 0:
            # Entire week has no instructional weekdays -> skip and do not increment week counter
            cur += timedelta(days=7)
            continue

        start_d = instructional_days[0]
        end_d = instructional_days[-1]
        short = len(instructional_days) < 5
        title = f"Week {week_num}" + (" (short)" if short else "")
        events.append((title, start_d, end_d))
        week_num += 1

        cur += timedelta(days=7)

    return events

def extract_subsection(block_text: str, header: str) -> str:
    """
    Given a term block (e.g., Spring 2026 section), extract the subsection that starts
    at a line exactly equal to `header` and continues until the next recognized subsection header.
    If the header is not found, returns the original block_text.
    """
    lines = block_text.splitlines()
    lines = [l.strip() for l in lines if l.strip()]

    # Common subheaders used on these pages (extend if needed)
    subsection_headers = {
        "All Parts of Term",
        "Winter Intersession",
        "Full Part of Term",
        "1st Half Part of Term",
        "2nd Half Part of Term",
        "Pre Summer Part of Term",
        "Extended Summer Part of Term",
    }

    try:
        start = next(i for i, l in enumerate(lines) if l == header)
    except StopIteration:
        return block_text

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] in subsection_headers:
            end = i
            break

    return "\n".join(lines[start:end])


# -----------------------------
# Main orchestration
# -----------------------------

def build_term_events(year: int, semester: str) -> Dict[str, object]:
    semester = semester.lower()
    if semester not in {"fall", "spring"}:
        raise ValueError("semester must be 'fall' or 'spring'")

    # 1) Academic year page
    ay_slug = academic_year_slug_for_term(year, semester)
    ay_url = f"{ACADEMIC_BASE}/{ay_slug}"
    academic_html = fetch_html(ay_url)
    term_text = extract_term_block_text(academic_html, year, semester)
    term_text = extract_subsection(term_text, "Full Part of Term")

    # Extract key dates/ranges
    first_day = find_bullet_date(term_text, "First Day of Classes")
    last_day = find_bullet_date(term_text, "Last Day of Classes")
    study_day = find_bullet_date(term_text, "Study Day")
    exams_rng = find_bullet_range(term_text, "Exams")

    if not first_day or not last_day:
        raise RuntimeError("Could not determine First/Last Day of Classes from academic-year calendar.")

    # Breaks / holidays to add and to treat as blackout for week-counting
    break_like_labels = ["Spring Break", "Fall Break", "Thanksgiving Holidays"]
    holiday_like_labels = [
        "Labor Day",
        "M. L. King, Jr. Holiday",
        "M. L. King, Jr. Holiday:",
        "M. L. King, Jr. Holiday (All University Offices CLOSED)",
    ]

    # Find common breaks as ranges
    labeled_ranges = find_any_labeled_ranges(term_text, break_like_labels + ["Exams"])
    # Find single-day holidays (bullets with dates)
    single_holidays: List[Tuple[str, date]] = []
    for lab in ["Labor Day", "M. L. King, Jr. Holiday"]:
        d = find_bullet_date(term_text, lab)
        if d:
            single_holidays.append((lab, d))

    # Thanksgiving is usually a range on the academic calendar; keep it as blackout
    breaks: List[Tuple[str, DateRange]] = []
    for lab in break_like_labels:
        rng = find_bullet_range(term_text, lab)
        if rng:
            breaks.append((lab, rng))

    # Add other “break/holiday-like” items that appear as ranges but not in our label list:
    # (If you want to expand, add more labels here.)
    # For now, we rely on the big ones present on the academic-year page.  [oai_citation:2‡University of Memphis](https://preview.memphis.edu/registrar/calendars/academic/ay2526.php?utm_source=chatgpt.com)

    # 2) Dates & Deadlines page (drop/withdraw)
    dd_html = None
    dd_url_used = None
    for u in candidate_dates_deadlines_urls(year, semester):
        try:
            dd_html = fetch_html(u)
            dd_url_used = u
            break
        except Exception:
            continue
    if not dd_html:
        raise RuntimeError(
            "Could not fetch a Dates & Deadlines page for the requested term. "
            "Tried: " + ", ".join(candidate_dates_deadlines_urls(year, semester))
        )

    last_drop_no_grade, last_withdraw_w = parse_deadlines_drop_withdraw(dd_html)

    # Only include holidays that fall within first..last day of classes inclusive
    term_range = DateRange(first_day, last_day)

    holidays_in_term: List[Tuple[str, DateRange]] = []
    for lab, d in single_holidays:
        if term_range.contains(d):
            holidays_in_term.append((lab, DateRange(d, d)))

    # Break ranges might extend beyond first/last; clamp for inclusion tests, but keep original for event display
    breaks_in_term: List[Tuple[str, DateRange]] = []
    for lab, rng in breaks:
        if rng.intersects(term_range):
            breaks_in_term.append((lab, rng))

    # Blackouts for week counting:
    blackout_ranges = [rng.clamp(first_day, last_day) for _, rng in breaks_in_term]
    blackout_ranges += [r for _, r in holidays_in_term]  # single-day holidays are blackout too

    week_events = instructional_week_events(first_day, last_day, blackout_ranges)

    return {
        "ay_url": ay_url,
        "dd_url": dd_url_used,
        "first_day": first_day,
        "last_day": last_day,
        "study_day": study_day,
        "exams_rng": exams_rng,
        "breaks_in_term": breaks_in_term,
        "holidays_in_term": holidays_in_term,
        "last_drop_no_grade": last_drop_no_grade,
        "last_withdraw_w": last_withdraw_w,
        "week_events": week_events,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True, help="Calendar year of the semester (e.g., 2025 for Fall 2025; 2026 for Spring 2026).")
    ap.add_argument("--semester", type=str, required=True, choices=["fall", "spring"], help="Semester name: fall or spring.")
    ap.add_argument("--calendar-id", type=str, default=TARGET_CALENDAR_ID, help="Target Google Calendar ID.")
    ap.add_argument("--credentials", type=str, default="credentials.json", help="OAuth credentials JSON path.")
    ap.add_argument("--token", type=str, default="token.json", help="OAuth token cache path.")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be created, but do not write to Google Calendar.")
    args = ap.parse_args()

    payload = build_term_events(args.year, args.semester)

    print("Sources:")
    print(f"  Academic year: {payload['ay_url']}")
    print(f"  Dates & deadlines: {payload['dd_url']}")
    print()

    events_to_create: List[Tuple[str, date, date, str]] = []

    # Key term dates
    events_to_create.append(("First Day of Classes", payload["first_day"], payload["first_day"], ""))
    events_to_create.append(("Last Day of Classes", payload["last_day"], payload["last_day"], ""))

    if payload["study_day"]:
        events_to_create.append(("Study Day", payload["study_day"], payload["study_day"], ""))

    if payload["exams_rng"]:
        rng: DateRange = payload["exams_rng"]
        events_to_create.append(("Exams", rng.start, rng.end, ""))

    # Breaks
    for lab, rng in payload["breaks_in_term"]:
        events_to_create.append((lab, rng.start, rng.end, ""))

    # Holidays
    for lab, rng in payload["holidays_in_term"]:
        events_to_create.append((lab, rng.start, rng.end, ""))

    # Drop/withdraw deadlines
    if payload["last_drop_no_grade"]:
        d = payload["last_drop_no_grade"]
        events_to_create.append(("Last day to drop with no grade assigned", d, d, "Derived from FULL Drop Period end date."))
    else:
        print("Warning: Could not determine last drop (no grade) deadline from Dates & Deadlines page.")

    if payload["last_withdraw_w"]:
        d = payload["last_withdraw_w"]
        events_to_create.append(('Last day to drop with a "W" grade assigned', d, d, 'Derived from FULL Withdrawal Period end date.'))
    else:
        print('Warning: Could not determine last "W" withdrawal deadline from Dates & Deadlines page.')

    # Week labels
    for title, start_d, end_d in payload["week_events"]:
        events_to_create.append((title, start_d, end_d, "Instructional week label (Mon–Fri), excluding breaks/holidays."))

    # Output / write
    if args.dry_run:
        for summary, s, e, desc in events_to_create:
            print(f"[DRY RUN] {summary}: {s} .. {e}")
        return

    service = get_calendar_service(args.credentials, args.token)
    for summary, s, e, desc in events_to_create:
        upsert_event(service, args.calendar_id, summary, s, e, desc)

    print("\nDone.")


if __name__ == "__main__":
    main()