"""SharePoint sync -- pulls the review exports down into Reviews/ before the
review sources read them, so nothing downstream needs to know the files came
from SharePoint.

ISSUE-01: sync() now matches files by *pattern* (FilePattern: name tokens +
extension), not exact filename. A renamed export ("(2)" suffix, fresh row
count, date stamp) is a warning + best-guess match -- newest wins -- instead
of aborting the entire run before a single review is analyzed. A pattern with
no match at all in the folder still raises, since the source genuinely can't
be loaded. The exact-filename mode is retained for callers that want it.

sync_from_subfolders() is the per-source "In Progress" flow: each source has
its own staging subfolder, an empty one means "nothing new for this source
this run" (skipped, not an error), and the DriveItem of whatever was
downloaded is retained so the caller can move_item() it into "Processed"
once -- and only once -- its reviews are confirmed durable in the database.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence
from urllib.parse import quote

from daylight_sentiment.config import FilePattern

logger = logging.getLogger("multi_source_sentiment")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Kept next to the module that uses it -- process-external (a file next to
# the repo) so a completed device-code sign-in survives across runs via its
# refresh token, instead of prompting every single time this is invoked.
BASE = Path(__file__).resolve().parent.parent.parent

# The one command a person runs, in a terminal from agents/product-review, to
# redo the device-code sign-in. It uses this module's sign-in and token cache
# and only downloads into Reviews/ -- nothing is analysed, loaded or moved.
SIGN_IN_RECOVERY_COMMAND = "python -m daylight_sentiment.cli --sync-sharepoint-only"


class SharePointSignInRequiredError(RuntimeError):
    """The saved sign-in is missing or expired and the session is not
    interactive (e.g. cron), so a device-code sign-in could never be
    completed. Raised instead of starting the device flow and waiting for it."""


def _stdin_is_interactive() -> bool:
    """True only when a person could type at this process: stdin exists, is
    open and is a terminal. A closed or missing stdin counts as unattended."""
    stdin = sys.stdin
    if stdin is None or getattr(stdin, "closed", True):
        return False
    try:
        if not stdin.isatty():
            return False
        if os.name == "nt":
            return _is_windows_console(stdin.fileno())
        return True
    except (AttributeError, ValueError, OSError):
        return False


def _is_windows_console(fd: int) -> bool:
    """On Windows isatty() is also true for NUL (any character device), which
    is what a scheduled or redirected run gets as stdin; only a real console
    handle answers GetConsoleMode."""
    import ctypes
    import msvcrt
    mode = ctypes.c_uint32()
    handle = msvcrt.get_osfhandle(fd)
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def _write_private(path: Path, text: str) -> None:
    """Write the token cache readable by the owner only on POSIX (0o600) --
    it holds refresh tokens. On Windows the file keeps the default ACLs."""
    if os.name != "posix":
        path.write_text(text, encoding="utf-8")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)  # O_CREAT's mode only applies to a new file
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


class NoMatchingFileError(RuntimeError):
    """A pattern matched nothing in the folder. Subclasses RuntimeError so
    existing callers/tests that catch the broad type keep working, while
    sync_from_subfolders can tell "folder is empty" apart from a sign-in or
    HTTP failure (which must still abort the run)."""


class SourceSpec(NamedTuple):
    """One review source's SharePoint staging folder, for sync_from_subfolders."""
    key: str            # ReviewSource.name -- what cli.py filters loaders on
    subpath: str        # "In Progress" folder, relative to the shared root
    pattern: FilePattern


@dataclass
class SourceSyncResult:
    """Outcome of sync_from_subfolders for one source. ``synced`` is False
    when the staging folder held nothing matching (skipped this run); when
    True, ``item`` is the DriveItem that was downloaded -- keep it, it's what
    move_item() needs later."""
    key: str
    subpath: str
    local_path: Optional[Path] = None
    item: Optional[Dict[str, Any]] = None

    @property
    def synced(self) -> bool:
        return self.item is not None


def select_items_by_patterns(items: List[Dict[str, Any]],
                             patterns: Sequence[FilePattern]
                             ) -> List[Dict[str, Any]]:
    """Pick one folder item per pattern: the most recently modified match.

    Pure function (no network) so the selection policy is unit-testable.
    There's no expected/preferred filename to compare against -- a rename
    never needs special-casing because nothing was ever pinned to a specific
    name. A pattern with no match raises.
    """
    files = [i for i in items if "folder" not in i and i.get("name")]
    selected: List[Dict[str, Any]] = []
    for pattern in patterns:
        matches = [i for i in files if pattern.matches(i["name"])]
        if not matches:
            available = sorted(i["name"] for i in files)
            raise NoMatchingFileError(
                f"SharePoint folder has no file matching {pattern.describe()} "
                f"(files present: {available})")
        best = max(matches, key=lambda i: i.get("lastModifiedDateTime") or "")
        if len(matches) > 1:
            logger.info("Pattern %s matched %d files; picked newest: %s",
                        pattern.describe(), len(matches), best["name"])
        selected.append(best)
    return selected


class SharePointSync:
    """Downloads files out of a SharePoint/OneDrive shared folder.

    Resolves the share link via Microsoft Graph's ``/shares`` endpoint --
    the supported way to turn a ``:f:/p/...`` / ``:f:/g/...`` sharing URL
    into a DriveItem -- then lists and downloads its children.

    Auth is Resource Owner Password Credentials (ROPC): a plain
    username/password sign-in via MSAL, using the Microsoft Graph
    PowerShell first-party public client ID by default. ROPC only succeeds
    when the account has no MFA and no Conditional Access policy blocking
    non-interactive sign-in; if that's not the case for this tenant/account,
    ``sync()`` raises a clear error rather than hanging or silently
    continuing on stale local files.
    """

    TOKEN_CACHE_PATH = BASE / ".sp_token_cache.json"
    # Files.ReadWrite.All is for move_item() (In Progress -> Processed);
    # everything else here is read-only.
    SCOPES = ["https://graph.microsoft.com/Files.ReadWrite.All",
              "https://graph.microsoft.com/Sites.Read.All"]
    # ROPC failures that mean "this account needs interactive/MFA sign-in" --
    # worth falling back to device-code for, as opposed to a bad password.
    _MFA_ERROR_CODES = ("AADSTS50076", "AADSTS50079", "AADSTS65001", "AADSTS70002")

    def __init__(self, share_url: str, username: str, password: Optional[str],
                client_id: str, tenant: str = "organizations",
                interactive: Optional[bool] = None):
        self.share_url = share_url
        self.username = username
        self.password = password
        self.client_id = client_id
        self.tenant = tenant
        # None = decide from stdin when a sign-in is actually needed.
        self.interactive = interactive
        self._token: Optional[str] = None

    def _is_interactive(self) -> bool:
        if self.interactive is not None:
            return self.interactive
        return _stdin_is_interactive()

    def _build_app(self, cache):
        import msal
        return msal.PublicClientApplication(
            self.client_id, authority=f"https://login.microsoftonline.com/{self.tenant}",
            token_cache=cache)

    def _get_token(self) -> str:
        if self._token:
            return self._token
        import msal

        cache = msal.SerializableTokenCache()
        if self.TOKEN_CACHE_PATH.exists():
            cache.deserialize(self.TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        app = self._build_app(cache)

        try:
            result = None
            accounts = app.get_accounts(username=self.username)
            if accounts:
                result = app.acquire_token_silent(self.SCOPES, account=accounts[0])

            if not result and self.password:
                result = app.acquire_token_by_username_password(
                    self.username, self.password, scopes=self.SCOPES)
                if "access_token" not in result and not any(
                        code in str(result.get("error_description", ""))
                        for code in self._MFA_ERROR_CODES):
                    raise RuntimeError(
                        "SharePoint sign-in failed "
                        f"({result.get('error')}): {result.get('error_description')}")

            if not result or "access_token" not in result:
                if not self._is_interactive():
                    raise SharePointSignInRequiredError(
                        "SharePoint sign-in has expired or has not been set up on this "
                        "machine, and no one is at a terminal to complete it. In a "
                        "terminal, from the agents/product-review folder, run "
                        f"`{SIGN_IN_RECOVERY_COMMAND}`, open the address shown, enter "
                        "the code and sign in, then re-run.")
                logger.info("Password sign-in isn't usable for this account (MFA/Conditional "
                            "Access) -- falling back to interactive device-code sign-in.")
                flow = app.initiate_device_flow(scopes=self.SCOPES)
                if "user_code" not in flow:
                    raise RuntimeError(f"Failed to start device-code flow: {flow}")
                logger.info("=" * 70)
                logger.info("SHAREPOINT SIGN-IN REQUIRED")
                logger.info(flow["message"])
                logger.info("=" * 70)
                result = app.acquire_token_by_device_flow(flow)  # blocks, polling, until done/expired

            if "access_token" not in result:
                raise RuntimeError(
                    "SharePoint sign-in failed "
                    f"({result.get('error')}): {result.get('error_description')}")

            if cache.has_state_changed:
                _write_private(self.TOKEN_CACHE_PATH, cache.serialize())

            self._token = result["access_token"]
            return self._token
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - msal/requests raise many shapes
            raise RuntimeError(f"SharePoint sign-in failed: {exc}") from exc

    @staticmethod
    def _encode_share_url(url: str) -> str:
        import base64
        b64 = base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")
        return "u!" + b64

    def _get(self, url: str, **kwargs):
        import requests
        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {self._get_token()}")
        resp = requests.get(url, headers=headers, timeout=kwargs.pop("timeout", 60), **kwargs)
        resp.raise_for_status()
        return resp

    def _list_items(self) -> List[Dict[str, Any]]:
        share_id = self._encode_share_url(self.share_url)
        root = self._get(f"{GRAPH_BASE}/shares/{share_id}/driveItem").json()
        if "folder" in root:
            drive_id = root["parentReference"]["driveId"]
            return self._get(
                f"{GRAPH_BASE}/drives/{drive_id}/items/{root['id']}/children"
            ).json().get("value", [])
        return [root]  # the share link points straight at one file

    def _resolve_root(self) -> Dict[str, Any]:
        share_id = self._encode_share_url(self.share_url)
        return self._get(f"{GRAPH_BASE}/shares/{share_id}/driveItem").json()

    def _list_items_in(self, subpath: str) -> List[Dict[str, Any]]:
        """List children of a subfolder, addressed by path relative to the
        shared root (e.g. 'TrustPilot/Service/In Progress')."""
        root = self._resolve_root()
        drive_id = root["parentReference"]["driveId"]
        encoded = quote(subpath, safe="/")
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{root['id']}:/{encoded}:/children"
        return self._get(url).json().get("value", [])

    def _resolve_subfolder(self, subpath: str) -> Dict[str, Any]:
        """Resolve a subfolder's own DriveItem (id, driveId), addressed by
        path relative to the shared root. Read-only -- safe to call freely."""
        root = self._resolve_root()
        drive_id = root["parentReference"]["driveId"]
        encoded = quote(subpath, safe="/")
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{root['id']}:/{encoded}"
        item = self._get(url).json()
        item.setdefault("parentReference", {})["driveId"] = drive_id
        return item

    def sync_from_subfolders(self, dest_dir: Path,
                             sources: Sequence[SourceSpec]) -> Dict[str, SourceSyncResult]:
        """Pull the newest matching file from each source's own "In Progress"
        subfolder, instead of matching by filename token across one flat folder.

        Returns {SourceSpec.key: SourceSyncResult}. A subfolder with nothing
        matching is NOT an error -- nobody has staged a new export for that
        source -- so it's logged and returned as a skipped result (synced is
        False) rather than aborting the sync for the other sources. A synced
        result keeps the DriveItem that was downloaded, for move_item() later.
        Reuses select_items_by_patterns for the newest-wins/rename-tolerant
        selection within each subfolder.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        results: Dict[str, SourceSyncResult] = {}
        for spec in sources:
            items = self._list_items_in(spec.subpath)
            try:
                selected = select_items_by_patterns(items, [spec.pattern])
            except NoMatchingFileError:
                logger.info("No new file waiting in SharePoint's %s for %s -- skipping "
                            "this source for this run", spec.subpath, spec.key)
                results[spec.key] = SourceSyncResult(spec.key, spec.subpath)
                continue
            item = selected[0]
            local_path = self._download_item(item, dest_dir)
            results[spec.key] = SourceSyncResult(spec.key, spec.subpath, local_path, item)
        return results

    def move_item(self, item: Dict[str, Any], dest_folder: Dict[str, Any],
                  conflict_behavior: str = "replace") -> None:
        """Move ``item`` (a DriveItem dict, e.g. SourceSyncResult.item) into
        ``dest_folder`` (a DriveItem dict from _resolve_subfolder), replacing
        any existing same-named file in the destination by default.

        This is a MUTATING call against real SharePoint content: only call it
        once the file's reviews are confirmed durable in the database, so a
        failed run leaves the file in "In Progress" for the next run.
        """
        import requests
        drive_id = item["parentReference"]["driveId"]
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item['id']}"
        body = {
            "parentReference": {"id": dest_folder["id"]},
            "name": item["name"],
            "@microsoft.graph.conflictBehavior": conflict_behavior,
        }
        headers = {"Authorization": f"Bearer {self._get_token()}",
                   "Content-Type": "application/json"}
        resp = requests.patch(url, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        logger.info("Moved %s to %s (conflict=%s)", item["name"],
                    dest_folder.get("name", dest_folder["id"]), conflict_behavior)

    def _download_item(self, item: Dict[str, Any], dest_dir: Path) -> Path:
        name = item["name"]
        download_url = item.get("@microsoft.graph.downloadUrl")
        if download_url:
            import requests
            resp = requests.get(download_url, timeout=120)  # pre-signed, no auth header
            resp.raise_for_status()
            content = resp.content
        else:
            drive_id = item["parentReference"]["driveId"]
            content = self._get(
                f"{GRAPH_BASE}/drives/{drive_id}/items/{item['id']}/content", timeout=120).content
        out_path = dest_dir / name
        out_path.write_bytes(content)
        logger.info("Downloaded %s (%d bytes) from SharePoint", name, len(content))
        return out_path

    def sync(self, dest_dir: Path, filenames: Optional[set] = None,
             patterns: Optional[Sequence[FilePattern]] = None) -> List[Path]:
        """Download files from the shared folder into dest_dir.

        With ``patterns``, one file per pattern is pulled -- the newest match,
        renames tolerated (see select_items_by_patterns). With ``filenames``,
        the old exact-name behavior: only those names are pulled and it's an
        error if any are missing. With neither, every file (non-recursively).
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        items = self._list_items()

        if patterns:
            chosen = select_items_by_patterns(items, patterns)
            return [self._download_item(i, dest_dir) for i in chosen]

        downloaded: List[Path] = []
        for item in items:
            name = item.get("name")
            if "folder" in item or not name:
                continue
            if filenames and name not in filenames:
                continue
            downloaded.append(self._download_item(item, dest_dir))

        if filenames:
            missing = filenames - {p.name for p in downloaded}
            if missing:
                raise RuntimeError(
                    f"SharePoint folder is missing expected file(s): {sorted(missing)}")
        return downloaded
