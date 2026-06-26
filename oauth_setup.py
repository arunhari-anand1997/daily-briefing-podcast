#!/usr/bin/env python3
"""oauth_setup.py — one-time helper to mint a Google Drive OAuth refresh token.

Run this ONCE on your own machine (it needs a browser). It walks you through the
Google consent screen for your own Google account and prints the three values to
paste into GitHub Actions secrets:

  GOOGLE_OAUTH_CLIENT_ID
  GOOGLE_OAUTH_CLIENT_SECRET
  GOOGLE_OAUTH_REFRESH_TOKEN

deliver.py then uploads episodes into YOUR Drive (using YOUR quota), so you don't
hit the service-account `storageQuotaExceeded` 403.

Setup in Google Cloud Console (https://console.cloud.google.com/):
  1. Pick/create a project and enable the Google Drive API.
  2. Configure the OAuth consent screen (External; add your own Gmail address as
     a Test user so the unverified app can be used indefinitely by you).
  3. Create credentials -> OAuth client ID -> Application type "Desktop app".
  4. Download the client JSON, OR copy its client_id and client_secret.

Usage:
  pip install google-auth-oauthlib            # only needed for this one-time step
  python oauth_setup.py --client-secrets-file client_secret.json
  # or:
  python oauth_setup.py --client-id XXX.apps.googleusercontent.com --client-secret YYY
"""
from __future__ import annotations

import argparse
import sys

# Must match the scope deliver.py requests.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-secrets-file",
                    help="Path to the OAuth client JSON downloaded from Google Cloud")
    ap.add_argument("--client-id", help="OAuth client ID (if not using --client-secrets-file)")
    ap.add_argument("--client-secret", help="OAuth client secret (with --client-id)")
    args = ap.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing dependency. Run: pip install google-auth-oauthlib", file=sys.stderr)
        return 1

    if args.client_secrets_file:
        flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets_file, SCOPES)
    elif args.client_id and args.client_secret:
        client_config = {
            "installed": {
                "client_id": args.client_id,
                "client_secret": args.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    else:
        ap.error("Provide --client-secrets-file OR both --client-id and --client-secret")
        return 2

    # access_type=offline + prompt=consent guarantees a refresh_token is returned.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent",
    )

    if not creds.refresh_token:
        print("No refresh token returned. Re-run; Google only returns it on first "
              "consent — revoke prior access at https://myaccount.google.com/permissions "
              "and try again.", file=sys.stderr)
        return 3

    print("\n=== Add these as GitHub Actions secrets ===\n")
    print(f"GOOGLE_OAUTH_CLIENT_ID={creds.client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={creds.client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("\nKeep these secret. The refresh token grants upload access to your Drive.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
