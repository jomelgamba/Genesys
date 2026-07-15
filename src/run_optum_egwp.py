"""
Entry point for the OPTUM EGWP line of business.

Loads configs/optum_egwp.env, then runs the shared qa_noncall_email ETL
against Optum EGWP's queue/folder configuration. Schedule this as its own
independent job, separate from the other three line-of-business scripts.
"""
from pathlib import Path

from dotenv import load_dotenv

CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "optum_egwp.env"
if not CONFIG_PATH.is_file():
    raise SystemExit(
        f"Config file not found: {CONFIG_PATH}\n"
        "configs/ must be a sibling of the folder this script lives in "
        "(e.g. Genesys_API/configs/, not Genesys_API/API/configs/)."
    )
load_dotenv(CONFIG_PATH, override=True)

import qa_noncall_email as etl  # noqa: E402

if __name__ == "__main__":
    try:
        raise SystemExit(etl.main())
    except Exception as exc:
        etl.logging.exception("Fatal ETL error: %s", exc)
        raise SystemExit(1)
