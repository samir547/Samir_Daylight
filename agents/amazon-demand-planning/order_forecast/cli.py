"""python -m order_forecast pull --pool US --env-file ../amazon-sp-api-ingest/.env
   python -m order_forecast backtest --snapshot snapshots/US/<stamp>
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def _load_default_env(explicit=None) -> None:
    """Load this agent's own .env (agents/amazon-demand-planning/.env) unless --env-file is given.
    Variables already set in the shell win, so cron or CI can override."""
    from .db import load_env_file
    path = Path(explicit) if explicit else ROOT / ".env"
    if path.exists():
        load_env_file(path)
    elif explicit:
        raise SystemExit(f"--env-file {explicit} not found")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="order_forecast")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull", help="read-only pull of sales, inventory and invoices into a snapshot")
    p.add_argument("--pool", required=True, choices=["US", "UK"])
    p.add_argument("--env-file")
    p.add_argument("--include-pending", action="store_true", help="also read accounts marked pending")
    p.add_argument("--out", default=str(ROOT / "snapshots"))
    b = sub.add_parser("backtest", help="score baseline forecasters on a snapshot (offline)")
    b.add_argument("--snapshot", required=True)
    b.add_argument("--target", default="ordered_units", choices=["ordered_units", "shipped_units"])
    b.add_argument("--horizon", type=int, default=6)
    b.add_argument("--min-train", type=int, default=24)
    b.add_argument("--stride", type=int, help="months between origins (default = horizon, non-overlapping)")
    b.add_argument("--min-history", type=int, default=12)
    b.add_argument("--order-step", action="store_true", help="also score each method with the stock-based order step (+orders)")
    b.add_argument("--out", default=str(ROOT / "results"))
    e = sub.add_parser("automl-export", help="write AutoML train/horizon tables from a snapshot (offline)")
    e.add_argument("--snapshot", required=True)
    e.add_argument("--target", default="ordered_units", choices=["ordered_units", "shipped_units"])
    e.add_argument("--horizon", type=int, default=6)
    e.add_argument("--min-history", type=int, default=12)
    e.add_argument("--out", default=str(ROOT / "automl_export"))
    j = sub.add_parser("automl-job", help="register data and validate (default) or submit the AutoML job")
    j.add_argument("--export", required=True, help="folder written by automl-export")
    j.add_argument("--dry-run", action="store_true", help="build the job locally, touch nothing in Azure")
    j.add_argument("--submit", action="store_true", help="really submit (costs compute); default only validates")
    j.add_argument("--trials", type=int, default=20)
    j.add_argument("--timeout", type=int, default=60, help="total minutes")
    j.add_argument("--concurrent", type=int, default=2)
    j.add_argument("--only", choices=["learned", "prophet", "seasonal"], help="restrict to a preset family of models")
    j.add_argument("--models", help="comma-separated AutoML models to allow, e.g. SeasonalNaive,ExponentialSmoothing,Prophet")
    j.add_argument("--tag", default="", help="suffix for the job display name")
    f = sub.add_parser("automl-fetch", help="download a finished AutoML job and locate its predictions file")
    f.add_argument("--job", required=True, help="job name printed by automl-job --submit")
    f.add_argument("--best", help="name of the best trial (marked Best in the portal), if it is not detected")
    f.add_argument("--dest", default=str(ROOT / "results" / "automl"))
    sub.add_parser("init-db", help="create the forecast tables and views if missing (safe to repeat)")
    fc = sub.add_parser("forecast", help="make the production forecast from a snapshot (offline) and write a CSV")
    fc.add_argument("--snapshot", required=True)
    fc.add_argument("--settings", help="settings JSON (default config/forecast_settings.json)")
    fc.add_argument("--out", default=str(ROOT / "results" / "live"))
    rc = sub.add_parser("run-cycle", help="pull, forecast and load into the database: the monthly job")
    rc.add_argument("--pool", required=True, choices=["US", "UK"])
    rc.add_argument("--no-load", action="store_true", help="do everything except write to the database")
    rc.add_argument("--allow-stale", action="store_true", help="run even if the last closed month is not loaded yet")
    rc.add_argument("--force", action="store_true", help="load even if the sanity check fails")
    rc.add_argument("--settings")
    rc.add_argument("--out", default=str(ROOT / "snapshots"))
    pf = sub.add_parser("pull-forecast", help="read Amazon's stored weekly forecasts into an existing snapshot folder")
    pf.add_argument("--snapshot", required=True)
    ca = sub.add_parser("compare-amazon", help="compare our forecast for the coming months with Amazon's own (offline)")
    ca.add_argument("--snapshot", required=True)
    ca.add_argument("--target", default="ordered_units", choices=["ordered_units", "shipped_units"])
    ca.add_argument("--horizon", type=int, default=6)
    ca.add_argument("--generation-date", help="Amazon forecast version (default: latest)")
    s2 = sub.add_parser("automl-score", help="score downloaded AutoML predictions against the baselines")
    s2.add_argument("--snapshot", required=True)
    s2.add_argument("--predictions", required=True, help="predictions.csv with series_id, ds, y_predicted")
    s2.add_argument("--target", default="ordered_units", choices=["ordered_units", "shipped_units"])
    s2.add_argument("--horizon", type=int, default=6)
    s2.add_argument("--min-history", type=int, default=12)
    a = ap.parse_args(argv)
    _load_default_env(getattr(a, "env_file", None))

    if a.cmd == "automl-export":
        from .automl_export import build_tables, write_export
        from .snapshot import load_snapshot
        snap = load_snapshot(Path(a.snapshot))
        t = build_tables(snap, a.target, a.horizon, a.min_history)
        d = write_export(t, snap.pool, Path(a.out), a.target, a.horizon, a.min_history, str(a.snapshot))
        print(f"Series {t.train['series_id'].nunique()}, train rows {len(t.train)}, train_end {t.train_end.date()}, "
              f"holdout {t.horizon_months[0].date()}..{t.horizon_months[-1].date()}; "
              f"{len(t.too_short)} ASINs skipped (<{a.min_history} months)")
        print(f"Written to {d}  (holdout_actuals.csv stays local)")
        return 0

    if a.cmd == "automl-job":
        from . import azure_ml as az
        export = Path(a.export)
        man = az.read_manifest(export)
        allow = az.parse_models(a.models) if a.models else (az.PRESETS.get(a.only) if a.only else None)
        job = az.build_job(man, a.trials, a.timeout, a.concurrent, allow=allow, tag=a.tag or a.only or ("custom" if a.models else ""))
        if allow:
            print("Allowed models:", ", ".join(allow))
        print(f"Job: {job.display_name}  compute={az.COMPUTE} trials={a.trials} timeout={a.timeout}m concurrent={a.concurrent}")
        if a.dry_run:
            print("Dry run: nothing sent to Azure.")
            return 0
        c = az.ml_client()
        print("Registered:", ", ".join(az.register_assets(c, export)))
        c.jobs.validate(job, raise_on_failure=True)
        print("Validation passed.")
        if not a.submit:
            print("Not submitted (add --submit to run; this spends compute).")
            return 0
        r = c.jobs.create_or_update(job)
        print(f"Submitted {r.name}\n{r.studio_url}")
        return 0

    if a.cmd == "init-db":
        from .store import init_db
        applied, problems = init_db()
        print(f"Applied {applied} SQL batches (tables and views are created only if missing).")
        for pr in problems:
            print("PROBLEM:", pr)
        return 1 if problems else 0

    if a.cmd in ("forecast", "run-cycle"):
        return _forecast_and_maybe_load(a)

    if a.cmd == "pull-forecast":
        from .amazon_forecast import pull_forecast
        from .snapshot import load_snapshot
        snap = load_snapshot(Path(a.snapshot))
        path = pull_forecast(Path(a.snapshot), snap.manifest.get("marketplace", snap.pool))
        df = pd.read_csv(path)
        print(f"{len(df)} rows, {df['forecast_generation_date'].nunique()} version(s): "
              f"{', '.join(sorted(df['forecast_generation_date'].astype(str).unique()))}\nWritten to {path}")
        return 0

    if a.cmd == "compare-amazon":
        from .amazon_forecast import FILE, compare
        from .snapshot import load_snapshot
        snap = load_snapshot(Path(a.snapshot))
        f = Path(a.snapshot) / FILE
        if not f.exists():
            raise SystemExit(f"{f} not found. Run: python -m order_forecast pull-forecast --snapshot {a.snapshot}")
        r = compare(snap, pd.read_csv(f), a.target, a.horizon, generation_date=a.generation_date)
        pd.set_option("display.width", 160)
        print(f"Amazon forecast generated {pd.Timestamp(r['generation_date']).date()}; months compared: "
              f"{', '.join(str(m.date()) for m in r['months'])}; ASINs ours {r['asins_ours']}, matched {r['asins_matched']}")
        print("\n--- our forecast vs Amazon's, ASIN-month (wape_vs_amazon = how far we are from Amazon, not from actuals)")
        print(r["asin_level"].to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
        print("\n--- monthly total, our units / Amazon units")
        print(r["monthly_total"].pivot(index="month_start", columns="method", values="ratio").to_string(float_format=lambda x: f"{x:,.2f}"))
        return 0

    if a.cmd == "automl-fetch":
        from . import azure_ml as az
        found = az.fetch_predictions(az.ml_client(), a.job, Path(a.dest), a.best)
        if not found:
            raise SystemExit(f"No predictions file (series_id, ds, y_predicted) found under {a.dest}. Send me the folder listing.")
        print(f"Predictions: {found}")
        return 0

    if a.cmd == "automl-score":
        from .score_external import compare, external_predictions
        from .snapshot import load_snapshot
        snap = load_snapshot(Path(a.snapshot))
        ext = external_predictions(pd.read_csv(a.predictions), snap, a.horizon, "automl")
        r = compare(snap, ext, a.target, a.horizon, a.min_history)
        pd.set_option("display.width", 160)
        print(f"Compared on {r['series_compared']} ASINs ({len(r['missing_from_external'])} baseline ASINs not predicted by AutoML)")
        for (key, level), g in r["scores"].groupby(["answer_key", "level"]):
            print(f"--- scored against {key} ({level})")
            print(g.drop(columns=["answer_key", "level"]).sort_values("wape").to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
        return 0

    if a.cmd == "pull":
        from . import pull
        d = pull.pull_snapshot(a.pool, Path(a.out), include_pending=a.include_pending)
        print(f"Snapshot written to {d}")
        return 0

    from .backtest import run_backtest
    from .snapshot import load_snapshot
    snap = load_snapshot(Path(a.snapshot))
    r = run_backtest(snap, a.target, a.horizon, a.min_train, a.stride, a.min_history, order_step=a.order_step)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out) / snap.pool / stamp
    out.mkdir(parents=True, exist_ok=True)
    r["predictions"].to_csv(out / "predictions.csv", index=False)
    r["scores"].to_csv(out / "scores.csv", index=False)
    pd.set_option("display.width", 160)
    print(f"{snap.pool}: {r['months']} months, {r['series']} ASINs, target={a.target}, horizon={a.horizon}")
    print("Forecast origins (last training month):", ", ".join(str(o.date()) for o in r["origins"]))
    if r.get("order_step_stats"):
        print("Order step:", r["order_step_stats"])
    print(f"Sell-out volume covered by scored series: {r['coverage']:.1%}\n")
    for (key, level), g in r["scores"].groupby(["answer_key", "level"]):
        print(f"--- scored against {key} ({level})")
        print(g.drop(columns=["answer_key", "level"]).sort_values("wape").to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
        print()
    print(f"Written to {out}")
    return 0


EXIT_OK, EXIT_FAILED, EXIT_CANNOT_RUN, EXIT_DATA_NOT_READY, EXIT_SANITY = 0, 1, 2, 3, 4


def _write_live_csv(f, pool: str, out_root: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = Path(out_root) / pool / stamp
    d.mkdir(parents=True, exist_ok=True)
    path = d / "forecast.csv"
    f.table.assign(method=f.method, target=f.target, train_end=f.train_end.date()).to_csv(path, index=False)
    return path


def _forecast_and_maybe_load(a) -> int:
    from .live import build_live_forecast, data_is_current, load_settings, sanity_check, settings_for_pool
    from .snapshot import load_snapshot
    settings = load_settings(a.settings)
    try:
        if a.cmd == "run-cycle":
            from . import pull
            snap_dir = pull.pull_snapshot(a.pool, Path(a.out))
            print(f"Snapshot: {snap_dir}")
        else:
            snap_dir = Path(a.snapshot)
        snap = load_snapshot(snap_dir)
        if a.cmd == "run-cycle" and not a.allow_stale and not data_is_current(snap):
            from .live import expected_last_closed_month
            print(f"DATA NOT READY: the last closed month loaded is {snap.months[-1].date()}, "
                  f"expected {expected_last_closed_month().date()}. Nothing was forecast or written.")
            return EXIT_DATA_NOT_READY
        settings = settings_for_pool(settings, snap.pool)
        f = build_live_forecast(snap, settings)
        print(f"{snap.pool}: method {f.method}, target {f.target}, data to {f.train_end.date()}, "
              f"{f.table['asin'].nunique()} ASINs forecast ({f.skipped_short} skipped: under {settings['min_history']} months), "
              f"{f.horizon} months, total {f.table['units_forecast'].sum():,.0f} units, sanity ratio {f.sanity_ratio:.2f}")
        reason = sanity_check(f, settings)
        if reason and not getattr(a, "force", False):
            print(f"SANITY CHECK FAILED: {reason}. Nothing was written. Use --force only after checking the data.")
            return EXIT_SANITY
        out = _write_live_csv(f, snap.pool, getattr(a, "out", str(ROOT / "results" / "live")) if a.cmd == "forecast"
                              else str(ROOT / "results" / "live"))
        print(f"Forecast CSV: {out}")
        if a.cmd == "run-cycle" and not a.no_load:
            from .store import load_forecast
            marketplace = snap.manifest.get("marketplace", snap.pool)
            run_id = load_forecast(f, marketplace, snapshot_ref=snap_dir.name)
            print(f"Loaded run {run_id} into dbo.amazon_order_forecast ({len(f.table)} rows).")
        return EXIT_OK
    except SystemExit:
        raise
    except RuntimeError as e:        # missing settings or credentials
        print(f"CANNOT RUN: {e}")
        return EXIT_CANNOT_RUN
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return EXIT_FAILED
