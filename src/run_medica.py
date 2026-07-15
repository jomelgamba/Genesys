"""
Entry point for the MEDICA line of business.

Loads configs/medica.env, then runs the shared qa_noncall_email ETL against
MEDICA's queue/folder configuration. Schedule this as its own independent
job, separate from the other three line-of-business scripts.
"""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "configs" / "medica.env", override=True)

import qa_noncall_email as etl  # noqa: E402

if __name__ == "__main__":
    try:
        raise SystemExit(etl.main())
    except Exception as exc:
        etl.logging.exception("Fatal ETL error: %s", exc)
        raise SystemExit(1)
