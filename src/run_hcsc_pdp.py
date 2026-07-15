"""
Entry point for the HCSC PDP Individual line of business.

Loads configs/hcsc_pdp.env, then runs the shared qa_noncall_email ETL
against HCSC PDP Individual's queue/folder configuration (six report types,
each routed to its own queue via QUEUE_ROUTES). Schedule this as its own
independent job, separate from the other three line-of-business scripts.
"""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "configs" / "hcsc_pdp.env", override=True)

import qa_noncall_email as etl  # noqa: E402

if __name__ == "__main__":
    try:
        raise SystemExit(etl.main())
    except Exception as exc:
        etl.logging.exception("Fatal ETL error: %s", exc)
        raise SystemExit(1)
