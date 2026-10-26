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
    j.add_argument("--only", choices=["learned", "prophet"], help="restrict algorithm family")
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
        job = az.build_job(man, a.trials, a.timeout, a.concurrent,
                           allow=az.PRESETS.get(a.only) if a.only else None, tag=a.only or "")
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
    r = run_backtest(snap, a.target, a.horizon, a.min_train, a.stride, a.min_history)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out) / snap.pool / stamp
    out.mkdir(parents=True, exist_ok=True)
    r["predictions"].to_csv(out / "predictions.csv", index=False)
    r["scores"].to_csv(out / "scores.csv", index=False)
    pd.set_option("display.width", 160)
    print(f"{snap.pool}: {r['months']} months, {r['series']} ASINs, target={a.target}, horizon={a.horizon}")
    print("Forecast origins (last training month):", ", ".join(str(o.date()) for o in r["origins"]))
    print(f"Sell-out volume covered by scored series: {r['coverage']:.1%}\n")
    for (key, level), g in r["scores"].groupby(["answer_key", "level"]):
        print(f"--- scored against {key} ({level})")
        print(g.drop(columns=["answer_key", "level"]).sort_values("wape").to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
        print()
    print(f"Written to {out}")
    return 0
