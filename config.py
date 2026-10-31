"""Centralized configuration for EPUB-to-JSON extraction pipeline.

All paths are loaded from .env file. To change paths, edit .env (not this file).
"""
import os
from pathlib import Path

def _load_env():
    """Load .env file from the same directory as this config."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value

_load_env()

# --- Network paths (from .env) ---
# Fallback to local "input"/"output" if .env not set
DEFAULT_INPUT_DIR = os.environ.get("EPUB_INPUT_DIR", "\\\\192.168.4.28\\Common\\Suman Kumar\\2 file")
DEFAULT_OUTPUT_DIR = os.environ.get("EPUB_OUTPUT_DIR", "\\\\192.168.4.28\\Common\\Suman Kumar\\50_files_json")

# --- ISBN filter Excel sheet (from .env) ---
ISBN_FILTER_EXCEL = os.environ.get("ISBN_FILTER_EXCEL", "")

# --- Column name in Excel for ISBN (from .env) ---
ISBN_COLUMN = os.environ.get("ISBN_COLUMN", "eisbn")