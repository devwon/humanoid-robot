#!/usr/bin/env python3
"""One-time Google Calendar OAuth authorization.

Run this script once to generate the token file:
    python3 scripts/gcal_auth.py

The token is saved to data/gcal_token.json and reused automatically.
"""

from pathlib import Path

import os
CREDENTIALS_PATH = Path(os.environ.get("GCAL_CREDENTIALS_PATH", str(Path.home() / "Downloads" / "gcal_credentials.json")))
TOKEN_PATH = Path(__file__).parent.parent / "data" / "gcal_token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_PATH.write_text(creds.to_json())
    print(f"Token saved to: {TOKEN_PATH}")
    print("You can now start the server — calendar will load automatically.")


if __name__ == "__main__":
    main()
