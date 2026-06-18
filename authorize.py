#!/usr/bin/env python3
"""One-time helper: turn credentials.json into token.json for cal_sync.py.

Run this once. It opens a browser for you to grant read-only Google
Calendar access, then writes the resulting token to token.json. Re-run it
if the refresh token is ever revoked or expires without a refresh_token.
"""

import argparse
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_PORT = 8765


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        self.server.auth_code = query.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authorization received, you can close this tab.")

    def log_message(self, *args):
        pass  # keep stdout clean


def get_authorization_code(client_id, redirect_uri, port):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = f"{AUTH_URL}?{urlencode(params)}"
    print(f"Opening browser for Google authorization:\n{url}\n")
    webbrowser.open(url)

    server = HTTPServer(("localhost", port), _CallbackHandler)
    server.auth_code = None
    print(f"Waiting for redirect on {redirect_uri} ...")
    while server.auth_code is None:
        server.handle_request()
    return server.auth_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", default=str(Path.home() / ".config/cal_sync/credentials.json"))
    parser.add_argument("--token", default=str(Path.home() / ".config/cal_sync/token.json"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    credentials_path = Path(args.credentials).expanduser()
    token_path = Path(args.token).expanduser()
    if not credentials_path.exists():
        sys.exit(f"credentials.json not found: {credentials_path}")

    client_config = json.loads(credentials_path.read_text()).get("installed")
    if not client_config:
        sys.exit("credentials.json must be a 'Desktop app' OAuth client (missing 'installed' key).")

    client_id = client_config["client_id"]
    client_secret = client_config["client_secret"]
    redirect_uri = f"http://localhost:{args.port}/"

    code = get_authorization_code(client_id, redirect_uri, args.port)

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    tokens = resp.json()

    token_info = {
        "token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "token_uri": TOKEN_URL,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": [SCOPE],
    }
    if not token_info["refresh_token"]:
        print(
            "WARNING: no refresh_token returned. If you've authorized this app "
            "before, revoke access at https://myaccount.google.com/permissions "
            "and re-run this script.",
            file=sys.stderr,
        )

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(token_info, indent=2))
    print(f"Wrote {token_path}")


if __name__ == "__main__":
    main()
