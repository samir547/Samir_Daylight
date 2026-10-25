"""python -m order_forecast pull --pool US --env-file ../amazon-sp-api-ingest/.env
   python -m order_forecast backtest --snapshot snapshots/US/<stamp>
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


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
    a = ap.parse_args(argv)

    if a.cmd == "pull":
        from . import db, pull
        if a.env_file:
            db.load_env_file(a.env_file)
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
