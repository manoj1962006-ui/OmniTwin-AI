"""Google API and messaging-webhook connectors for the unified inbox."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

from database import init_db


GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/calendar'
]
MESSAGE_SOURCES = frozenset({"whatsapp", "sms", "linkedin"})
INBOX_SOURCES = frozenset({"gmail", "calendar", *MESSAGE_SOURCES})
DEFAULT_TOKEN_PATH = Path.home() / ".omnitwin" / "google-token.json"
MAX_WEBHOOK_BODY_BYTES = 1_000_000


def _iso_datetime(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds")


def normalize_inbox_record(
    *,
    source: str,
    external_id: str | None,
    item_type: str,
    account_id: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    subject: str | None = None,
    content: str | None = None,
    occurred_at: Any = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a row dictionary matching the insertable unified_inbox columns."""
    normalized_source = source.strip().lower()
    if normalized_source not in INBOX_SOURCES:
        raise ValueError(f"Unsupported inbox source: {source!r}")
    if not item_type.strip():
        raise ValueError("item_type must not be empty")
    if external_id is not None:
        external_id = str(external_id).strip() or None

    return {
        "source": normalized_source,
        "external_id": external_id,
        "item_type": item_type.strip(),
        "account_id": account_id,
        "sender": sender,
        "recipient": recipient,
        "subject": subject,
        "content": content,
        "occurred_at": _iso_datetime(occurred_at),
        "payload_json": json.dumps(payload or {}, ensure_ascii=False, default=str),
        "is_read": 0,
        "processing_status": "pending",
    }


def _fallback_external_id(record: Mapping[str, Any]) -> str:
    material = "\0".join(
        str(record.get(key) or "")
        for key in ("source", "sender", "recipient", "subject", "content", "occurred_at")
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def insert_inbox_records(
    records: Iterable[Mapping[str, Any]], db_path: str | Path = "omnitwin.db"
) -> int:
    """Insert normalized records idempotently; return the inserted row count."""
    connection = init_db(db_path)
    inserted = 0
    try:
        for record in records:
            values = dict(record)
            if "payload_json" not in values:
                values["payload_json"] = json.dumps(values.pop("payload", {}), default=str)
            values.setdefault("external_id", None)
            if not values["external_id"]:
                values["external_id"] = _fallback_external_id(values)
            values.setdefault("is_read", 0)
            values.setdefault("processing_status", "pending")
            columns = (
                "source", "external_id", "item_type", "account_id", "sender",
                "recipient", "subject", "content", "occurred_at", "payload_json",
                "is_read", "processing_status",
            )
            missing = [column for column in columns if column not in values]
            if missing:
                raise ValueError(f"Inbox record missing required fields: {', '.join(missing)}")
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO unified_inbox ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            inserted += cursor.rowcount
        connection.commit()
        return inserted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_google_credentials(
    client_secrets_file: str | Path | None = None,
    token_file: str | Path = DEFAULT_TOKEN_PATH,
    scopes: Sequence[str] = GOOGLE_SCOPES,
    *,
    open_browser: bool = True,
) -> Any:
    """Load or run installed-app OAuth for Gmail and Calendar."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as error:
        raise RuntimeError(
            "Google OAuth dependencies are missing; install the packages in requirements.txt."
        ) from error

    token_path = Path(token_file).expanduser()
    credentials = None
    if token_path.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), list(scopes))
        except (ValueError, OSError):
            credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        secrets_path = Path(
            client_secrets_file or os.environ.get("GOOGLE_CLIENT_SECRETS", "credentials.json")
        ).expanduser()
        if not secrets_path.is_file():
            raise FileNotFoundError(
                "Google OAuth client JSON not found. Set GOOGLE_CLIENT_SECRETS or pass "
                "client_secrets_file downloaded from Google Cloud Console."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), list(scopes))
        credentials = flow.run_local_server(port=0, open_browser=open_browser)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    try:
        token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return credentials


def _google_service(name: str, version: str, credentials: Any) -> Any:
    try:
        from googleapiclient.discovery import build
    except ImportError as error:
        raise RuntimeError(
            "Google API client is missing; install the packages in requirements.txt."
        ) from error
    return build(name, version, credentials=credentials, cache_discovery=False)


def _decode_gmail_part(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _gmail_text(payload: Mapping[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def visit(part: Mapping[str, Any]) -> None:
        body = part.get("body") or {}
        data = body.get("data") if isinstance(body, Mapping) else None
        if data:
            decoded = _decode_gmail_part(str(data))
            mime_type = str(part.get("mimeType", "")).lower()
            if mime_type == "text/plain":
                plain_parts.append(decoded)
            elif mime_type == "text/html":
                html_parts.append(decoded)
        children = part.get("parts") or []
        for child in children:
            if isinstance(child, Mapping):
                visit(child)

    visit(payload)
    if plain_parts:
        return "\n".join(plain_parts).strip()
    if html_parts:
        html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", " ".join(html_parts))
        html = re.sub(r"(?s)<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", html).strip()
    return ""


def _headers(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in payload.get("headers", [])
        if isinstance(header, Mapping)
    }


def _parse_addresses(value: str) -> str | None:
    return value.strip() or None


def _format_gmail_message(
    message: Mapping[str, Any], account_id: str | None
) -> dict[str, Any]:
    payload = message.get("payload") or {}
    headers = _headers(payload)
    return normalize_inbox_record(
        source="gmail",
        external_id=str(message.get("id", "")) or None,
        item_type="email",
        account_id=account_id,
        sender=_parse_addresses(headers.get("from", "")),
        recipient=_parse_addresses(headers.get("to", "")),
        subject=headers.get("subject") or "(no subject)",
        content=_gmail_text(payload) or str(message.get("snippet", "")),
        occurred_at=headers.get("date") or message.get("internalDate"),
        payload=message,
    )


def fetch_gmail_unread(
    credentials: Any,
    *,
    max_results: int = 100,
    user_id: str = "me",
) -> list[dict[str, Any]]:
    """Fetch unread Gmail messages and return unified_inbox-shaped records."""
    if max_results < 1:
        raise ValueError("max_results must be positive")
    service = _google_service("gmail", "v1", credentials)
    profile = service.users().getProfile(userId=user_id).execute()
    account_id = profile.get("emailAddress")
    records: list[dict[str, Any]] = []
    page_token = None
    while len(records) < max_results:
        response = (
            service.users()
            .messages()
            .list(
                userId=user_id,
                q="is:unread",
                maxResults=min(max_results - len(records), 500),
                pageToken=page_token,
            )
            .execute()
        )
        for item in response.get("messages", []):
            full_message = (
                service.users()
                .messages()
                .get(userId=user_id, id=item["id"], format="full")
                .execute()
            )
            records.append(_format_gmail_message(full_message, account_id))
            if len(records) >= max_results:
                break
        page_token = response.get("nextPageToken")
        if not page_token or not response.get("messages"):
            break
    return records


def fetch_upcoming_calendar_events(
    credentials: Any,
    *,
    calendar_id: str = "primary",
    time_min: datetime | None = None,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    """Fetch upcoming Google Calendar events as unified_inbox-shaped records."""
    if max_results < 1:
        raise ValueError("max_results must be positive")
    start = time_min or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    service = _google_service("calendar", "v3", credentials)
    response = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=start.isoformat(),
            maxResults=min(max_results, 2500),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    records = []
    for event in response.get("items", []):
        event_start = event.get("start") or {}
        start_value = event_start.get("dateTime") or event_start.get("date")
        organizer = event.get("organizer") or {}
        attendees = event.get("attendees") or []
        recipients = ", ".join(
            str(person["email"])
            for person in attendees
            if isinstance(person, Mapping) and person.get("email")
        )
        details = [str(event.get("description", "")).strip()]
        location = str(event.get("location", "")).strip()
        if location:
            details.append(f"Location: {location}")
        records.append(
            normalize_inbox_record(
                source="calendar",
                external_id=str(event.get("id", "")) or None,
                item_type="event",
                account_id=calendar_id,
                sender=organizer.get("email"),
                recipient=recipients or None,
                subject=str(event.get("summary") or "(untitled event)"),
                content="\n\n".join(part for part in details if part) or None,
                occurred_at=start_value,
                payload=event,
            )
        )
    return records


def sync_google_data(
    credentials: Any,
    db_path: str | Path = "omnitwin.db",
    *,
    gmail_limit: int = 100,
    calendar_id: str = "primary",
    calendar_limit: int = 100,
) -> dict[str, int]:
    """Fetch and insert unread mail plus upcoming events; duplicates are ignored."""
    records = fetch_gmail_unread(credentials, max_results=gmail_limit)
    records.extend(
        fetch_upcoming_calendar_events(
            credentials, calendar_id=calendar_id, max_results=calendar_limit
        )
    )
    inserted = insert_inbox_records(records, db_path)
    return {"fetched": len(records), "inserted": inserted}


def _unix_time(value: Any) -> str | None:
    try:
        return _iso_datetime(float(value))
    except (TypeError, ValueError):
        return _iso_datetime(value)


def _whatsapp_messages(payload: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    found = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            contacts = {
                str(contact.get("wa_id")): contact
                for contact in value.get("contacts", []) or []
                if contact.get("wa_id")
            }
            for message in value.get("messages", []) or []:
                enriched = dict(message)
                contact = contacts.get(str(message.get("from", "")), {})
                if contact:
                    enriched["_contact_name"] = (contact.get("profile") or {}).get("name")
                enriched["_phone_number_id"] = (value.get("metadata") or {}).get("phone_number_id")
                enriched["_display_phone_number"] = (value.get("metadata") or {}).get("display_phone_number")
                found.append((enriched, value))
    if not found and any(key in payload for key in ("from", "sender", "text", "messages")):
        for item in payload.get("messages", []) or [payload]:
            if isinstance(item, Mapping):
                found.append((item, payload))
    return found


def _generic_messages(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("messages", "events", "records", "items", "elements"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, Mapping)]
    nested = payload.get("message")
    if isinstance(nested, Mapping):
        return [nested]
    if any(key in payload for key in ("text", "body", "content", "message", "MessageSid", "id")):
        return [payload]
    return []


def normalize_messaging_webhook(
    source: str, payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Normalize WhatsApp Cloud, Twilio SMS, or generic messaging webhook data."""
    normalized_source = source.strip().lower()
    if normalized_source not in MESSAGE_SOURCES:
        raise ValueError(f"Unsupported messaging source: {source!r}")

    if normalized_source == "whatsapp":
        raw_messages = _whatsapp_messages(payload)
    else:
        raw_messages = [(message, payload) for message in _generic_messages(payload)]

    records = []
    for message, envelope in raw_messages:
        if normalized_source == "whatsapp":
            message_type = str(message.get("type", "text"))
            message_content = message.get("text") or message.get(message_type) or {}
            if isinstance(message_content, Mapping):
                content = message_content.get("body") or message_content.get("caption")
                if not content and message_content.get("id"):
                    content = f"[{message_type} attachment: {message_content['id']}]"
            else:
                content = str(message_content) if message_content else None
            sender = message.get("_contact_name") or message.get("from")
            recipient = message.get("_display_phone_number")
            account_id = message.get("_phone_number_id")
            external_id = message.get("id")
            occurred_at = _unix_time(message.get("timestamp"))
            subject = "WhatsApp message"
        elif normalized_source == "sms":
            content = message.get("Body") or message.get("body") or message.get("text")
            sender = message.get("From") or message.get("from") or message.get("sender")
            recipient = message.get("To") or message.get("to") or message.get("recipient")
            external_id = message.get("MessageSid") or message.get("SmsSid") or message.get("id")
            occurred_at = _iso_datetime(
                message.get("DateSent") or message.get("date") or message.get("timestamp")
            )
            account_id = message.get("AccountSid") or message.get("account_id")
            subject = "SMS message"
        else:
            sender_data = message.get("sender") or message.get("from") or message.get("actor")
            if isinstance(sender_data, Mapping):
                sender = sender_data.get("name") or sender_data.get("email") or sender_data.get("id")
            else:
                sender = sender_data
            recipient = message.get("recipient") or message.get("to")
            external_id = message.get("id") or message.get("eventId") or message.get("messageId")
            content = message.get("text") or message.get("body") or message.get("content")
            occurred_at = _iso_datetime(
                message.get("createdAt") or message.get("created_at") or message.get("timestamp")
            )
            account_id = message.get("accountId") or message.get("account_id")
            subject = message.get("subject") or "LinkedIn message"

        records.append(
            normalize_inbox_record(
                source=normalized_source,
                external_id=str(external_id) if external_id else None,
                item_type="message",
                account_id=str(account_id) if account_id else None,
                sender=str(sender) if sender else None,
                recipient=str(recipient) if recipient else None,
                subject=str(subject) if subject else None,
                content=str(content) if content else None,
                occurred_at=occurred_at,
                payload={"message": message, "envelope": envelope},
            )
        )
    return records


def _authorized(headers: Any, token: str) -> bool:
    authorization = headers.get("Authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(supplied, token)
def create_ingestion_handler(
    db_path: str | Path = "omnitwin.db", bearer_token: str | None = None
) -> type[BaseHTTPRequestHandler]:
    """Create a webhook handler for POST /ingest/{whatsapp|sms|linkedin}."""
    expected_token = bearer_token or os.environ.get("OMNITWIN_INGESTION_TOKEN", None)

    class IngestionHandler(BaseHTTPRequestHandler):
        server_version = "OmniTwinIngestion/1.0"

        def _respond(self, status: int, body: Mapping[str, Any]) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/webhook/whatsapp"):
                query = parse_qs(parsed.query)
                mode = query.get("hub.mode", [""])[0]
                token = query.get("hub.verify_token", [""])[0]
                challenge = query.get("hub.challenge", [""])[0]
                expected_verify = os.environ.get("WHATSAPP_VERIFY_TOKEN", "omnitwin_verify")
                if mode == "subscribe" and token == expected_verify:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(challenge.encode("utf-8"))
                    return
            self._respond(404, {"error": "Not Found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            path_parts = [p for p in parsed.path.split("/") if p]
            if len(path_parts) >= 2 and path_parts[0] in ("ingest", "webhook"):
                source = path_parts[1].lower()
                if source in INBOX_SOURCES:
                    if expected_token and not _authorized(self.headers, expected_token):
                        self._respond(401, {"error": "Unauthorized"})
                        return
                    content_length = int(self.headers.get("Content-Length", 0))
                    if content_length > MAX_WEBHOOK_BODY_BYTES:
                        self._respond(413, {"error": "Payload too large"})
                        return
                    body_bytes = self.rfile.read(content_length)
                    try:
                        payload = json.loads(body_bytes.decode("utf-8"))
                    except json.JSONDecodeError:
                        self._respond(400, {"error": "Invalid JSON"})
                        return
                    try:
                        if source in MESSAGE_SOURCES:
                            records = normalize_messaging_webhook(source, payload)
                        elif source == "gmail":
                            records = [normalize_inbox_record(source="gmail", external_id=payload.get("id"), item_type="email", payload=payload)]
                        elif source == "calendar":
                            records = [normalize_inbox_record(source="calendar", external_id=payload.get("id"), item_type="event", payload=payload)]
                        else:
                            records = []
                        inserted = insert_inbox_records(records, db_path)
                        self._respond(200, {"status": "success", "inserted": inserted})
                        return
                    except Exception as e:
                        self._respond(500, {"error": str(e)})
                        return
            self._respond(404, {"error": "Not Found"})

    return IngestionHandler

