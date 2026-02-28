"""Google Calendar integration for robot brain context."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_CREDENTIALS_PATH = Path(os.environ.get("GCAL_CREDENTIALS_PATH", str(Path.home() / "Downloads" / "gcal_credentials.json")))
_TOKEN_PATH = Path(__file__).parent.parent / "data" / "gcal_token.json"
_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _build_service():
    """Build Google Calendar service using stored OAuth token."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _TOKEN_PATH.write_text(creds.to_json())
        else:
            logger.warning("Google Calendar token missing or invalid. Run scripts/gcal_auth.py first.")
            return None

    return build("calendar", "v3", credentials=creds)


def fetch_calendar_context(days_ahead: int = 1) -> str:
    """Fetch upcoming events and return a formatted string for robot brain context."""
    try:
        service = _build_service()
        if not service:
            return ""

        kst = timezone(timedelta(hours=9))
        now = datetime.now(kst)
        time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=days_ahead + 1)

        events_result = service.events().list(
            calendarId="primary",
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = events_result.get("items", [])
        if not events:
            return "오늘 일정 없음"

        lines = []
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date", ""))
            end = event["end"].get("dateTime", event["end"].get("date", ""))
            summary = event.get("summary", "(제목 없음)")
            location = event.get("location", "")
            description = event.get("description", "")

            # Format date label
            if "T" in start:
                dt = datetime.fromisoformat(start)
                dt_local = dt.astimezone(kst)
                date_label = "오늘" if dt_local.strftime("%Y-%m-%d") == today_str else "내일"
                time_str = dt_local.strftime("%H:%M")

                if "T" in end:
                    dt_end = datetime.fromisoformat(end).astimezone(kst)
                    time_str += f"~{dt_end.strftime('%H:%M')}"

                line = f"- [{date_label} {time_str}] {summary}"
            else:
                date_label = "오늘 종일" if start == today_str else "내일 종일"
                line = f"- [{date_label}] {summary}"

            if location:
                line += f" @ {location}"
            if description:
                short_desc = description.strip()[:80].replace("\n", " ")
                line += f" ({short_desc})"

            lines.append(line)

        return "\n".join(lines)

    except Exception as e:
        logger.error("Failed to fetch Google Calendar: %s", e)
        return ""


def fetch_events(days_ahead: int = 1) -> list[dict]:
    """Fetch upcoming events as structured list with parsed datetimes."""
    try:
        service = _build_service()
        if not service:
            return []

        kst = timezone(timedelta(hours=9))
        now = datetime.now(kst)
        time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=days_ahead + 1)

        result = service.events().list(
            calendarId="primary",
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = []
        for item in result.get("items", []):
            start_raw = item["start"].get("dateTime", item["start"].get("date", ""))
            end_raw = item["end"].get("dateTime", item["end"].get("date", ""))
            start_dt = datetime.fromisoformat(start_raw).astimezone(kst) if "T" in start_raw else None
            end_dt = datetime.fromisoformat(end_raw).astimezone(kst) if "T" in end_raw else None
            events.append({
                "id": item.get("id", ""),
                "summary": item.get("summary", "(제목 없음)"),
                "location": item.get("location", ""),
                "start_dt": start_dt,
                "end_dt": end_dt,
                "all_day": "T" not in start_raw,
            })
        return events

    except Exception as e:
        logger.error("Failed to fetch events: %s", e)
        return []


async def fetch_calendar_context_async(days_ahead: int = 1) -> str:
    """Async wrapper for calendar fetch (runs in thread pool)."""
    return await asyncio.get_event_loop().run_in_executor(
        None, fetch_calendar_context, days_ahead
    )


async def fetch_events_async(days_ahead: int = 1) -> list[dict]:
    """Async wrapper for structured events fetch."""
    return await asyncio.get_event_loop().run_in_executor(
        None, fetch_events, days_ahead
    )
