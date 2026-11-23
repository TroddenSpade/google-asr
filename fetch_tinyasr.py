#!/usr/bin/env python3
"""Fetch tinyasr models (the on-device ASR stack used by Telegram clients).

tinyasr is GPL-3.0-or-later, including the model files. Downloading them is
fine for local use; distributing an app that bundles them triggers the GPL.

    python fetch_tinyasr.py             # models only
    python fetch_tinyasr.py --runtime   # also build libtinyasr.so (needs cargo)
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
BASE = "https://raw.githubusercontent.com/dkaraush/tinyasr/main/models"
REPO = "https://github.com/dkaraush/tinyasr"

FILES = {
    "en-fa.tzf.gz": "en-fa.tzf",          # Persian + English, 60M, ~30 MB gzip
    "silero_vad.onnx.gz": "silero_vad.onnx",
    "tinylid.bin.gz": "tinylid.bin",      # optional language identification
}


def fetch(name: str, target: str) -> None:
    destination = MODELS / target
    if destination.exists() and destination.stat().st_size > 0:
        print(f"  {target}: already present")
        return

    archive = MODELS / name
    print(f"  {name}: downloading")
    with urllib.request.urlopen(f"{BASE}/{name}", timeout=120) as response:
        with archive.open("wb") as handle:
            shutil.copyfileobj(response, handle)

    with gzip.open(archive, "rb") as source, destination.open("wb") as handle:
        shutil.copyfileobj(source, handle)
    archive.unlink()
    print(f"  {target}: {destination.stat().st_size / 1e6:.1f} MB")


def build_runtime() -> None:
    cargo = shutil.which("cargo")
    if cargo is None:
        raise SystemExit(
            "cargo not found. Install Rust (https://rustup.rs), then rerun "
            "`python fetch_tinyasr.py --runtime`."
        )
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is required to fetch the tinyasr source.")

    with tempfile.TemporaryDirectory(prefix="tinyasr-") as workdir:
        print("  building libtinyasr.so")
        subprocess.run(
            [git, "clone", "--depth", "1", REPO, workdir],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [cargo, "rustc", "--release", "--features", "c-api", "--crate-type", "cdylib"],
            cwd=workdir,
            check=True,
        )
        shutil.copy(
            Path(workdir) / "target/release/libtinyasr.so",
            MODELS / "libtinyasr.so",
        )
    print(f"  libtinyasr.so: {(MODELS / 'libtinyasr.so').stat().st_size / 1e6:.1f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="also clone tinyasr and build models/libtinyasr.so",
    )
    args = parser.parse_args()

    MODELS.mkdir(parents=True, exist_ok=True)
    for name, target in FILES.items():
        fetch(name, target)

    if args.runtime:
        build_runtime()

    print("done. Test with: python tinyasr_engine.py sample.wav --lang fa")
    return 0


if __name__ == "__main__":
    sys.exit(main())
