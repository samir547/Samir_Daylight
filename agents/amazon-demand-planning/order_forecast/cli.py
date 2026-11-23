"""python -m order_forecast pull --pool US --env-file ../amazon-sp-api-ingest/.env
   python -m order_forecast backtest --snapshot snapshots/US/<stamp>
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .errors import EXIT_CANNOT_RUN, ConfigError
from .pull import POOL_MARKETPLACES

ROOT = Path(__file__).resolve().parent.parent
POOLS = sorted(POOL_MARKETPLACES)


def _load_default_env(explicit=None) -> None:
    """Load this agent's own .env (agents/amazon-demand-planning/.env) unless --env-file is given.
    Variables already set in the shell win, so cron or CI can override."""
    from .db import load_env_file
    path = Path(explicit) if explicit else ROOT / ".env"
    if path.exists():
        load_env_file(path)
    elif explicit:
        raise ConfigError(f"--env-file {explicit} not found")


def main(argv=None) -> int:
    """Run one command. A configuration error (unsupported pool, unknown method, missing settings file, bad
    adjustments file) prints CANNOT RUN and returns 2, which scripts/run_monthly.sh does not retry."""
    try:
        return _main(argv)
    except ConfigError as e:
        print(f"CANNOT RUN: {e}")
        return EXIT_CANNOT_RUN


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="order_forecast")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull", help="read-only pull of sales, inventory and invoices into a snapshot")
    p.add_argument("--pool", required=True, choices=POOLS)
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
    b.add_argument("--replenish", action="store_true",
                   help="also score the replenishment replay (method 'replenish'; uses our own demand forecast)")
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
    rc.add_argument("--pool", required=True, choices=POOLS)
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
    r = run_backtest(snap, a.target, a.horizon, a.min_train, a.stride, a.min_history, order_step=a.order_step,
                     replenish=a.replenish)
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


EXIT_OK, EXIT_FAILED, EXIT_DATA_NOT_READY, EXIT_SANITY = 0, 1, 3, 4      # EXIT_CANNOT_RUN = 2 (errors.py)


def _write_live_csv(f, pool: str, out_root: str, snapshot_ref: str = "") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = Path(out_root) / pool / stamp
    d.mkdir(parents=True, exist_ok=True)
    path = d / "forecast.csv"
    f.table.assign(method=f.method, target=f.target, train_end=f.train_end.date()).to_csv(path, index=False)
    if "habits" in f.extras:              # replenish: what was learned per ASIN, so a number can be explained
        f.extras["habits"].to_csv(d / "habits.csv", index=False)
    if "unadjusted" in f.extras:          # replenish: the same forecast without the client adjustments
        u = f.extras["unadjusted"]
        u.table.assign(method=u.method, target=u.target, train_end=u.train_end.date()).to_csv(d / "forecast_unadjusted.csv", index=False)
    if "decisions" in f.extras:           # replenish: every ASIN seen, forecast or not, and why
        f.extras["decisions"].to_csv(d / "asin_decisions.csv", index=False)
    if "summary" in f.extras:             # replenish: the run in numbers
        summary = {"pool": pool, "snapshot_ref": snapshot_ref or None, "written_at_utc": stamp, **f.extras["summary"]}
        if "unadjusted" in f.extras and "summary" in f.extras["unadjusted"].extras:
            summary["unadjusted"] = f.extras["unadjusted"].extras["summary"]
        (d / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


def _print_explanation(f, settings: dict) -> None:
    """The key lines of the run summary, for the cron log."""
    s = f.extras.get("summary")
    if not s:
        return
    print(f"Explain: sales ASINs {s['asins_in_sales']}, forecast {s['asins_forecast']} "
          f"({s['asins_forecast_standard']} standard + {s['young_product_backups']} young-product backups), "
          f"skipped {s['skipped_short_history']} (under {s['young_min_months']} months) + {s['skipped_no_sales']} (no sales); "
          f"without Amazon forecast {s['asins_without_amazon_forecast']}, sales-only (stock unknown) {s['sales_only_asins']}, "
          f"default habits {s['default_habit_asins']}; not forecast: {s['amazon_only_dropped']} only in Amazon's forecast, "
          f"{s['inventory_only_dropped']} only in inventory")
    pct = lambda x: "n/a" if x is None else f"{x:.1%}"
    num = lambda x: "n/a" if x is None else f"{x:.2f}"
    print(f"Explain: seasonality {s['seasonality_mode']} ({s['market_months']} months of history); "
          f"Amazon forecast {s['amazon_generation_date'] or 'none'}; sanity ratio {num(s['sanity_ratio'])} on the forecast "
          f"ASINs (all ASINs {num(s['sanity_ratio_all_asins'])}; skipped ASINs' share of last-12-month sales "
          f"{pct(s['skipped_sales_share'])}); adjustments file {'found' if s['adjustments_file_found'] else 'not found'}, "
          f"{s['adjustment_rows']} rows for this market")
    defaults = [n for n, k in (("cover", "habit_market_default_cover"), ("order size", "habit_market_default_lot"),
                               ("open-PO share", "po_share_default"), ("P80/P90 uplift", "scenario_ratios_default")) if s[k]]
    print(f"Explain: market-level defaults used: {', '.join(defaults) if defaults else 'none'}; "
          f"reason codes {s['reason_codes']}; confidence {s['confidence_rows']}")


def _forecast_and_maybe_load(a) -> int:
    from .live import build_live_forecast, data_is_current, load_settings, sanity_verdict, settings_for_pool
    from .snapshot import load_snapshot
    settings = load_settings(a.settings)
    try:
        if a.cmd == "run-cycle":
            from . import pull
            snap_dir = pull.pull_snapshot(a.pool, Path(a.out))
            print(f"Snapshot: {snap_dir}")
            if settings_for_pool(settings, a.pool)["method"] == "replenish":     # needs Amazon's weekly forecast too
                from .amazon_forecast import pull_forecast
                from .snapshot import load_snapshot as _ls
                fpath = pull_forecast(snap_dir, _ls(snap_dir).manifest.get("marketplace", a.pool))
                print(f"Amazon forecast: {fpath}")
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
        skipped_rule = (f"under {settings.get('young_min_months', 3)} months of sales or no sales"
                        if "summary" in f.extras else f"under {settings['min_history']} months")
        print(f"{snap.pool}: method {f.method}, target {f.target}, data to {f.train_end.date()}, "
              f"{f.table['asin'].nunique()} ASINs forecast ({f.skipped_short} skipped: {skipped_rule}), "
              f"{f.horizon} months, total {f.table['units_forecast'].sum():,.0f} units, sanity ratio {f.sanity_ratio:.2f}")
        if f.extras.get("stats"):
            print(f"Replenish: {f.extras['stats']}; Amazon forecast used: {f.extras['amazon_used']}; "
                  f"P80/P90 uplift when Amazon is missing: {f.extras['scenario_ratios'][0]:.2f}/{f.extras['scenario_ratios'][1]:.2f}; "
                  f"open orders received next month: {f.extras['po_share']:.0%}")
        _print_explanation(f, settings)
        verdict, reason = sanity_verdict(f, settings)
        forced = verdict == "block" and getattr(a, "force", False)
        if verdict == "block" and not forced:
            print(f"SANITY CHECK FAILED: {reason}. Nothing was written. Use --force only after checking the data.")
            return EXIT_SANITY
        status = "forced" if forced else verdict         # ok | warn | forced: stored with the run (sql/007)
        if status == "warn":
            print(f"SANITY WARNING: {reason}")
        elif forced:
            print(f"SANITY CHECK OVERRIDDEN with --force: {reason}")
        if "summary" in f.extras:
            f.extras["summary"]["sanity_status"] = status
            f.extras["summary"]["sanity_warning"] = reason
        out = _write_live_csv(f, snap.pool, getattr(a, "out", str(ROOT / "results" / "live")) if a.cmd == "forecast"
                              else str(ROOT / "results" / "live"), snapshot_ref=snap_dir.name)
        print(f"Forecast CSV: {out}")
        if a.cmd == "run-cycle" and not a.no_load:
            from .store import load_forecast
            marketplace = snap.manifest.get("marketplace", snap.pool)
            if "unadjusted" in f.extras:      # keep the no-adjustments forecast beside it, so the effect of the notes is visible
                u = f.extras["unadjusted"]      # loaded FIRST: the view's is_latest takes the newest run per marketplace
                print(f"Loaded run {load_forecast(u, marketplace, snapshot_ref=snap_dir.name)} ({u.method}).")
            run_id = load_forecast(f, marketplace, snapshot_ref=snap_dir.name)
            print(f"Loaded run {run_id} into dbo.amazon_order_forecast ({len(f.table)} rows).")
        return EXIT_OK
    except SystemExit:            # ConfigError included: main() turns it into exit code 2
        raise
    except RuntimeError as e:        # missing settings or credentials
        print(f"CANNOT RUN: {e}")
        return EXIT_CANNOT_RUN
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return EXIT_FAILED
