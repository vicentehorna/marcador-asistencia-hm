"""Lanza flet build en el mismo proceso Python (SSL Windows vía pip-system-certs)."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import pip_system_certs.wrapt_requests  # noqa: F401

sys.argv = [
    "flet",
    "build",
    "apk",
    "--project",
    "MarcadorAsistencia",
    "--org",
    "com.tuempresa",
    "--arch",
    "arm64-v8a",
    "--yes",
    "-vv",
]

from flet.cli import main

main()
