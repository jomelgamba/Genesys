from __future__ import annotations

import csv
import glob
import hashlib
import html
import io
import json
import logging
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests
from dotenv import load_dotenv


# =============================================================================
# CONFIGURATION
# =============================================================================

load_dotenv()

RUN_ID = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _env_int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            f"Invalid integer value for {name}={raw!r}. Check your .env file."
        ) from None


def _env_float(name: str, default: str) -> float:
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(
            f"Invalid numeric value for {name}={raw!r}. Check your .env file."
        ) from None


REGION = os.getenv("GENESYS_REGION", "mypurecloud.com").strip()
REGION = REGION.removeprefix("https://").removeprefix("http://").rstrip("/")

CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "").strip()

MIRAMAR_FOLDER = os.getenv("MIRAMAR_FOLDER", ".").strip()
OUTPUT_DIR = os.getenv("OUTPUT_DIR", ".").strip()

# Resolve queue and user by NAME. IDs default to empty so name-lookup is used
# unless an ID is explicitly provided in .env.
TARGET_QUEUE_NAME = os.getenv("TARGET_QUEUE_NAME", "NonCall Evaluation").strip()
TARGET_QUEUE_ID = os.getenv("TARGET_QUEUE_ID", "").strip()

# Optional multi-queue routing for scripts that cover more than one report
# type. Format: "<filename substring>::<queue name>" pairs separated by ";".
# A file's queue is the first rule whose substring matches (case-insensitive)
# the source filename. Example:
#   QUEUE_ROUTES=Approved Enrollment::HCSC_EGWP_NONCALL_Approved_Enrollment;Group Invoice Report::HCSC_EGWP_NONCALL_Group_Invoice
# When unset, every file uses the single TARGET_QUEUE_NAME/TARGET_QUEUE_ID
# above (unchanged, backward-compatible single-queue behavior). When set, a
# file that matches no rule is skipped with a logged error rather than
# silently falling back to TARGET_QUEUE_NAME -- misrouting a real report
# into the wrong QA queue is worse than not processing it.
def _parse_queue_routes(raw: str) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for rule in raw.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        if "::" not in rule:
            raise RuntimeError(
                f"Invalid QUEUE_ROUTES rule (expected 'match::queue name'): {rule!r}"
            )
        match_text, queue_name = rule.split("::", 1)
        match_text, queue_name = match_text.strip(), queue_name.strip()
        if not match_text or not queue_name:
            raise RuntimeError(
                f"Invalid QUEUE_ROUTES rule (empty match or queue name): {rule!r}"
            )
        routes.append((match_text, queue_name))
    return routes


QUEUE_ROUTES = _parse_queue_routes(os.getenv("QUEUE_ROUTES", ""))


def resolve_queue_name_for_file(source_file: str) -> Optional[str]:
    """
    Returns the configured queue NAME for a source file, or None if it
    matches no QUEUE_ROUTES rule (when QUEUE_ROUTES is configured) or if
    neither QUEUE_ROUTES nor a default TARGET_QUEUE_NAME/ID is configured.
    """
    if QUEUE_ROUTES:
        source_cf = source_file.casefold()
        for match_text, queue_name in QUEUE_ROUTES:
            if match_text.casefold() in source_cf:
                return queue_name
        return None
    return TARGET_QUEUE_NAME or None

INTERNAL_USER_EMAIL = os.getenv(
    "INTERNAL_USER_EMAIL", "susernoncall@uspgi.com"
).strip()
INTERNAL_USER_ID = os.getenv("INTERNAL_USER_ID", "").strip()

EMAIL_PROVIDER = os.getenv(
    "EMAIL_PROVIDER", "ConveyNonCall@NonCall.mypurecloud.com"
).strip()

EMAIL_FROM_ADDRESS = os.getenv(
    "EMAIL_FROM_ADDRESS", "noncall.qa.import@uspgi.com"
).strip()
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "NonCall QA Import").strip()
EMAIL_TO_ADDRESS = os.getenv("EMAIL_TO_ADDRESS", EMAIL_PROVIDER).strip()
EMAIL_TO_NAME = os.getenv("EMAIL_TO_NAME", TARGET_QUEUE_NAME).strip()

REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT", "30")
ASSIGNMENT_WAIT_SECONDS = _env_int("ASSIGNMENT_WAIT_SECONDS", "60")
POLL_INTERVAL_SECONDS = _env_float("POLL_INTERVAL_SECONDS", "2")
INTER_RECORD_DELAY_SECONDS = _env_float("INTER_RECORD_DELAY_SECONDS", "1")
PROCESS_LIMIT = _env_int("PROCESS_LIMIT", "0")
PROCESS_LIMIT_PER_FILE = _env_int("PROCESS_LIMIT_PER_FILE", "0")
PRIORITY = _env_int("PRIORITY", "0")

# When true, records are loaded and email payloads are built locally but no
# Genesys API calls are made (no auth, no interaction creation). Useful for
# validating CSV/header parsing against a new report format before running
# for real. Combine with PROCESS_LIMIT_PER_FILE to sample a few rows per file.
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "y"}

REQUIRE_QUEUE_MEMBERSHIP = os.getenv(
    "REQUIRE_QUEUE_MEMBERSHIP", "true"
).strip().lower() in {"1", "true", "yes", "y"}

REQUIRE_ROUTABLE_USER = os.getenv(
    "REQUIRE_ROUTABLE_USER", "false"
).strip().lower() in {"1", "true", "yes", "y"}

CHECK_EMAIL_UTILIZATION = os.getenv(
    "CHECK_EMAIL_UTILIZATION", "true"
).strip().lower() in {"1", "true", "yes", "y"}

AUTO_CONFIGURE_EMAIL_UTILIZATION = os.getenv(
    "AUTO_CONFIGURE_EMAIL_UTILIZATION", "false"
).strip().lower() in {"1", "true", "yes", "y"}

EMAIL_MAXIMUM_CAPACITY = _env_int("EMAIL_MAXIMUM_CAPACITY", "1")

CLOSE_ON_SUCCESS = os.getenv("CLOSE_ON_SUCCESS", "true").strip().lower() in {
    "1", "true", "yes", "y"
}
CLOSE_ON_FAILURE = os.getenv("CLOSE_ON_FAILURE", "true").strip().lower() in {
    "1", "true", "yes", "y"
}

WRAPUP_CODE_ID = os.getenv("WRAPUP_CODE_ID", "").strip()
WRAPUP_CODE_NAME = os.getenv("WRAPUP_CODE_NAME", "Evaluated").strip()

CREATE_EVALUATION = os.getenv("CREATE_EVALUATION", "false").strip().lower() in {
    "1", "true", "yes", "y"
}
EVALUATION_FORM_ID = os.getenv("EVALUATION_FORM_ID", "").strip()
EVALUATOR_USER_EMAIL = os.getenv("EVALUATOR_USER_EMAIL", "").strip()

SKIP_PREVIOUS_SUCCESSES = os.getenv(
    "SKIP_PREVIOUS_SUCCESSES", "true"
).strip().lower() in {"1", "true", "yes", "y"}

RESULT_FILE_PREFIX = os.getenv(
    "RESULT_FILE_PREFIX", "qa_email_insert_results"
).strip()

# Move a source file into the processed subfolder once EVERY row in it has
# succeeded. The moved file gets a timestamp suffix so nothing is overwritten.
# Files with any failure are left in place so they can be re-run (the
# idempotency ledger skips rows that already succeeded).
MOVE_PROCESSED_FILES = os.getenv(
    "MOVE_PROCESSED_FILES", "true"
).strip().lower() in {"1", "true", "yes", "y"}

PROCESSED_SUBFOLDER = os.getenv("PROCESSED_SUBFOLDER", "processed").strip()

# Header detection: a candidate header line must contain one of these tokens
# as a full cell (exact or prefix), so report title rows above the real header
# are skipped. Comma-separated, case-insensitive.
HEADER_MARKERS = [
    marker.strip()
    for marker in os.getenv("HEADER_MARKERS", "Member_ID,Member_id").split(",")
    if marker.strip()
]

API_BASE_URL = f"https://api.{REGION}"
LOGIN_BASE_URL = f"https://login.{REGION}"

LOG_FILE = Path(OUTPUT_DIR) / f"qa_email_etl_{RUN_ID}.log"

token_info: dict[str, Optional[str]] = {"access_token": None}


def configure_logging() -> None:
    """Sets up the output directory and log handlers.

    Kept out of module import so importing this file (e.g. from tests or
    other tools) never touches the filesystem or mutates global logging
    config as a side effect.
    """
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


# =============================================================================
# API WRAPPER
# =============================================================================

def authenticate(force: bool = False) -> None:
    if token_info["access_token"] and not force:
        return
    logging.info("Authenticating to Genesys Cloud region=%s", REGION)
    response = requests.post(
        f"{LOGIN_BASE_URL}/oauth/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(
            "OAuth authentication failed. "
            f"status={response.status_code} body={response.text[:2000]}"
        )
    token_info["access_token"] = response.json()["access_token"]
    logging.info("OAuth token obtained.")


def api_headers() -> dict[str, str]:
    token = token_info["access_token"]
    if not token:
        raise RuntimeError("No access token is available.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def sleep_backoff(attempt: int, response: requests.Response) -> None:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            delay = max(float(retry_after), 0.5)
        except ValueError:
            delay = 0.5 * (2 ** (attempt - 1))
    else:
        delay = 0.5 * (2 ** (attempt - 1))
    time.sleep(min(delay, 30))


def api_request(
    method: str,
    path: str,
    *,
    expected_statuses: tuple[int, ...] = (200,),
    **kwargs: Any,
) -> requests.Response:
    url = path if path.startswith("http") else f"{API_BASE_URL}{path}"
    last_response: Optional[requests.Response] = None
    last_exception: Optional[Exception] = None
    for attempt in range(1, 6):
        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                headers=api_headers(),
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
        except requests.exceptions.RequestException as exc:
            last_exception = exc
            logging.warning(
                "Network error calling Genesys API. method=%s path=%s attempt=%s error=%s",
                method.upper(), path, attempt, exc,
            )
            if attempt < 5:
                time.sleep(min(0.5 * (2 ** (attempt - 1)), 30))
                continue
            break
        last_response = response
        last_exception = None
        if response.status_code == 401 and attempt < 5:
            logging.warning("401 received; refreshing OAuth token.")
            authenticate(force=True)
            continue
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 5:
            logging.warning(
                "Retryable API response. method=%s path=%s status=%s attempt=%s body=%s",
                method.upper(), path, response.status_code, attempt,
                response.text[:500],
            )
            sleep_backoff(attempt, response)
            continue
        if response.status_code not in expected_statuses:
            logging.error(
                "Genesys API error. method=%s path=%s status=%s body=%s",
                method.upper(), path, response.status_code, response.text[:2000],
            )
        return response
    if last_response is not None:
        return last_response
    raise RuntimeError(
        f"Network error calling {method.upper()} {path} after 5 attempts: {last_exception}"
    ) from last_exception


def response_json(response: requests.Response) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {"raw": response.text}
    return payload if isinstance(payload, dict) else {"data": payload}


# =============================================================================
# LOOKUPS AND PREFLIGHT
# =============================================================================

def lookup_user_by_email(email_address: str) -> tuple[Optional[str], Optional[str]]:
    for query_type in ("EXACT", "TERM"):
        response = api_request(
            "POST", "/api/v2/users/search",
            expected_statuses=(200,),
            json={
                "pageSize": 25, "pageNumber": 1,
                "query": [
                    {"type": query_type, "fields": ["email"], "value": email_address}
                ],
                "sortOrder": "ASC",
            },
        )
        if response.status_code == 200:
            for user in response_json(response).get("results", []):
                if str(user.get("email", "")).casefold() == email_address.casefold():
                    return user.get("id"), user.get("name")
    return None, None


def lookup_queue_by_name(queue_name: str) -> tuple[Optional[str], Optional[str]]:
    response = api_request(
        "GET", "/api/v2/routing/queues",
        expected_statuses=(200,),
        params={"name": queue_name, "pageSize": 100, "pageNumber": 1},
    )
    if response.status_code != 200:
        return None, None
    for queue in response_json(response).get("entities", []):
        if str(queue.get("name", "")).casefold() == queue_name.casefold():
            return queue.get("id"), queue.get("name")
    return None, None


def resolve_target_user() -> tuple[str, str]:
    if INTERNAL_USER_ID:
        response = api_request(
            "GET", f"/api/v2/users/{INTERNAL_USER_ID}", expected_statuses=(200,)
        )
        if response.status_code == 200:
            user = response_json(response)
            actual_email = str(user.get("email", ""))
            if (
                INTERNAL_USER_EMAIL
                and actual_email.casefold() != INTERNAL_USER_EMAIL.casefold()
            ):
                raise RuntimeError(
                    "INTERNAL_USER_ID resolves to a different email. "
                    f"expected={INTERNAL_USER_EMAIL} actual={actual_email}"
                )
            return INTERNAL_USER_ID, str(user.get("name") or INTERNAL_USER_EMAIL)
        logging.warning(
            "Configured INTERNAL_USER_ID could not be read. Falling back to email."
        )
    user_id, user_name = lookup_user_by_email(INTERNAL_USER_EMAIL)
    if not user_id:
        raise RuntimeError(f"Internal user not found: {INTERNAL_USER_EMAIL}")
    return user_id, user_name or INTERNAL_USER_EMAIL


def resolve_target_queue() -> tuple[str, str]:
    if TARGET_QUEUE_ID:
        response = api_request(
            "GET", f"/api/v2/routing/queues/{TARGET_QUEUE_ID}",
            expected_statuses=(200,),
        )
        if response.status_code == 200:
            queue = response_json(response)
            actual_name = str(queue.get("name", ""))
            if (
                TARGET_QUEUE_NAME
                and actual_name.casefold() != TARGET_QUEUE_NAME.casefold()
            ):
                raise RuntimeError(
                    "TARGET_QUEUE_ID resolves to a different queue. "
                    f"expected={TARGET_QUEUE_NAME} actual={actual_name}"
                )
            return TARGET_QUEUE_ID, actual_name or TARGET_QUEUE_NAME
        logging.warning(
            "Configured TARGET_QUEUE_ID could not be read. Falling back to name."
        )
    queue_id, queue_name = lookup_queue_by_name(TARGET_QUEUE_NAME)
    if not queue_id:
        raise RuntimeError(f"Target queue not found: {TARGET_QUEUE_NAME}")
    return queue_id, queue_name or TARGET_QUEUE_NAME


def resolve_queue_id_cached(queue_name: str, cache: dict[str, str]) -> str:
    """
    Resolves a queue name to an ID, caching the result so a multi-queue run
    (QUEUE_ROUTES) only looks up each distinct queue once. The single
    TARGET_QUEUE_NAME/TARGET_QUEUE_ID pair still goes through
    resolve_target_queue() so the ID-matches-name safety check keeps working.
    """
    if queue_name in cache:
        return cache[queue_name]
    if queue_name == TARGET_QUEUE_NAME and TARGET_QUEUE_ID:
        queue_id, resolved_name = resolve_target_queue()
    else:
        queue_id, resolved_name = lookup_queue_by_name(queue_name)
        if not queue_id:
            raise RuntimeError(f"Queue not found: {queue_name}")
    cache[queue_name] = queue_id
    logging.info("Queue resolved. name=%s id=%s", resolved_name or queue_name, queue_id)
    return queue_id


def user_is_queue_member(queue_id: str, user_id: str) -> bool:
    page_number = 1
    while True:
        response = api_request(
            "GET", f"/api/v2/routing/queues/{queue_id}/members",
            expected_statuses=(200,),
            params={"pageSize": 100, "pageNumber": page_number},
        )
        if response.status_code != 200:
            return False
        payload = response_json(response)
        for member in payload.get("entities", []):
            member_id = member.get("id") or member.get("user", {}).get("id")
            if member_id == user_id:
                logging.info(
                    "Queue membership confirmed. queue_id=%s user_id=%s joined=%s",
                    queue_id, user_id, member.get("joined"),
                )
                return True
        page_count = int(payload.get("pageCount") or 1)
        if page_number >= page_count:
            return False
        page_number += 1


def get_user_presence(user_id: str) -> Optional[dict[str, Any]]:
    response = api_request(
        "GET", f"/api/v2/users/{user_id}/presences/purecloud",
        expected_statuses=(200,),
    )
    return response_json(response) if response.status_code == 200 else None


def get_user_routing_status(user_id: str) -> Optional[dict[str, Any]]:
    response = api_request(
        "GET", f"/api/v2/users/{user_id}/routingstatus", expected_statuses=(200,)
    )
    return response_json(response) if response.status_code == 200 else None


def preflight_routing_state(user_id: str) -> bool:
    presence = get_user_presence(user_id) or {}
    routing = get_user_routing_status(user_id) or {}
    system_presence = presence.get("presenceDefinition", {}).get("systemPresence")
    routing_status = routing.get("status")
    logging.info(
        "Target user routing preflight. systemPresence=%s routingStatus=%s",
        system_presence, routing_status,
    )
    routable = routing_status in {"IDLE", "INTERACTING", "COMMUNICATING"}
    if REQUIRE_ROUTABLE_USER and not routable:
        logging.error("Target user not routable. routingStatus=%s", routing_status)
        return False
    return True


def check_or_configure_email_utilization(user_id: str) -> bool:
    if not CHECK_EMAIL_UTILIZATION:
        return True
    response = api_request(
        "GET", f"/api/v2/routing/users/{user_id}/utilization",
        expected_statuses=(200,),
    )
    if response.status_code != 200:
        if REQUIRE_ROUTABLE_USER:
            return False
        logging.warning(
            "Unable to read user utilization; continuing (REQUIRE_ROUTABLE_USER=false)."
        )
        return True
    payload = response_json(response)
    utilization = payload.get("utilization", {})
    email_settings = utilization.get("email", {})
    maximum_capacity = int(email_settings.get("maximumCapacity") or 0)
    logging.info(
        "Email utilization. maximumCapacity=%s includeNonAcd=%s",
        maximum_capacity, email_settings.get("includeNonAcd"),
    )
    if maximum_capacity > 0:
        return True
    if not AUTO_CONFIGURE_EMAIL_UTILIZATION:
        logging.warning("Email maximumCapacity is zero and auto-config disabled.")
        return not REQUIRE_ROUTABLE_USER
    updated_utilization = dict(utilization)
    updated_utilization["email"] = {
        **email_settings,
        "maximumCapacity": EMAIL_MAXIMUM_CAPACITY,
        "includeNonAcd": email_settings.get("includeNonAcd", False),
        "interruptableMediaTypes": email_settings.get("interruptableMediaTypes", []),
    }
    update_response = api_request(
        "PUT", f"/api/v2/routing/users/{user_id}/utilization",
        expected_statuses=(200, 202),
        json={"utilization": updated_utilization},
    )
    if update_response.status_code not in {200, 202}:
        return False
    logging.info("Email utilization set to maximumCapacity=%s.", EMAIL_MAXIMUM_CAPACITY)
    time.sleep(2)
    return True


def lookup_wrapup_code_by_name(wrapup_name: str) -> tuple[Optional[str], Optional[str]]:
    if not wrapup_name:
        return None, None
    response = api_request(
        "GET", "/api/v2/routing/wrapupcodes",
        expected_statuses=(200,),
        params={"pageSize": 100, "name": wrapup_name},
    )
    if response.status_code != 200:
        return None, None
    for code in response_json(response).get("entities", []):
        if str(code.get("name", "")).casefold() == wrapup_name.casefold():
            return code.get("id"), code.get("name")
    return None, None


# =============================================================================
# CSV EXTRACTION (mapping-free: capture every column, no concept resolution)
# =============================================================================

def normalize_scalar(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _normalize_header_text(text: str) -> str:
    """Collapses whitespace to '_' so 'Member ID' and 'Member_ID' compare equal."""
    return "_".join(text.split()).casefold()


def _detect_encoding(file_path: str) -> str:
    """
    Most exports are UTF-8 (with BOM). Some Windows-originated exports use
    CP-1252 (smart quotes, en/em dashes) and fail to decode as UTF-8.
    """
    with open(file_path, "rb") as handle:
        raw_bytes = handle.read()
    try:
        raw_bytes.decode("utf-8-sig")
        return "utf-8-sig"
    except UnicodeDecodeError:
        logging.warning(
            "%s is not valid UTF-8; falling back to cp1252 encoding.",
            os.path.basename(file_path),
        )
        return "cp1252"


def _detect_header_and_delimiter(file_path: str, encoding: str) -> tuple[int, str]:
    """
    Returns (zero-based header line index, delimiter).

    Report exports can have title rows that *mention* a marker (e.g.
    'textbox7,Member_ID_1') above the real header. To avoid matching those,
    a candidate header line must:
      - contain a marker as a full cell (exact or prefix match, matched
        after normalizing spaces/underscores), AND
      - have at least MIN_HEADER_COLUMNS columns.
    The line with the most columns among candidates wins, so the real
    multi-column header beats a short title row.
    """
    markers_norm = [_normalize_header_text(m) for m in HEADER_MARKERS]
    MIN_HEADER_COLUMNS = _env_int("MIN_HEADER_COLUMNS", "3")
    MAX_SCAN_LINES = _env_int("MAX_HEADER_SCAN_LINES", "30")

    def cell_matches_marker(cell: str) -> bool:
        cell_norm = _normalize_header_text(cell.strip().strip('"'))
        if not cell_norm:
            return False
        for marker in markers_norm:
            if cell_norm == marker or cell_norm.startswith(marker):
                return True
        return False

    best: Optional[tuple[int, str, int]] = None  # (line_index, delimiter, col_count)

    try:
        with open(file_path, "r", encoding=encoding, newline="") as handle:
            for index, raw_line in enumerate(handle):
                if index >= MAX_SCAN_LINES:
                    break
                if not raw_line.strip():
                    continue

                comma_cells = raw_line.rstrip("\n").split(",")
                tab_cells = raw_line.rstrip("\n").split("\t")

                if len(tab_cells) > len(comma_cells):
                    cells, delimiter = tab_cells, "\t"
                else:
                    cells, delimiter = comma_cells, ","

                has_marker = any(cell_matches_marker(c) for c in cells)
                col_count = len([c for c in cells if c.strip() != ""])

                if has_marker and col_count >= MIN_HEADER_COLUMNS:
                    if best is None or col_count > best[2]:
                        best = (index, delimiter, col_count)
    except Exception as exc:
        logging.warning("Header detection failed for %s: %s", file_path, exc)

    if best is not None:
        logging.info(
            "Header at line %s, delimiter=%r, columns=%s in %s",
            best[0] + 1, best[1], best[2], os.path.basename(file_path),
        )
        return best[0], best[1]

    logging.warning(
        "No suitable header found in %s; assuming line 1, comma.",
        os.path.basename(file_path),
    )
    return 0, ","


def record_key(source_file: str, row_number: int, row_signature: str) -> str:
    digest = hashlib.sha256(row_signature.encode("utf-8")).hexdigest()[:16]
    normalized = f"{source_file.casefold()}|{row_number}|{digest}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, normalized))


def load_csv_file(file_path: str) -> list[dict[str, Any]]:
    logging.info("Reading CSV file: %s", file_path)

    encoding = _detect_encoding(file_path)
    header_row_index, delimiter = _detect_header_and_delimiter(file_path, encoding)

    dataframe = pd.read_csv(
        file_path,
        dtype=str,
        keep_default_na=False,
        encoding=encoding,
        header=header_row_index,
        sep=delimiter,
        skip_blank_lines=False,
        engine="python",
        on_bad_lines="skip",
    )
    dataframe.columns = [str(column).strip() for column in dataframe.columns]
    dataframe = dataframe.loc[:, [
        c for c in dataframe.columns if c and not str(c).startswith("Unnamed")
    ]]

    # A real report header rarely collapses to 0-1 usable columns; when it
    # does, header detection almost certainly landed on a title row instead
    # of the real header (misdetection silently drops every other column's
    # data rather than raising). Fail loud instead of loading garbage.
    if len(dataframe.columns) <= 1:
        raise RuntimeError(
            f"Header detection likely failed for {os.path.basename(file_path)}: "
            f"only {len(dataframe.columns)} usable column(s) found at line "
            f"{header_row_index + 1}. Check HEADER_MARKERS / MIN_HEADER_COLUMNS."
        )

    records: list[dict[str, Any]] = []
    source_file = os.path.basename(file_path)

    for dataframe_index, row in dataframe.iterrows():
        csv_row_number = header_row_index + int(dataframe_index) + 2

        all_fields = {
            column: normalize_scalar(row.get(column))
            for column in dataframe.columns
        }

        # Skip fully-empty rows (export artifacts); keep any row with content.
        if not any(all_fields.values()):
            continue

        row_signature = "|".join(f"{k}={v}" for k, v in all_fields.items())
        key = record_key(source_file, csv_row_number, row_signature)

        records.append({
            "record_key": key,
            "source_file": source_file,
            "source_path": file_path,
            "row_number": csv_row_number,
            "all_fields": all_fields,
        })

    logging.info(
        "Loaded %s rows from %s (header line %s, delimiter %r).",
        len(records), source_file, header_row_index + 1, delimiter,
    )
    return records


def find_csv_files(folder: str) -> list[str]:
    return sorted(glob.glob(os.path.join(folder, "*.csv")))


def load_all_records(csv_files: list[str], limit_per_file: int = 0) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for file_path in csv_files:
        try:
            file_records = load_csv_file(file_path)
            if limit_per_file > 0:
                file_records = file_records[:limit_per_file]
            records.extend(file_records)
        except Exception as exc:
            logging.exception("Failed to read file (skipping): %s error=%s", file_path, exc)
    return records


def move_processed_file(file_path: str) -> Optional[str]:
    """
    Moves a fully-processed source file into a date-partitioned processed
    subfolder (processed/YYYY/MM/DD/) based on the processing date (now),
    adding a timestamp suffix so existing files are never overwritten.
    Returns the new path, or None if the move was skipped or failed.
    """
    try:
        source = Path(file_path)
        now = datetime.now()
        processed_dir = (
            source.parent
            / PROCESSED_SUBFOLDER
            / now.strftime("%Y")
            / now.strftime("%m")
            / now.strftime("%d")
        )
        processed_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%d_%H%M%S")
        destination = processed_dir / f"{source.stem}_{stamp}{source.suffix}"
        # Guarantee uniqueness if two runs collide within the same second.
        counter = 1
        while destination.exists():
            destination = processed_dir / (
                f"{source.stem}_{stamp}_{counter}{source.suffix}"
            )
            counter += 1
        shutil.move(str(source), str(destination))
        logging.info("Moved processed file: %s -> %s", source.name, destination)
        return str(destination)
    except Exception as exc:
        logging.warning("Could not move processed file %s: %s", file_path, exc)
        return None


# =============================================================================
# EMAIL BODY / TRANSCRIPT (full row, no mapping)
# =============================================================================

def csv_line(values: list[str]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="")
    writer.writerow(values)
    return stream.getvalue()


def build_email_subject(record: dict[str, Any]) -> str:
    subject = f"NonCall QA | {record['source_file']} | Row {record['row_number']}"
    return subject[:390]


def build_text_body(record: dict[str, Any]) -> str:
    fields = record["all_fields"]
    headers = list(fields.keys())
    values = [fields[column] for column in headers]

    detail_lines = [
        "NONCALL QA CSV RECORD",
        "=====================",
        f"Run ID: {RUN_ID}",
        f"Record Key: {record['record_key']}",
        f"Source File: {record['source_file']}",
        f"Source Row: {record['row_number']}",
        "",
        "CSV HEADER",
        "----------",
        csv_line(headers),
        "",
        "CSV ROW",
        "-------",
        csv_line(values),
        "",
        "FIELD-BY-FIELD VIEW",
        "-------------------",
    ]
    for column in headers:
        detail_lines.append(f"{column}: {fields[column]}")
    return "\n".join(detail_lines)


def build_html_body(record: dict[str, Any]) -> str:
    fields = record["all_fields"]
    table_rows = "\n".join(
        "<tr>"
        f"<th style='text-align:left;vertical-align:top;padding:6px;"
        f"border:1px solid #ccc'>{html.escape(column)}</th>"
        f"<td style='padding:6px;border:1px solid #ccc'>{html.escape(value)}</td>"
        "</tr>"
        for column, value in fields.items()
    )
    header_line = html.escape(csv_line(list(fields.keys())))
    value_line = html.escape(csv_line(list(fields.values())))

    return f"""<!DOCTYPE html>
<html>
<body>
  <h2>NonCall QA CSV Record</h2>
  <p>
    <strong>Run ID:</strong> {html.escape(RUN_ID)}<br>
    <strong>Record Key:</strong> {html.escape(record['record_key'])}<br>
    <strong>Source File:</strong> {html.escape(record['source_file'])}<br>
    <strong>Source Row:</strong> {record['row_number']}
  </p>
  <h3>CSV Header</h3>
  <pre>{header_line}</pre>
  <h3>CSV Row</h3>
  <pre>{value_line}</pre>
  <h3>Field-by-field view</h3>
  <table style="border-collapse:collapse">
    {table_rows}
  </table>
</body>
</html>"""


def build_customer_attributes(record: dict[str, Any]) -> dict[str, str]:
    attributes = {
        "qa.runId": RUN_ID,
        "qa.recordKey": record["record_key"],
        "qa.sourceFile": record.get("source_file", "")[:250],
        "qa.sourceRow": str(record.get("row_number", "")),
    }
    for column, value in record["all_fields"].items():
        safe_key = "csv." + "".join(
            ch if (ch.isalnum() or ch in "._-") else "_"
            for ch in column.strip()
        )
        attributes[safe_key[:100]] = str(value)[:250]
    return attributes


# =============================================================================
# EMAIL CONVERSATION CREATION AND VERIFICATION
# =============================================================================

def build_email_payload(
    record: dict[str, Any], queue_id: str, internal_user_id: str
) -> dict[str, Any]:
    return {
        "queueId": queue_id,
        "userId": internal_user_id,
        "emailAddress": EMAIL_FROM_ADDRESS,
        "provider": EMAIL_PROVIDER,
        "direction": "INBOUND",
        "priority": PRIORITY,
        "fromAddress": EMAIL_FROM_ADDRESS,
        "fromName": EMAIL_FROM_NAME,
        "toAddress": EMAIL_TO_ADDRESS,
        "toName": EMAIL_TO_NAME,
        "subject": build_email_subject(record),
        "textBody": build_text_body(record),
        "htmlBody": build_html_body(record),
        "attributes": build_customer_attributes(record),
    }


def create_email_interaction(
    record: dict[str, Any], queue_id: str, internal_user_id: str
) -> tuple[Optional[str], dict[str, Any], int]:
    payload = build_email_payload(record, queue_id, internal_user_id)
    logging.info(
        "Creating email interaction. source_file=%s row=%s subject=%s",
        record["source_file"], record["row_number"], payload["subject"],
    )
    response = api_request(
        "POST", "/api/v2/conversations/emails",
        expected_statuses=(200, 201, 202), json=payload,
    )
    response_payload = response_json(response)
    if response.status_code not in {200, 201, 202}:
        return None, response_payload, response.status_code
    conversation_id = (
        response_payload.get("id")
        or response_payload.get("conversationId")
        or response_payload.get("conversation", {}).get("id")
    )
    if not conversation_id:
        logging.error(
            "Create response had no conversation ID. body=%s",
            json.dumps(response_payload, default=str)[:2000],
        )
        return None, response_payload, response.status_code
    logging.info("Email interaction created. conversation_id=%s", conversation_id)
    return conversation_id, response_payload, response.status_code


def get_email_conversation(conversation_id: str) -> Optional[dict[str, Any]]:
    response = api_request(
        "GET", f"/api/v2/conversations/emails/{conversation_id}",
        expected_statuses=(200,),
    )
    if response.status_code == 200:
        return response_json(response)
    response = api_request(
        "GET", f"/api/v2/conversations/{conversation_id}", expected_statuses=(200,)
    )
    return response_json(response) if response.status_code == 200 else None


def participant_user_id(participant: dict[str, Any]) -> Optional[str]:
    return participant.get("userId") or participant.get("user", {}).get("id")


def participant_queue_id(participant: dict[str, Any]) -> Optional[str]:
    return participant.get("queueId") or participant.get("queue", {}).get("id")


def participant_summary(participant: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": participant.get("id"),
        "name": participant.get("name"),
        "purpose": participant.get("purpose"),
        "state": participant.get("state"),
        "userId": participant_user_id(participant),
        "queueId": participant_queue_id(participant),
    }


def find_agent_participant(
    conversation: dict[str, Any], user_id: str
) -> Optional[dict[str, Any]]:
    for participant in conversation.get("participants", []):
        if (
            str(participant.get("purpose", "")).casefold() == "agent"
            and participant_user_id(participant) == user_id
        ):
            return participant
    return None


def wait_for_agent_assignment(
    conversation_id: str, internal_user_id: str, queue_id: str
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    deadline = time.monotonic() + ASSIGNMENT_WAIT_SECONDS
    poll_number = 0
    last_conversation: Optional[dict[str, Any]] = None
    while time.monotonic() < deadline:
        poll_number += 1
        conversation = get_email_conversation(conversation_id)
        if conversation:
            last_conversation = conversation
            agent = find_agent_participant(conversation, internal_user_id)
            if agent:
                state = str(agent.get("state", "")).casefold()
                logging.info(
                    "Target agent found. conversation_id=%s participant_id=%s "
                    "state=%s poll=%s",
                    conversation_id, agent.get("id"), state, poll_number,
                )
                if state in {"connected", "interacting", "disconnected", "terminated"}:
                    return agent, conversation
            logging.info(
                "Waiting for agent assignment. conversation_id=%s poll=%s parts=%s",
                conversation_id, poll_number,
                [participant_summary(p) for p in conversation.get("participants", [])],
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    logging.error(
        "Agent not confirmed within %ss. conversation_id=%s parts=%s",
        ASSIGNMENT_WAIT_SECONDS, conversation_id,
        [participant_summary(p) for p in (last_conversation or {}).get("participants", [])],
    )
    return None, last_conversation


# =============================================================================
# CLOSE / WRAP-UP
# =============================================================================

def patch_email_participant(
    conversation_id: str, participant_id: str, payload: dict[str, Any]
) -> requests.Response:
    response = api_request(
        "PATCH",
        f"/api/v2/conversations/emails/{conversation_id}/participants/{participant_id}",
        expected_statuses=(200, 202, 204), json=payload,
    )
    if response.status_code in {200, 202, 204}:
        return response
    logging.warning("Email participant PATCH failed; trying generic endpoint.")
    return api_request(
        "PATCH",
        f"/api/v2/conversations/{conversation_id}/participants/{participant_id}",
        expected_statuses=(200, 202, 204), json=payload,
    )


def close_email_conversation(
    conversation_id: str,
    agent_participant_id: Optional[str],
    wrapup_code_id: Optional[str],
    wrapup_code_name: Optional[str],
) -> bool:
    conversation = get_email_conversation(conversation_id)
    if not conversation:
        logging.error("Cannot read conversation to close. id=%s", conversation_id)
        return False
    overall_success = True
    if agent_participant_id:
        agent_payload: dict[str, Any] = {"state": "disconnected"}
        if wrapup_code_id:
            agent_payload["wrapup"] = {
                "code": wrapup_code_id,
                "name": wrapup_code_name or WRAPUP_CODE_NAME,
                "provisional": False,
            }
        response = patch_email_participant(
            conversation_id, agent_participant_id, agent_payload
        )
        if response.status_code not in {200, 202, 204}:
            overall_success = False
            logging.error(
                "Failed to disconnect agent. id=%s participant=%s",
                conversation_id, agent_participant_id,
            )
        time.sleep(1)
    refreshed = get_email_conversation(conversation_id) or conversation
    for participant in refreshed.get("participants", []):
        participant_id = participant.get("id")
        state = str(participant.get("state", "")).casefold()
        if not participant_id or participant_id == agent_participant_id:
            continue
        if state in {"disconnected", "terminated"}:
            continue
        response = patch_email_participant(
            conversation_id, participant_id, {"state": "disconnected"}
        )
        if response.status_code not in {200, 202, 204}:
            overall_success = False
    return overall_success


# =============================================================================
# OPTIONAL QUALITY EVALUATION
# =============================================================================

def resolve_published_form_id(form_id: str) -> Optional[str]:
    if not form_id:
        return None
    response = api_request(
        "GET", f"/api/v2/quality/forms/evaluations/{form_id}", expected_statuses=(200,)
    )
    context_id = form_id
    if response.status_code == 200:
        form = response_json(response)
        if form.get("published"):
            return form.get("id")
        context_id = form.get("contextId") or form_id
    response = api_request(
        "GET", "/api/v2/quality/forms/evaluations",
        expected_statuses=(200,),
        params={"contextId": context_id, "published": "true", "pageSize": 25},
    )
    if response.status_code != 200:
        return None
    entities = response_json(response).get("entities", [])
    return entities[0].get("id") if entities else None


def create_pending_evaluation(
    conversation_id: str,
    agent_user_id: str,
    published_form_id: str,
    evaluator_user_id: str,
) -> Optional[str]:
    response = api_request(
        "POST", f"/api/v2/quality/conversations/{conversation_id}/evaluations",
        expected_statuses=(200, 201, 202),
        json={
            "agent": {"id": agent_user_id},
            "evaluator": {"id": evaluator_user_id},
            "evaluationForm": {"id": published_form_id},
            "status": "PENDING",
        },
    )
    if response.status_code not in {200, 201, 202}:
        return None
    return response_json(response).get("id")


# =============================================================================
# RESULT LEDGER / IDEMPOTENCY
# =============================================================================

RESULT_FIELDS = [
    "run_id", "record_key", "source_file", "source_row",
    "conversation_id", "agent_participant_id", "evaluation_id", "provider",
    "queue_id", "agent_user_id", "subject", "status", "error", "created_at_utc",
]


def load_previous_success_keys() -> set[str]:
    if not SKIP_PREVIOUS_SUCCESSES:
        return set()
    success_keys: set[str] = set()
    pattern = os.path.join(OUTPUT_DIR, f"{RESULT_FILE_PREFIX}_*.csv")
    for result_path in glob.glob(pattern):
        try:
            with open(result_path, "r", newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    if str(row.get("status", "")).startswith("success"):
                        key = str(row.get("record_key", "")).strip()
                        if key:
                            success_keys.add(key)
        except Exception as exc:
            logging.warning("Could not read prior result file %s: %s", result_path, exc)
    logging.info("Loaded %s previously successful record keys.", len(success_keys))
    return success_keys


def append_result(
    results: list[dict[str, Any]],
    record: dict[str, Any],
    *,
    conversation_id: str = "",
    agent_participant_id: str = "",
    evaluation_id: str = "",
    queue_id: str = "",
    agent_user_id: str = "",
    subject: str = "",
    status: str,
    error: str = "",
) -> None:
    results.append({
        "run_id": RUN_ID,
        "record_key": record.get("record_key", ""),
        "source_file": record.get("source_file", ""),
        "source_row": record.get("row_number", ""),
        "conversation_id": conversation_id,
        "agent_participant_id": agent_participant_id,
        "evaluation_id": evaluation_id,
        "provider": EMAIL_PROVIDER,
        "queue_id": queue_id,
        "agent_user_id": agent_user_id,
        "subject": subject,
        "status": status,
        "error": error[:2000],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    })


def write_dry_run_preview(records: list[dict[str, Any]]) -> str:
    """
    Builds email payloads locally (no Genesys API calls) so CSV/header
    parsing -- and QUEUE_ROUTES routing -- can be validated before running
    live. Records whose file matches no queue route are still listed, with
    target_queue_name="UNROUTED", so a routing config can be checked here
    before it ever touches the live queue/API.
    """
    preview_rows = []
    for record in records:
        queue_name = resolve_queue_name_for_file(record["source_file"]) or "UNROUTED"
        payload = build_email_payload(record, queue_id="DRY_RUN", internal_user_id="DRY_RUN")
        logging.info(
            "[DRY_RUN] file=%s row=%s queue=%s subject=%s field_count=%s",
            record["source_file"], record["row_number"], queue_name, payload["subject"],
            len(record["all_fields"]),
        )
        preview_rows.append({
            "source_file": record["source_file"],
            "source_row": record["row_number"],
            "record_key": record["record_key"],
            "target_queue_name": queue_name,
            "subject": payload["subject"],
            "field_count": len(record["all_fields"]),
        })
    preview_path = Path(OUTPUT_DIR) / f"qa_email_dry_run_preview_{RUN_ID}.csv"
    with preview_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "source_file", "source_row", "record_key",
                "target_queue_name", "subject", "field_count",
            ],
        )
        writer.writeheader()
        writer.writerows(preview_rows)
    unrouted_count = sum(1 for row in preview_rows if row["target_queue_name"] == "UNROUTED")
    logging.info(
        "DRY_RUN complete. records_previewed=%s unrouted=%s preview_file=%s",
        len(preview_rows), unrouted_count, preview_path,
    )
    return str(preview_path)


def write_results(results: list[dict[str, Any]]) -> str:
    output_path = Path(OUTPUT_DIR) / f"{RESULT_FILE_PREFIX}_{RUN_ID}.csv"
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    return str(output_path)


# =============================================================================
# MAIN
# =============================================================================

def validate_config() -> None:
    required = {
        "MIRAMAR_FOLDER": MIRAMAR_FOLDER,
        "EMAIL_PROVIDER": EMAIL_PROVIDER,
        "EMAIL_FROM_ADDRESS": EMAIL_FROM_ADDRESS,
    }
    if not DRY_RUN:
        required["CLIENT_ID"] = CLIENT_ID
        required["CLIENT_SECRET"] = CLIENT_SECRET
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required configuration values: {missing}")
    if DRY_RUN:
        return
    if not QUEUE_ROUTES and not TARGET_QUEUE_ID and not TARGET_QUEUE_NAME:
        raise RuntimeError(
            "Must configure either TARGET_QUEUE_ID or TARGET_QUEUE_NAME, or QUEUE_ROUTES."
        )
    if not INTERNAL_USER_ID and not INTERNAL_USER_EMAIL:
        raise RuntimeError(
            "Must configure either INTERNAL_USER_ID or INTERNAL_USER_EMAIL."
        )


def main() -> int:
    configure_logging()
    validate_config()

    csv_files = find_csv_files(MIRAMAR_FOLDER)
    if not csv_files:
        raise RuntimeError(f"No CSV files found in MIRAMAR_FOLDER={MIRAMAR_FOLDER}")

    logging.info("=" * 72)
    logging.info("Genesys NonCall QA Email Interaction ETL (mapping-free)")
    logging.info("Run ID: %s", RUN_ID)
    logging.info("Source folder: %s", MIRAMAR_FOLDER)
    logging.info("Output folder: %s", OUTPUT_DIR)
    logging.info("Email provider: %s", EMAIL_PROVIDER)
    logging.info("CSV files found: %s", len(csv_files))
    logging.info("Header markers: %s", HEADER_MARKERS)
    if QUEUE_ROUTES:
        logging.info("Queue routes: %s", QUEUE_ROUTES)
    else:
        logging.info("Queue routes: none configured (single queue: %s)", TARGET_QUEUE_NAME)
    logging.info("=" * 72)

    records = load_all_records(csv_files, PROCESS_LIMIT_PER_FILE)
    if PROCESS_LIMIT_PER_FILE > 0:
        logging.info("PROCESS_LIMIT_PER_FILE applied. records_after_sampling=%s", len(records))

    if not records:
        raise RuntimeError("No CSV records were found.")

    # Full row count per file (before any filtering) -- a file may only be
    # moved to processed when ALL of its rows are accounted for as successful.
    file_total: dict[str, int] = {}
    for record in records:
        file_total[record["source_path"]] = (
            file_total.get(record["source_path"], 0) + 1
        )

    previous_success_keys = load_previous_success_keys()
    # Count rows already successful in prior runs, per file.
    file_already_success: dict[str, int] = {}
    if previous_success_keys:
        for record in records:
            if record["record_key"] in previous_success_keys:
                file_already_success[record["source_path"]] = (
                    file_already_success.get(record["source_path"], 0) + 1
                )
        original_count = len(records)
        records = [
            record for record in records
            if record["record_key"] not in previous_success_keys
        ]
        logging.info(
            "Skipped %s previously successful records.", original_count - len(records)
        )

    # If PROCESS_LIMIT or PROCESS_LIMIT_PER_FILE is in effect we do NOT move
    # any files, because a file may be only partially processed. Track this
    # to disable moves below.
    process_limited = PROCESS_LIMIT > 0 or PROCESS_LIMIT_PER_FILE > 0
    if PROCESS_LIMIT > 0:
        records = records[:PROCESS_LIMIT]
        logging.info("PROCESS_LIMIT applied. records_to_process=%s", len(records))

    if not records:
        logging.info("Nothing to process after idempotency filtering.")
        return 0

    if DRY_RUN:
        write_dry_run_preview(records)
        return 0

    results: list[dict[str, Any]] = []
    success_count = 0
    failure_count = 0

    # Records whose file matches no QUEUE_ROUTES rule cannot be routed --
    # fail loud for those rather than guessing a queue. (When QUEUE_ROUTES
    # is unset every file resolves to the single TARGET_QUEUE_NAME, so this
    # never triggers in single-queue mode.)
    unrouted_records = [
        record for record in records
        if resolve_queue_name_for_file(record["source_file"]) is None
    ]
    if unrouted_records:
        unrouted_keys = {record["record_key"] for record in unrouted_records}
        for record in unrouted_records:
            failure_count += 1
            append_result(
                results, record, status="error",
                error="No QUEUE_ROUTES rule matched this file's name.",
            )
        logging.error(
            "%s record(s) skipped: no queue route matched their source file. files=%s",
            len(unrouted_records),
            sorted({record["source_file"] for record in unrouted_records}),
        )
        records = [record for record in records if record["record_key"] not in unrouted_keys]

    if not records:
        result_path = write_results(results)
        logging.error(
            "Nothing left to process: every record was unrouted. Result file: %s",
            result_path,
        )
        return 2

    authenticate()

    internal_user_id, internal_user_name = resolve_target_user()
    logging.info("Resolved agent: %s (%s)", internal_user_name, internal_user_id)

    # Resolve (and cache) every distinct queue this run will touch, and
    # confirm the agent is a member of each one used.
    queue_id_cache: dict[str, str] = {}
    record_queue_names = {
        record["record_key"]: resolve_queue_name_for_file(record["source_file"])
        for record in records
    }
    for queue_name in sorted(set(record_queue_names.values())):
        queue_id = resolve_queue_id_cached(queue_name, queue_id_cache)
        if REQUIRE_QUEUE_MEMBERSHIP and not user_is_queue_member(queue_id, internal_user_id):
            raise RuntimeError(
                f"System User NonCall is not a member of queue: {queue_name}"
            )

    if not preflight_routing_state(internal_user_id):
        raise RuntimeError("System User NonCall did not pass routing-state preflight.")
    if not check_or_configure_email_utilization(internal_user_id):
        raise RuntimeError("System User NonCall did not pass email-utilization preflight.")

    resolved_wrapup_id = WRAPUP_CODE_ID
    resolved_wrapup_name = WRAPUP_CODE_NAME
    if not resolved_wrapup_id and WRAPUP_CODE_NAME:
        resolved_wrapup_id, resolved_wrapup_name = lookup_wrapup_code_by_name(
            WRAPUP_CODE_NAME
        )
        if resolved_wrapup_id:
            logging.info(
                "Wrap-up code resolved. name=%s id=%s",
                resolved_wrapup_name, resolved_wrapup_id,
            )
        else:
            logging.warning("Wrap-up code not found. Closing without wrap-up code.")

    published_form_id: Optional[str] = None
    evaluator_user_id: Optional[str] = None
    if CREATE_EVALUATION:
        if not EVALUATION_FORM_ID or not EVALUATOR_USER_EMAIL:
            raise RuntimeError(
                "CREATE_EVALUATION=true requires EVALUATION_FORM_ID and "
                "EVALUATOR_USER_EMAIL."
            )
        published_form_id = resolve_published_form_id(EVALUATION_FORM_ID)
        if not published_form_id:
            raise RuntimeError("Could not resolve a published evaluation form.")
        evaluator_user_id, evaluator_user_name = lookup_user_by_email(
            EVALUATOR_USER_EMAIL
        )
        if not evaluator_user_id:
            raise RuntimeError(f"Evaluator not found: {EVALUATOR_USER_EMAIL}")
        logging.info("Resolved evaluator: %s (%s)", evaluator_user_name, evaluator_user_id)

    # Rows succeeded in THIS run, per file (combined with file_already_success
    # to decide whether a whole file is done).
    file_success: dict[str, int] = {}

    try:
        for position, record in enumerate(records, start=1):
            logging.info(
                "[%s/%s] file=%s row=%s",
                position, len(records), record["source_file"], record["row_number"],
            )
            subject = build_email_subject(record)
            record_queue_id = queue_id_cache[record_queue_names[record["record_key"]]]
            conversation_id = ""
            agent_participant_id = ""
            evaluation_id = ""

            try:
                created_conversation_id, create_response, create_status = (
                    create_email_interaction(
                        record=record,
                        queue_id=record_queue_id,
                        internal_user_id=internal_user_id,
                    )
                )
                if not created_conversation_id:
                    raise RuntimeError(
                        "Email creation failed. "
                        f"http_status={create_status} "
                        f"response={json.dumps(create_response, default=str)}"
                    )
                conversation_id = created_conversation_id

                agent_participant, conversation = wait_for_agent_assignment(
                    conversation_id=conversation_id,
                    internal_user_id=internal_user_id,
                    queue_id=record_queue_id,
                )
                if not agent_participant:
                    if CLOSE_ON_FAILURE:
                        close_email_conversation(
                            conversation_id, None, None, None
                        )
                    raise RuntimeError(
                        "System User NonCall was not confirmed on the interaction."
                    )

                agent_participant_id = str(agent_participant.get("id") or "")
                logging.info(
                    "Assignment verified. conversation_id=%s agent_participant_id=%s",
                    conversation_id, agent_participant_id,
                )

                if CLOSE_ON_SUCCESS:
                    if not close_email_conversation(
                        conversation_id, agent_participant_id,
                        resolved_wrapup_id, resolved_wrapup_name,
                    ):
                        raise RuntimeError(
                            "Created and assigned, but close did not fully succeed."
                        )

                if CREATE_EVALUATION and published_form_id and evaluator_user_id:
                    evaluation_id = create_pending_evaluation(
                        conversation_id, internal_user_id,
                        published_form_id, evaluator_user_id,
                    ) or ""
                    if not evaluation_id:
                        raise RuntimeError("Succeeded, but evaluation creation failed.")

                success_count += 1
                file_success[record["source_path"]] = (
                    file_success.get(record["source_path"], 0) + 1
                )
                append_result(
                    results, record,
                    conversation_id=conversation_id,
                    agent_participant_id=agent_participant_id,
                    evaluation_id=evaluation_id,
                    queue_id=record_queue_id,
                    agent_user_id=internal_user_id,
                    subject=subject,
                    status="success",
                )

            except Exception as record_error:
                failure_count += 1
                logging.exception(
                    "Record failed. file=%s row=%s",
                    record["source_file"], record["row_number"],
                )
                if conversation_id and CLOSE_ON_FAILURE:
                    try:
                        close_email_conversation(
                            conversation_id, agent_participant_id or None, None, None
                        )
                    except Exception:
                        logging.exception(
                            "Failure cleanup also failed. conversation_id=%s",
                            conversation_id,
                        )
                append_result(
                    results, record,
                    conversation_id=conversation_id,
                    agent_participant_id=agent_participant_id,
                    evaluation_id=evaluation_id,
                    queue_id=record_queue_id,
                    agent_user_id=internal_user_id,
                    subject=subject,
                    status="error",
                    error=str(record_error),
                )

            if INTER_RECORD_DELAY_SECONDS > 0:
                time.sleep(INTER_RECORD_DELAY_SECONDS)

    except KeyboardInterrupt:
        logging.warning("Run interrupted by user. Writing partial results.")

    # Move files where EVERY row is accounted for as successful (rows succeeded
    # this run + rows already successful in prior runs == total rows in file).
    moved_files = 0
    if MOVE_PROCESSED_FILES and not process_limited:
        for source_path, total in file_total.items():
            done = file_success.get(source_path, 0) + file_already_success.get(
                source_path, 0
            )
            if done >= total and total > 0:
                if move_processed_file(source_path):
                    moved_files += 1
            else:
                logging.info(
                    "Not moving %s: %s/%s rows succeeded (left in place for re-run).",
                    os.path.basename(source_path), done, total,
                )
    elif process_limited:
        logging.info(
            "PROCESS_LIMIT active -- skipping processed-file move "
            "(files may be only partially processed)."
        )

    result_path = write_results(results)

    logging.info("=" * 72)
    logging.info(
        "Run complete. success=%s failure=%s processed=%s files_moved=%s",
        success_count, failure_count, len(results), moved_files,
    )
    logging.info("Result file: %s", result_path)
    logging.info("Log file: %s", LOG_FILE)
    logging.info("=" * 72)
    return 0 if failure_count == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.exception("Fatal ETL error: %s", exc)
        raise SystemExit(1)
