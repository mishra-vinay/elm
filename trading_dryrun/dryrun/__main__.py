"""python -m dryrun  ->  start the dashboard (loads .env if present)."""
import logging
import os
import re
from pathlib import Path


def _load_env():
    p = Path(".env")
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if v[:1] not in ('"', "'"):
                    v = re.split(r"\s+#", v, maxsplit=1)[0].strip()      # inline comment
                os.environ.setdefault(k.strip(), v.strip('"').strip("'"))


def main():
    _load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import sys

    import uvicorn
    from .app import create_app
    from .config import Settings
    st = Settings()
    try:
        app = create_app(st)
    except RuntimeError as e:          # unsafe or incomplete configuration: say why instead of a traceback
        sys.exit(f"Cannot start: {e}")
    uvicorn.run(app, host=st.host, port=st.port, log_level="info")


if __name__ == "__main__":
    main()
