"""SharePoint "In Progress" -> "Processed" flow, fully offline: subfolder
resolution, the move call, skip-on-empty in sync_from_subfolders, and the
CLI wiring that decides which loaders run and when files get moved.

The HTTP layer (requests.get / requests.patch) and token acquisition are
monkeypatched -- nothing here touches Graph."""
import io
import logging
import os
import sys
from pathlib import Path

import pytest

from daylight_sentiment import cli
from daylight_sentiment.application.checkpoint import load_checkpoint
from daylight_sentiment.application.pipeline import MultiSourcePipeline
from daylight_sentiment.config import Config, FilePattern, SourceFiles
from daylight_sentiment.infra import sharepoint_sync
from daylight_sentiment.infra.sharepoint_sync import (
    GRAPH_BASE,
    SIGN_IN_RECOVERY_COMMAND,
    NoMatchingFileError,
    SharePointSignInRequiredError,
    SharePointSync,
    SourceSpec,
    SourceSyncResult,
    select_items_by_patterns,
)

TP_PRODUCT = FilePattern(("trustpilot", "product"), ".xlsx")
TP_SERVICE = FilePattern(("trustpilot", "service"), ".xlsx")
ROOT_ITEM = {"id": "ROOT", "folder": {}, "parentReference": {"driveId": "DRIVE"}}


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture
def syncer(monkeypatch):
    s = SharePointSync("https://x.sharepoint.com/:f:/p/u/abc", "u@x.com", "pw", "client")
    monkeypatch.setattr(s, "_get_token", lambda: "TOKEN")
    return s


class TestResolveSubfolder:
    def test_addresses_by_colon_path_and_injects_drive_id(self, syncer, monkeypatch):
        calls = []

        def fake_get(url, headers=None, timeout=None, **kw):
            calls.append((url, headers))
            if "/shares/" in url:
                return _Resp(ROOT_ITEM)
            return _Resp({"id": "FOLDER1", "name": "Processed", "folder": {}})

        monkeypatch.setattr("requests.get", fake_get)
        item = syncer._resolve_subfolder("TrustPilot/Product/Processed")

        assert item["id"] == "FOLDER1"
        assert item["parentReference"]["driveId"] == "DRIVE"
        assert calls[1][0] == f"{GRAPH_BASE}/drives/DRIVE/items/ROOT:/TrustPilot/Product/Processed"
        assert calls[1][1]["Authorization"] == "Bearer TOKEN"

    def test_spaces_in_path_are_url_encoded(self, syncer, monkeypatch):
        urls = []
        monkeypatch.setattr("requests.get", lambda url, **kw: (
            urls.append(url) or _Resp(ROOT_ITEM if "/shares/" in url else {"id": "F"})))
        syncer._resolve_subfolder("Amazon/In Progress")
        assert urls[1].endswith("/items/ROOT:/Amazon/In%20Progress")

    def test_missing_folder_raises(self, syncer, monkeypatch):
        import requests
        monkeypatch.setattr("requests.get", lambda url, **kw: (
            _Resp(ROOT_ITEM) if "/shares/" in url else _Resp({}, status=404)))
        with pytest.raises(requests.HTTPError):
            syncer._resolve_subfolder("Does/Not/Exist")


class TestMoveItem:
    ITEM = {"id": "FILE1", "name": "trustpilot-product-reviews 901.xlsx",
            "parentReference": {"driveId": "DRIVE"}}
    DEST = {"id": "FOLDER1", "name": "Processed"}

    def test_patches_parent_with_replace(self, syncer, monkeypatch, caplog):
        calls = []

        def fake_patch(url, headers=None, json=None, timeout=None):
            calls.append((url, headers, json))
            return _Resp({})

        monkeypatch.setattr("requests.patch", fake_patch)
        with caplog.at_level(logging.INFO, logger="multi_source_sentiment"):
            syncer.move_item(self.ITEM, self.DEST)

        assert len(calls) == 1
        url, headers, body = calls[0]
        assert url == f"{GRAPH_BASE}/drives/DRIVE/items/FILE1"
        assert headers["Authorization"] == "Bearer TOKEN"
        assert body == {
            "parentReference": {"id": "FOLDER1"},
            "name": "trustpilot-product-reviews 901.xlsx",
            "@microsoft.graph.conflictBehavior": "replace",
        }
        assert "Moved trustpilot-product-reviews 901.xlsx to Processed" in caplog.text

    def test_conflict_behavior_is_overridable(self, syncer, monkeypatch):
        bodies = []
        monkeypatch.setattr("requests.patch", lambda url, headers=None, json=None, timeout=None: (
            bodies.append(json) or _Resp({})))
        syncer.move_item(self.ITEM, self.DEST, conflict_behavior="fail")
        assert bodies[0]["@microsoft.graph.conflictBehavior"] == "fail"

    def test_http_failure_propagates(self, syncer, monkeypatch):
        import requests
        monkeypatch.setattr("requests.patch", lambda *a, **kw: _Resp({}, status=403))
        with pytest.raises(requests.HTTPError):
            syncer.move_item(self.ITEM, self.DEST)


class TestSyncFromSubfolders:
    SPECS = [
        SourceSpec("trustpilot_product", "TrustPilot/Product/In Progress", TP_PRODUCT),
        SourceSpec("trustpilot_service", "TrustPilot/Service/In Progress", TP_SERVICE),
    ]
    PRODUCT_ITEM = {"id": "FILE1", "name": "trustpilot-product-reviews 901.xlsx",
                    "lastModifiedDateTime": "2026-09-20T00:00:00Z",
                    "parentReference": {"driveId": "DRIVE"}}

    def test_no_match_is_typed_runtime_error(self):
        with pytest.raises(NoMatchingFileError):
            select_items_by_patterns([], [TP_PRODUCT])
        assert issubclass(NoMatchingFileError, RuntimeError)

    def test_empty_folder_is_skipped_not_raised(self, syncer, monkeypatch, tmp_path, caplog):
        listed = {"TrustPilot/Product/In Progress": [self.PRODUCT_ITEM],
                  "TrustPilot/Service/In Progress": []}
        monkeypatch.setattr(syncer, "_list_items_in", lambda sub: listed[sub])
        downloaded = []

        def fake_download(item, dest_dir):
            downloaded.append(item["name"])
            return dest_dir / item["name"]

        monkeypatch.setattr(syncer, "_download_item", fake_download)

        with caplog.at_level(logging.INFO, logger="multi_source_sentiment"):
            results = syncer.sync_from_subfolders(tmp_path, self.SPECS)

        assert set(results) == {"trustpilot_product", "trustpilot_service"}
        prod, svc = results["trustpilot_product"], results["trustpilot_service"]
        assert prod.synced and prod.item is self.PRODUCT_ITEM
        assert prod.local_path == tmp_path / "trustpilot-product-reviews 901.xlsx"
        assert not svc.synced and svc.item is None and svc.local_path is None
        assert svc.subpath == "TrustPilot/Service/In Progress"
        assert downloaded == ["trustpilot-product-reviews 901.xlsx"]
        assert ("No new file waiting in SharePoint's TrustPilot/Service/In Progress for "
                "trustpilot_service -- skipping this source for this run") in caplog.text

    def test_excel_lock_files_are_never_downloaded(self, syncer, monkeypatch, tmp_path):
        # Someone has the export open in Excel: its "~$" owner file sits next
        # to it (newer) in one folder, and alone in the other.
        lock = {"id": "LOCK", "name": "~$trustpilot-product-reviews 901.xlsx",
                "lastModifiedDateTime": "2026-09-21T00:00:00Z"}
        listed = {"TrustPilot/Product/In Progress": [self.PRODUCT_ITEM, lock],
                  "TrustPilot/Service/In Progress": [
                      {"id": "LOCK2", "name": "~$TrustPilot Service Reviews.xlsx"}]}
        monkeypatch.setattr(syncer, "_list_items_in", lambda sub: listed[sub])
        downloaded = []
        monkeypatch.setattr(syncer, "_download_item",
                            lambda item, d: downloaded.append(item["name"]) or d / item["name"])
        results = syncer.sync_from_subfolders(tmp_path, self.SPECS)
        assert results["trustpilot_product"].item is self.PRODUCT_ITEM
        assert not results["trustpilot_service"].synced
        assert downloaded == ["trustpilot-product-reviews 901.xlsx"]

    def test_all_empty_completes_with_nothing_synced(self, syncer, monkeypatch, tmp_path):
        monkeypatch.setattr(syncer, "_list_items_in", lambda sub: [])
        monkeypatch.setattr(syncer, "_download_item",
                            lambda *a: pytest.fail("nothing should download"))
        results = syncer.sync_from_subfolders(tmp_path, self.SPECS)
        assert len(results) == 2
        assert not any(r.synced for r in results.values())

    def test_other_runtime_errors_still_abort(self, syncer, monkeypatch, tmp_path):
        # A sign-in failure surfaces as a plain RuntimeError from the token
        # path -- that must NOT be mistaken for "folder is empty".
        def boom(sub):
            raise RuntimeError("SharePoint sign-in failed")
        monkeypatch.setattr(syncer, "_list_items_in", boom)
        with pytest.raises(RuntimeError, match="sign-in failed"):
            syncer.sync_from_subfolders(tmp_path, self.SPECS)


def _synced(key, subpath, name):
    item = {"id": f"id-{key}", "name": name, "parentReference": {"driveId": "DRIVE"}}
    return SourceSyncResult(key, subpath, Path(name), item)


def _skipped(key, subpath):
    return SourceSyncResult(key, subpath)


class TestCliActiveSources:
    def test_no_sync_means_all_three_loaders(self):
        names = [s.name for s in cli.active_sources(Config(), None)]
        assert names == ["trustpilot_product", "trustpilot_service", "amazon_brightdata"]

    def test_skipped_sources_are_dropped_from_loaders(self):
        sync_results = {
            "trustpilot_product": _skipped("trustpilot_product", "TrustPilot/Product/In Progress"),
            "trustpilot_service": _synced("trustpilot_service", "TrustPilot/Service/In Progress",
                                          "TrustPilot Service Reviews.xlsx"),
            "amazon_brightdata": _skipped("amazon_brightdata", "Amazon/In Progress"),
        }
        names = [s.name for s in cli.active_sources(Config(), sync_results)]
        assert names == ["trustpilot_service"]

    def test_all_skipped_means_no_loaders(self):
        sync_results = {k: _skipped(k, "x") for k in
                        ("trustpilot_product", "trustpilot_service", "amazon_brightdata")}
        assert cli.active_sources(Config(), sync_results) == []

    def test_spec_keys_match_loader_names(self):
        spec_keys = {s.key for s in cli.sharepoint_source_specs(Config())}
        loader_names = {s.name for s in cli.active_sources(Config(), None)}
        assert spec_keys == loader_names
        assert set(cli.sharepoint_processed_dirs(Config())) == loader_names


class TestCliMoveToProcessed:
    class _FakeSyncer:
        def __init__(self):
            self.resolved, self.moved = [], []

        def _resolve_subfolder(self, subpath):
            self.resolved.append(subpath)
            return {"id": f"folder:{subpath}", "name": subpath.rsplit("/", 1)[-1]}

        def move_item(self, item, dest_folder, conflict_behavior="replace"):
            self.moved.append((item["id"], dest_folder["id"], conflict_behavior))

    @pytest.fixture
    def fake(self, monkeypatch):
        fake = self._FakeSyncer()
        monkeypatch.setattr(cli, "_build_syncer", lambda config: fake)
        return fake

    def test_only_synced_sources_move_to_their_processed_dir(self, fake):
        sync_results = {
            "trustpilot_product": _synced("trustpilot_product", "TrustPilot/Product/In Progress",
                                          "trustpilot-product-reviews 901.xlsx"),
            "trustpilot_service": _skipped("trustpilot_service", "TrustPilot/Service/In Progress"),
            "amazon_brightdata": _synced("amazon_brightdata", "Amazon/In Progress",
                                         "amazon_product_reviews_all.csv"),
        }
        cli.move_synced_files_to_processed(Config(), sync_results)
        assert fake.resolved == ["TrustPilot/Product/Processed", "Amazon/Processed"]
        assert fake.moved == [
            ("id-trustpilot_product", "folder:TrustPilot/Product/Processed", "replace"),
            ("id-amazon_brightdata", "folder:Amazon/Processed", "replace"),
        ]

    def test_processed_dir_overrides_are_honoured(self, fake):
        config = Config(sources=SourceFiles(sp_amazon_product_processed_dir="Amazon/Done"))
        sync_results = {"amazon_brightdata": _synced("amazon_brightdata", "Amazon/In Progress",
                                                     "amazon_product_reviews_all.csv")}
        cli.move_synced_files_to_processed(config, sync_results)
        assert fake.resolved == ["Amazon/Done"]

    def test_nothing_synced_touches_nothing(self, fake, caplog):
        sync_results = {k: _skipped(k, "x") for k in
                        ("trustpilot_product", "trustpilot_service", "amazon_brightdata")}
        with caplog.at_level(logging.INFO, logger="multi_source_sentiment"):
            cli.move_synced_files_to_processed(Config(), sync_results)
        assert fake.resolved == [] and fake.moved == []
        assert "No SharePoint files to move" in caplog.text


class TestProcessedDirConfig:
    def test_defaults(self):
        src = SourceFiles()
        assert src.sp_trustpilot_product_processed_dir == "TrustPilot/Product/Processed"
        assert src.sp_trustpilot_service_processed_dir == "TrustPilot/Service/Processed"
        assert src.sp_amazon_product_processed_dir == "Amazon/Processed"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("SP_TRUSTPILOT_PRODUCT_PROCESSED_DIR", "TP/P/Done")
        monkeypatch.setenv("SP_TRUSTPILOT_SERVICE_PROCESSED_DIR", "TP/S/Done")
        monkeypatch.setenv("SP_AMAZON_PRODUCT_PROCESSED_DIR", "AMZ/Done")
        src = SourceFiles.from_env()
        assert src.sp_trustpilot_product_processed_dir == "TP/P/Done"
        assert src.sp_trustpilot_service_processed_dir == "TP/S/Done"
        assert src.sp_amazon_product_processed_dir == "AMZ/Done"
        # The In Progress dirs still fall back to their defaults.
        assert src.sp_amazon_product_dir == "Amazon/In Progress"


class TestCheckpointSurvivesPartialRun:
    """A run that loads no (or only some) sources must not truncate the other
    sources' checkpoint entries -- that would force a full paid re-analysis
    on the next run that does have their files."""

    def test_zero_reviews_with_explicit_resume_keeps_checkpoint(self, tmp_path):
        ckpt = tmp_path / "c.jsonl"
        ckpt.write_text('{"uid": "trustpilot:product:1", "analysis": {"sentiment": "positive"}}\n',
                        encoding="utf-8")
        pipeline = MultiSourcePipeline(Config(), analyzer=None)
        results = pipeline.process_all_reviews([], ckpt, done_uids={}, resume=True)
        assert results == []
        assert set(load_checkpoint(ckpt)) == {"trustpilot:product:1"}

    def test_default_resume_still_derives_from_done(self, tmp_path):
        # Unchanged behavior for callers that don't pass resume.
        ckpt = tmp_path / "c.jsonl"
        ckpt.write_text('{"uid": "stale", "analysis": {"sentiment": "positive"}}\n',
                        encoding="utf-8")
        MultiSourcePipeline(Config(), analyzer=None).process_all_reviews([], ckpt, done_uids={})
        assert load_checkpoint(ckpt) == {}


class _FakeMsalApp:
    """Stands in for msal.PublicClientApplication: records which sign-in
    paths were tried and never touches the network or blocks."""

    def __init__(self, cached=False, ropc_result=None):
        self.cached = cached
        self.ropc_result = ropc_result or {
            "error": "invalid_grant", "error_description": "AADSTS50076: MFA required"}
        self.calls = []

    def get_accounts(self, username=None):
        self.calls.append("get_accounts")
        return [{"username": username}] if self.cached else []

    def acquire_token_silent(self, scopes, account=None):
        self.calls.append("silent")
        return {"access_token": "CACHED"} if self.cached else None

    def acquire_token_by_username_password(self, username, password, scopes=None):
        self.calls.append("ropc")
        return self.ropc_result

    def initiate_device_flow(self, scopes=None):
        self.calls.append("initiate_device_flow")
        return {"user_code": "ABC123", "message": "Go to https://microsoft.com/devicelogin"}

    def acquire_token_by_device_flow(self, flow):
        self.calls.append("device_flow")
        return {"access_token": "DEVICE"}


class TestSignInGuard:
    def _syncer(self, monkeypatch, tmp_path, app, interactive, password="pw"):
        monkeypatch.setattr(SharePointSync, "TOKEN_CACHE_PATH", tmp_path / ".sp_token_cache.json")
        s = SharePointSync("https://x.sharepoint.com/:f:/p/u/abc", "u@x.com", password,
                           "client", interactive=interactive)
        monkeypatch.setattr(s, "_build_app", lambda cache: app)
        return s

    def test_unattended_without_cached_token_fails_fast(self, monkeypatch, tmp_path):
        app = _FakeMsalApp(cached=False)
        s = self._syncer(monkeypatch, tmp_path, app, interactive=False)
        with pytest.raises(SharePointSignInRequiredError) as err:
            s._get_token()
        assert "initiate_device_flow" not in app.calls
        assert "device_flow" not in app.calls
        assert SIGN_IN_RECOVERY_COMMAND in str(err.value)
        assert "no one is at a terminal" in str(err.value)

    def test_unattended_without_password_also_fails_fast(self, monkeypatch, tmp_path):
        app = _FakeMsalApp(cached=False)
        s = self._syncer(monkeypatch, tmp_path, app, interactive=False, password=None)
        with pytest.raises(SharePointSignInRequiredError):
            s._get_token()
        assert app.calls == ["get_accounts"]

    def test_unattended_with_cached_token_signs_in_silently(self, monkeypatch, tmp_path):
        app = _FakeMsalApp(cached=True)
        s = self._syncer(monkeypatch, tmp_path, app, interactive=False)
        assert s._get_token() == "CACHED"
        assert app.calls == ["get_accounts", "silent"]

    def test_interactive_without_cached_token_still_uses_device_code(self, monkeypatch, tmp_path):
        app = _FakeMsalApp(cached=False)
        s = self._syncer(monkeypatch, tmp_path, app, interactive=True)
        assert s._get_token() == "DEVICE"
        assert app.calls == ["get_accounts", "ropc",
                             "initiate_device_flow", "device_flow"]

    def test_non_mfa_password_failure_is_unchanged(self, monkeypatch, tmp_path):
        app = _FakeMsalApp(cached=False, ropc_result={
            "error": "invalid_grant", "error_description": "AADSTS50126: bad password"})
        s = self._syncer(monkeypatch, tmp_path, app, interactive=False)
        with pytest.raises(RuntimeError, match="AADSTS50126") as err:
            s._get_token()
        assert not isinstance(err.value, SharePointSignInRequiredError)

    def test_is_a_runtime_error(self):
        assert issubclass(SharePointSignInRequiredError, RuntimeError)


class _Tty(io.StringIO):
    def isatty(self):
        return True


class TestInteractiveDetection:
    def test_none_stdin_is_unattended(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", None)
        assert sharepoint_sync._stdin_is_interactive() is False

    def test_closed_stdin_is_unattended(self, monkeypatch):
        closed = _Tty()
        closed.close()
        monkeypatch.setattr(sys, "stdin", closed)
        assert sharepoint_sync._stdin_is_interactive() is False

    def test_non_tty_stdin_is_unattended(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert sharepoint_sync._stdin_is_interactive() is False

    def test_tty_stdin_is_interactive(self, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(sys, "stdin", _Tty())
        assert sharepoint_sync._stdin_is_interactive() is True

    def test_devnull_stdin_is_unattended(self, monkeypatch):
        # `< /dev/null` under cron; on Windows NUL even claims isatty().
        with open(os.devnull, encoding="utf-8") as devnull:
            monkeypatch.setattr(sys, "stdin", devnull)
            assert sharepoint_sync._stdin_is_interactive() is False

    def test_auto_detect_is_the_default(self, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        s = SharePointSync("u", "u@x.com", "pw", "client")
        monkeypatch.setattr(sys, "stdin", None)
        assert s._is_interactive() is False
        monkeypatch.setattr(sys, "stdin", _Tty())
        assert s._is_interactive() is True

    def test_explicit_setting_wins(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", None)
        assert SharePointSync("u", "u@x.com", "pw", "c", interactive=True)._is_interactive()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
class TestTokenCachePermissions:
    def test_new_cache_file_is_owner_only(self, tmp_path):
        path = tmp_path / ".sp_token_cache.json"
        sharepoint_sync._write_private(path, "{}")
        assert path.read_text(encoding="utf-8") == "{}"
        assert path.stat().st_mode & 0o777 == 0o600

    def test_existing_cache_file_is_tightened(self, tmp_path):
        path = tmp_path / ".sp_token_cache.json"
        path.write_text("old", encoding="utf-8")
        path.chmod(0o644)
        sharepoint_sync._write_private(path, "new")
        assert path.read_text(encoding="utf-8") == "new"
        assert path.stat().st_mode & 0o777 == 0o600
