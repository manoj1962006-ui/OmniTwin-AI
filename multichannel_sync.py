"""Sync Google inbox data and receive live messaging webhook records."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from connectors import load_google_credentials, serve_ingestion_receiver, sync_google_data


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "omnitwin.db"
DEFAULT_CREDENTIALS_PATH = BASE_DIR / "credentials.json"
DEFAULT_TOKEN_PATH = BASE_DIR / "token.json"


def sync_google(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    credentials_path: str | Path = DEFAULT_CREDENTIALS_PATH,
    token_path: str | Path = DEFAULT_TOKEN_PATH,
    gmail_limit: int = 100,
    calendar_limit: int = 100,
) -> dict[str, int]:
    """Fetch unread Gmail and upcoming Calendar events into the local inbox."""
    credentials = load_google_credentials(
        client_secrets_file=credentials_path,
        token_file=token_path,
    )
    return sync_google_data(
        credentials,
        db_path=db_path,
        gmail_limit=gmail_limit,
        calendar_limit=calendar_limit,
    )


def serve_live_streams(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    host: str = "127.0.0.1",
    port: int = 8765,
    bearer_token: str | None = None,
) -> None:
    """Run authenticated webhook routes for WhatsApp, SMS, and LinkedIn."""
    server = serve_ingestion_receiver(
        host=host,
        port=port,
        db_path=db_path,
        bearer_token=bearer_token,
    )
    print(
        "Live messaging receiver listening on "
        f"http://{server.server_address[0]}:{server.server_address[1]}"
    )
    print("Routes: POST /ingest/whatsapp, /ingest/sms, /ingest/linkedin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping live messaging receiver.")
    finally:
        server.server_close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS_PATH)
    parser.add_argument("--token", type=Path, default=DEFAULT_TOKEN_PATH)
    parser.add_argument("--gmail-limit", type=int, default=100)
    parser.add_argument("--calendar-limit", type=int, default=100)
    parser.add_argument("--host", default=os.environ.get("OMNITWIN_INGESTION_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("OMNITWIN_INGESTION_PORT", "8765"))
    )
    parser.add_argument("--sync-only", action="store_true", help="Sync Google data and exit")
    args = parser.parse_args(argv)

    result = sync_google(
        db_path=args.db,
        credentials_path=args.credentials,
        token_path=args.token,
        gmail_limit=args.gmail_limit,
        calendar_limit=args.calendar_limit,
    )
    print(f"Google sync complete: fetched {result['fetched']}, inserted {result['inserted']} records.")

    if not args.sync_only:
        serve_live_streams(db_path=args.db, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
