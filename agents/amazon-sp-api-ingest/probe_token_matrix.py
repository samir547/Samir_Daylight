"""
Diagnostic: which LWA client pair (shared SPAPI_CLIENT_ID vs the UK app's
SPAPI_CLIENT_ID_UK) accepts which refresh token? Tries the token exchange
only - no reports are requested, and no secrets are printed (only the
outcome per combination).

Run (venv activated, in this folder):
    python probe_token_matrix.py
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

clients = {
    "client_shared": (os.environ.get("SPAPI_CLIENT_ID"), os.environ.get("SPAPI_CLIENT_SECRET")),
    "client_UK": (os.environ.get("SPAPI_CLIENT_ID_UK"), os.environ.get("SPAPI_CLIENT_SECRET_UK")),
}
tokens = {
    name.replace("SPAPI_REFRESH_TOKEN_", ""): val
    for name, val in sorted(os.environ.items())
    if name.startswith("SPAPI_REFRESH_TOKEN_")
}

print(f"{'token':<6}" + "".join(f"{c:<28}" for c in clients))
for tname, token in tokens.items():
    row = f"{tname:<6}"
    for cname, (cid, secret) in clients.items():
        if not cid or not secret:
            row += f"{'(client not set)':<28}"
            continue
        try:
            r = requests.post(
                LWA_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": token,
                      "client_id": cid, "client_secret": secret},
                timeout=30,
            )
            if r.ok:
                result = "OK"
            else:
                try:
                    result = r.json().get("error", str(r.status_code))
                except ValueError:
                    result = str(r.status_code)
        except requests.RequestException as e:
            result = f"network error: {type(e).__name__}"
        row += f"{result:<28}"
    print(row)
