# -*- coding: utf-8 -*-
"""Start the local Web API for the Android THS switcher."""
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

os.environ.setdefault("THS_EMBED_SCHEDULER", "0")
os.environ.setdefault("THS_NO_SCHEDULER", "1")
os.environ.setdefault("THS_BIND_PORT", "5004")

runpy.run_path("main.py", run_name="__main__")
