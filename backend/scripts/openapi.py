"""Print the API's OpenAPI schema (design 07 §8): `frontend/src/api-schema.ts` is generated from it.

Runs without a database or secrets: only the route and model definitions are read.
    uv run python scripts/openapi.py ../frontend/openapi.json

Writes to a file, not stdout: logging set up at import time prints to stdout.
"""

import json
import os
import sys

os.environ.setdefault("MWB_DATABASE_URL", "postgresql://openapi@localhost/openapi")
os.environ.setdefault("MWB_SECRET_KEY", "openapi-export-only")
os.environ.setdefault("MWB_ENV", "dev")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

with open(sys.argv[1], "w", encoding="utf-8") as out:
    json.dump(app.openapi(), out, indent=2, sort_keys=True)
    out.write("\n")
