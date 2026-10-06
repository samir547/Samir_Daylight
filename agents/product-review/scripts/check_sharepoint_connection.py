#!/usr/bin/env python3
"""
Standalone SharePoint connectivity check for the Daylight sentiment pipeline.

Verifies, independently of daylight_sentiment/infra/sharepoint_sync.py:

  1. MSAL sign-in against the same tenant / client / scopes the real pipeline
     uses (silent -> ROPC -> interactive device-code fallback on MFA), so you
     can complete the one-time interactive sign-in on a new machine (e.g. the
     VM) without running the actual pipeline.
  2. Resolving SP_SHARE_URL to a SharePoint/OneDrive DriveItem via Microsoft
     Graph's /shares endpoint.
  3. Recursively listing everything under it, so you can see the real folder
     structure (TrustPilot/Product/In Progress, Amazon/In Progress, etc.)
     and confirm this is the right share before pointing anything at it.

Deliberately does NOT import anything from daylight_sentiment -- this is a
throwaway diagnostic, not a change to the pipeline. It reads the same .env
variables sharepoint_sync.py reads and, if you let it, writes to the exact
same token cache file (.sp_token_cache.json, next to this script) that the
real pipeline reads -- so a successful sign-in here also unblocks the real
`daylight_sentiment.cli` run afterwards, with no second interactive prompt.

Usage:
    python check_sharepoint_connection.py
    python check_sharepoint_connection.py --max-depth 2
    python check_sharepoint_connection.py --no-cache      # force a fresh sign-in

Reads from .env (place this script next to it, at the repo root):
    SP_SHARE_URL   (required)
    SP_USERNAME    (required)
    SP_PASSWORD    (optional -- omit to go straight to device-code sign-in)
    SP_CLIENT_ID   (optional -- defaults to the same MS Graph PowerShell client)
    SP_TENANT_ID   (optional -- defaults to "organizations")
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import msal
import requests
from dotenv import load_dotenv

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # MS Graph PowerShell public client
DEFAULT_TENANT = "organizations"
SCOPES = ["https://graph.microsoft.com/Files.ReadWrite.All",
          "https://graph.microsoft.com/Sites.Read.All"]
# Same list sharepoint_sync.py checks -- ROPC error codes that mean "this
# account needs interactive/MFA sign-in", worth falling back for, as opposed
# to a plain bad password.
MFA_ERROR_CODES = ("AADSTS50076", "AADSTS50079", "AADSTS65001", "AADSTS70002")

BASE = Path(__file__).resolve().parent
TOKEN_CACHE_PATH = BASE / ".sp_token_cache.json"  # same path the real pipeline reads


def get_token(client_id: str, tenant: str, username: str, password: Optional[str],
              use_cache: bool) -> str:
    cache = msal.SerializableTokenCache()
    if use_cache and TOKEN_CACHE_PATH.exists():
        print(f"Loading existing token cache: {TOKEN_CACHE_PATH}")
        try:
            cache.deserialize(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - corrupt/partial cache file
            print(f"  (couldn't read cache, ignoring it: {exc})")

    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=cache)

    result = None
    accounts = app.get_accounts(username=username)
    if accounts:
        print(f"Found cached account for {username}, trying silent token acquisition...")
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result:
            print("  silent acquisition succeeded -- no sign-in needed.")

    if not result and password:
        print("Trying username/password (ROPC) sign-in...")
        result = app.acquire_token_by_username_password(username, password, scopes=SCOPES)
        if "access_token" not in result:
            desc = str(result.get("error_description", ""))
            if any(code in desc for code in MFA_ERROR_CODES):
                print("  ROPC rejected -- account needs interactive/MFA sign-in.")
                result = None
            else:
                raise RuntimeError(
                    f"SharePoint sign-in failed ({result.get('error')}): {desc}")
    elif not result:
        print("SP_PASSWORD not set -- skipping ROPC, going straight to device-code sign-in.")

    if not result or "access_token" not in result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Failed to start device-code flow: {flow}")
        print("=" * 70)
        print("SHAREPOINT SIGN-IN REQUIRED")
        print(flow["message"])
        print("=" * 70)
        result = app.acquire_token_by_device_flow(flow)  # blocks, polling, until done/expired

    if "access_token" not in result:
        raise RuntimeError(
            f"SharePoint sign-in failed ({result.get('error')}): "
            f"{result.get('error_description')}")

    if use_cache and cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")
        print(f"Token cached to {TOKEN_CACHE_PATH} for future runs "
              f"(the real pipeline will reuse this too).")

    return result["access_token"]


def encode_share_url(url: str) -> str:
    b64 = base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")
    return "u!" + b64


def graph_get(url: str, token: str) -> Dict[str, Any]:
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def list_children(drive_id: str, item_id: str, token: str) -> List[Dict[str, Any]]:
    return graph_get(
        f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/children", token
    ).get("value", [])


def walk(drive_id: str, item_id: str, token: str, prefix: str, depth: int,
         max_depth: int, counts: Dict[str, int]) -> None:
    if depth > max_depth:
        print(f"{prefix}... (max depth {max_depth} reached, not descending further)")
        return
    children = list_children(drive_id, item_id, token)
    for child in sorted(children, key=lambda c: (0 if "folder" in c else 1, c["name"].lower())):
        if "folder" in child:
            counts["folders"] += 1
            n = child["folder"].get("childCount", "?")
            print(f"{prefix}[DIR]  {child['name']}/  ({n} items)")
            walk(drive_id, child["id"], token, prefix + "  ", depth + 1, max_depth, counts)
        else:
            counts["files"] += 1
            size = child.get("size", 0)
            modified = child.get("lastModifiedDateTime", "?")
            print(f"{prefix}[FILE] {child['name']}  ({size:,} bytes, modified {modified})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-depth", type=int, default=4,
                        help="Folder levels to recurse into (default: 4)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore any existing token cache and force a fresh sign-in")
    args = parser.parse_args()

    load_dotenv()
    share_url = os.getenv("SP_SHARE_URL")
    username = os.getenv("SP_USERNAME")
    password = os.getenv("SP_PASSWORD")
    client_id = os.getenv("SP_CLIENT_ID", DEFAULT_CLIENT_ID)
    tenant = os.getenv("SP_TENANT_ID", DEFAULT_TENANT)

    missing = [name for name, val in [("SP_SHARE_URL", share_url), ("SP_USERNAME", username)]
              if not val]
    if missing:
        print(f"Missing required .env value(s): {', '.join(missing)}")
        return 1

    print(f"Tenant:     {tenant}")
    print(f"Client ID:  {client_id}")
    print(f"Account:    {username}")
    print(f"Share URL:  {share_url}")
    print()

    try:
        token = get_token(client_id, tenant, username, password, use_cache=not args.no_cache)
    except RuntimeError as exc:
        print(f"\nAUTHENTICATION FAILED: {exc}")
        return 1
    print("\nAuthentication succeeded.\n")

    try:
        share_id = encode_share_url(share_url)
        root = graph_get(f"{GRAPH_BASE}/shares/{share_id}/driveItem", token)
    except requests.HTTPError as exc:
        print(f"\nFAILED to resolve the share link: {exc}")
        if exc.response is not None:
            print(f"Response body: {exc.response.text[:500]}")
        return 1

    drive_id = root["parentReference"]["driveId"]
    print(f"Resolved share -> {root.get('name', '(root)')}  "
          f"(drive {drive_id}, item {root['id']})")

    if "folder" not in root:
        print("\nNote: the share URL points at a single FILE, not a folder -- nothing to list.")
        return 0

    print(f"Top-level item count reported by SharePoint: {root['folder'].get('childCount', '?')}")
    print("\nFolder structure:\n")
    counts = {"folders": 0, "files": 0}
    walk(drive_id, root["id"], token, prefix="  ", depth=1, max_depth=args.max_depth,
        counts=counts)

    print(f"\nDone. {counts['folders']} folder(s), {counts['files']} file(s) found "
          f"(within {args.max_depth} level(s) of the root).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
