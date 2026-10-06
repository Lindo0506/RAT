#!/usr/bin/env python3
"""Repo Analysis Tool (RAT) - entry point.

Usage:
    python run.py [--host 0.0.0.0] [--port 8000]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import uvicorn  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Repo Analysis Tool server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()
    print(f"\n  RAT dashboard -> http://localhost:{args.port}\n")
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
