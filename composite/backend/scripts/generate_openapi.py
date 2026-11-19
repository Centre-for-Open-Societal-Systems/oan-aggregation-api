#!/usr/bin/env python3
"""
Generate the Composite's OpenAPI JSON and write it to composite/docs/openapi/.

Usage (from a venv with the backend installed: pip install -e composite/backend):
    python composite/backend/scripts/generate_openapi.py [output_dir]
Default output_dir: composite/docs/openapi (next to this repo's composite/backend).

The app is built as main.py builds it, with the console on so its staff API is
in the spec. The settings are safe placeholders: nothing connects to a
database, Partner Management, a registry or IAM (no start-up runs).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

COMPOSITE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUT = COMPOSITE_DIR / "docs" / "openapi"
FILENAME = "openapi-composite.json"

PLACEHOLDER_ENV = {
    # The console's routes exist only with the console on and a database configured.
    "AGRI_COMPOSITE_CONSOLE_ENABLED": "true",
    "AGRI_COMPOSITE_DB_DATASOURCE": "postgresql+asyncpg://openapi:openapi@localhost:1/openapi",
    "AGRI_COMPOSITE_USE_CASES_RELOAD_SECONDS": "0",
    "AGRI_COMPOSITE_REGISTRIES": "{}",
    "AGRI_COMPOSITE_SIGNING_P12_PATH": "",
    "AGRI_COMPOSITE_AUDIT_MANAGER_URL": "",
    "AGRI_COMPOSITE_LOGGING_LEVEL": "ERROR",
}

SERVERS = [{
    "url": "https://{host}",
    "description": "The Composite (paths start with /composite/v1)",
    "variables": {"host": {"default": "composite.example.org", "description": "The Composite's host name"}},
}]


def main() -> None:
    out_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUT
    with tempfile.TemporaryDirectory() as tmp:
        # Settings read ./.env: run from an empty directory so a local .env cannot leak in.
        os.chdir(tmp)
        os.environ.update(PLACEHOLDER_ENV)
        os.environ["AGRI_COMPOSITE_USE_CASES_DIR"] = tmp

        from openg2p_agri_composite.main import app

        schema = app.openapi()
    schema["servers"] = SERVERS
    out_path = out_dir / FILENAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(schema, f, indent=2)
        f.write("\n")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
