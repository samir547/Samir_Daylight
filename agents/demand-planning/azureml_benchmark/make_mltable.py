"""
Write the MLTable definition for a snapshot's training folder.

An MLTable is a small YAML file saying where the data is and how to load it
(here: read train.parquet, path relative to the folder). Azure ML AutoML
forecasting takes its training data as an MLTable. ONLY the training folder gets
one — test actuals live in a different folder and are never uploaded as training
input.

If this fails with a proxy-parsing error from `rslex`, unset the proxy
variables for the call (NO_PROXY / HTTP(S)_PROXY) — mltable initialises a
network-capable data-store resolver even for local files.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import mltable


def write_mltable(train_dir: Path, parquet_name: str = "train.parquet") -> Path:
    train_dir = Path(train_dir)
    if not (train_dir / parquet_name).exists():
        raise FileNotFoundError(train_dir / parquet_name)
    table = mltable.from_parquet_files([{"file": str((train_dir / parquet_name).resolve())}])
    table.save(str(train_dir))
    return train_dir / "MLTable"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("train_dir", type=Path)
    print(write_mltable(ap.parse_args().train_dir))
