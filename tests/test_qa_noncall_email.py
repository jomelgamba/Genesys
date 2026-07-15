import csv
from pathlib import Path

import pytest

import qa_noncall_email as module


# =============================================================================
# CSV header / delimiter detection
# =============================================================================

def test_detect_header_skips_title_row(tmp_path):
    content = (
        "Some Report Title\n"
        "textbox7,Member_ID_1\n"
        "Member_ID,Name,Date\n"
        "123,John Doe,2024-01-01\n"
    )
    file_path = tmp_path / "sample.csv"
    file_path.write_text(content, encoding="utf-8")

    header_index, delimiter = module._detect_header_and_delimiter(str(file_path), "utf-8-sig")
    assert (header_index, delimiter) == (2, ",")


def test_detect_header_falls_back_to_line_one_when_no_marker_found(tmp_path):
    content = "colA,colB\n1,2\n"
    file_path = tmp_path / "no_marker.csv"
    file_path.write_text(content, encoding="utf-8")

    header_index, delimiter = module._detect_header_and_delimiter(str(file_path), "utf-8-sig")
    assert (header_index, delimiter) == (0, ",")


def test_detect_header_matches_marker_regardless_of_space_or_underscore(tmp_path):
    content = (
        "Convey Health Solutions\n"
        "Approved Enrollment\n"
        "Processed Date,Member ID,First Name\n"
        "2026-07-09,E6699487991,Kirankumar\n"
    )
    file_path = tmp_path / "enrollment.csv"
    file_path.write_text(content, encoding="utf-8")

    header_index, delimiter = module._detect_header_and_delimiter(str(file_path), "utf-8-sig")
    assert (header_index, delimiter) == (2, ",")


def test_detect_encoding_falls_back_to_cp1252_for_non_utf8_files(tmp_path):
    file_path = tmp_path / "windows_export.csv"
    # Raw byte 0x96 (CP-1252 en dash) is not a valid UTF-8 continuation byte.
    file_path.write_bytes(b"Member_ID,Notes\n123,before\x96after\n")

    assert module._detect_encoding(str(file_path)) == "cp1252"


def test_load_csv_file_reads_cp1252_encoded_report(tmp_path):
    file_path = tmp_path / "windows_export.csv"
    file_path.write_bytes(b"Member_ID,Notes\n123,before\x96after\n")

    records = module.load_csv_file(str(file_path))

    assert len(records) == 1
    assert records[0]["all_fields"]["Notes"].startswith("before")
    assert records[0]["all_fields"]["Notes"].endswith("after")


def test_load_csv_file_raises_when_header_detection_collapses_to_one_column(tmp_path):
    # Real header uses an identifier column no configured marker matches, so
    # detection falls back to line 1 -- a near-empty single-cell title row.
    content = (
        "Convey Health Solutions\n"
        "SLA Returned Mail Detail\n"
        "\n"
        "Account_Number,MBI,Status\n"
        "123,ABC123,Closed\n"
    )
    file_path = tmp_path / "sla_report.csv"
    file_path.write_text(content, encoding="utf-8")

    with pytest.raises(RuntimeError, match="Header detection likely failed"):
        module.load_csv_file(str(file_path))


# =============================================================================
# CSV loading + idempotent record keys
# =============================================================================

def test_load_csv_file_skips_blank_rows_and_counts_row_numbers(tmp_path):
    content = (
        "Member_ID,Name,Date\n"
        "123,John Doe,2024-01-01\n"
        "\n"
        "456,Jane Doe,2024-01-02\n"
    )
    file_path = tmp_path / "data.csv"
    file_path.write_text(content, encoding="utf-8")

    records = module.load_csv_file(str(file_path))

    assert len(records) == 2
    assert records[0]["all_fields"]["Member_ID"] == "123"
    assert records[0]["row_number"] == 2
    assert records[1]["row_number"] == 4


def test_record_key_is_stable_and_content_sensitive(tmp_path):
    content = "Member_ID,Name,Date\n123,John Doe,2024-01-01\n"
    file_path = tmp_path / "data.csv"
    file_path.write_text(content, encoding="utf-8")

    first_pass = module.load_csv_file(str(file_path))
    second_pass = module.load_csv_file(str(file_path))
    assert first_pass[0]["record_key"] == second_pass[0]["record_key"]

    file_path.write_text(content.replace("John Doe", "John Doe Jr"), encoding="utf-8")
    changed_pass = module.load_csv_file(str(file_path))
    assert changed_pass[0]["record_key"] != first_pass[0]["record_key"]


# =============================================================================
# Email payload construction
# =============================================================================

def test_build_customer_attributes_sanitizes_column_names():
    record = {
        "record_key": "abc-123",
        "source_file": "sample.csv",
        "row_number": 5,
        "all_fields": {"Member ID#": "999", "Notes (QA)": "looks good"},
    }
    attrs = module.build_customer_attributes(record)
    assert attrs["qa.recordKey"] == "abc-123"
    assert attrs["csv.Member_ID_"] == "999"
    assert attrs["csv.Notes__QA_"] == "looks good"


def test_build_email_payload_includes_queue_and_user(monkeypatch):
    monkeypatch.setattr(module, "EMAIL_FROM_ADDRESS", "from@example.com")
    monkeypatch.setattr(module, "EMAIL_PROVIDER", "TestProvider")
    record = {
        "record_key": "key-1",
        "source_file": "sample.csv",
        "row_number": 2,
        "all_fields": {"Member_ID": "123"},
    }
    payload = module.build_email_payload(record, queue_id="queue-1", internal_user_id="user-1")
    assert payload["queueId"] == "queue-1"
    assert payload["userId"] == "user-1"
    assert payload["fromAddress"] == "from@example.com"
    assert payload["provider"] == "TestProvider"
    assert "Row 2" in payload["subject"]


# =============================================================================
# api_request retry/backoff behavior
# =============================================================================

class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.content = b"{}"
        self.text = "{}"
        self.headers: dict[str, str] = {}

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch):
    monkeypatch.setitem(module.token_info, "access_token", "fake-token")


def test_api_request_retries_network_errors_then_succeeds(monkeypatch):
    calls = {"count": 0}

    def fake_request(method, url, headers, timeout, **kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise module.requests.exceptions.ConnectionError("boom")
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(module.requests, "request", fake_request)

    response = module.api_request("GET", "/api/v2/whatever")
    assert response.status_code == 200
    assert calls["count"] == 3


def test_api_request_raises_clear_error_after_persistent_network_failure(monkeypatch):
    def fake_request(*args, **kwargs):
        raise module.requests.exceptions.ConnectionError("still down")

    monkeypatch.setattr(module.requests, "request", fake_request)

    with pytest.raises(RuntimeError, match="Network error"):
        module.api_request("GET", "/api/v2/whatever")


def test_api_request_retries_on_server_error_then_succeeds(monkeypatch):
    calls = {"count": 0}

    def fake_request(method, url, headers, timeout, **kwargs):
        calls["count"] += 1
        if calls["count"] < 2:
            return _FakeResponse(503)
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(module.requests, "request", fake_request)

    response = module.api_request("GET", "/api/v2/whatever")
    assert response.status_code == 200
    assert calls["count"] == 2


# =============================================================================
# Close / wrap-up
# =============================================================================

def test_close_email_conversation_disconnects_agent_and_other_participants(monkeypatch):
    conversation = {
        "participants": [
            {"id": "agent-1", "state": "connected"},
            {"id": "customer-1", "state": "connected"},
            {"id": "already-gone", "state": "disconnected"},
        ]
    }
    calls: list[tuple[str, dict]] = []

    def fake_get_email_conversation(conversation_id):
        return conversation

    def fake_patch(conversation_id, participant_id, payload):
        calls.append((participant_id, payload))
        return _FakeResponse(200)

    monkeypatch.setattr(module, "get_email_conversation", fake_get_email_conversation)
    monkeypatch.setattr(module, "patch_email_participant", fake_patch)

    result = module.close_email_conversation(
        "conv-1", "agent-1", "wrapup-code-id", "Evaluated"
    )

    assert result is True
    assert calls[0] == (
        "agent-1",
        {
            "state": "disconnected",
            "wrapup": {"code": "wrapup-code-id", "name": "Evaluated", "provisional": False},
        },
    )
    assert ("customer-1", {"state": "disconnected"}) in calls
    assert not any(participant_id == "already-gone" for participant_id, _ in calls)


def test_close_email_conversation_returns_false_when_conversation_unreadable(monkeypatch):
    monkeypatch.setattr(module, "get_email_conversation", lambda conversation_id: None)
    assert module.close_email_conversation("conv-1", "agent-1", None, None) is False


# =============================================================================
# Idempotency ledger
# =============================================================================

def test_load_previous_success_keys_only_returns_successes(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(module, "SKIP_PREVIOUS_SUCCESSES", True)
    monkeypatch.setattr(module, "RESULT_FILE_PREFIX", "qa_email_insert_results")

    ledger_path = tmp_path / "qa_email_insert_results_20240101000000_aaaaaaaa.csv"
    blank_row = {field: "" for field in module.RESULT_FIELDS}
    with ledger_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=module.RESULT_FIELDS)
        writer.writeheader()
        writer.writerow({**blank_row, "record_key": "key-success", "status": "success"})
        writer.writerow({**blank_row, "record_key": "key-error", "status": "error"})

    assert module.load_previous_success_keys() == {"key-success"}


def test_load_previous_success_keys_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(module, "SKIP_PREVIOUS_SUCCESSES", False)
    assert module.load_previous_success_keys() == set()


# =============================================================================
# Config validation
# =============================================================================

def test_validate_config_raises_for_missing_required_values(monkeypatch):
    monkeypatch.setattr(module, "CLIENT_ID", "")
    with pytest.raises(RuntimeError, match="Missing required configuration"):
        module.validate_config()


def test_validate_config_requires_queue_identifier(monkeypatch):
    monkeypatch.setattr(module, "CLIENT_ID", "id")
    monkeypatch.setattr(module, "CLIENT_SECRET", "secret")
    monkeypatch.setattr(module, "MIRAMAR_FOLDER", ".")
    monkeypatch.setattr(module, "EMAIL_PROVIDER", "provider")
    monkeypatch.setattr(module, "EMAIL_FROM_ADDRESS", "from@example.com")
    monkeypatch.setattr(module, "TARGET_QUEUE_ID", "")
    monkeypatch.setattr(module, "TARGET_QUEUE_NAME", "")
    monkeypatch.setattr(module, "INTERNAL_USER_ID", "id")
    monkeypatch.setattr(module, "INTERNAL_USER_EMAIL", "user@example.com")
    with pytest.raises(RuntimeError, match="TARGET_QUEUE"):
        module.validate_config()


def test_validate_config_requires_user_identifier(monkeypatch):
    monkeypatch.setattr(module, "CLIENT_ID", "id")
    monkeypatch.setattr(module, "CLIENT_SECRET", "secret")
    monkeypatch.setattr(module, "MIRAMAR_FOLDER", ".")
    monkeypatch.setattr(module, "EMAIL_PROVIDER", "provider")
    monkeypatch.setattr(module, "EMAIL_FROM_ADDRESS", "from@example.com")
    monkeypatch.setattr(module, "TARGET_QUEUE_ID", "queue-id")
    monkeypatch.setattr(module, "TARGET_QUEUE_NAME", "")
    monkeypatch.setattr(module, "INTERNAL_USER_ID", "")
    monkeypatch.setattr(module, "INTERNAL_USER_EMAIL", "")
    with pytest.raises(RuntimeError, match="INTERNAL_USER"):
        module.validate_config()


def test_validate_config_dry_run_skips_credential_and_queue_checks(monkeypatch):
    monkeypatch.setattr(module, "DRY_RUN", True)
    monkeypatch.setattr(module, "CLIENT_ID", "")
    monkeypatch.setattr(module, "CLIENT_SECRET", "")
    monkeypatch.setattr(module, "MIRAMAR_FOLDER", ".")
    monkeypatch.setattr(module, "EMAIL_PROVIDER", "provider")
    monkeypatch.setattr(module, "EMAIL_FROM_ADDRESS", "from@example.com")
    monkeypatch.setattr(module, "TARGET_QUEUE_ID", "")
    monkeypatch.setattr(module, "TARGET_QUEUE_NAME", "")
    monkeypatch.setattr(module, "INTERNAL_USER_ID", "")
    monkeypatch.setattr(module, "INTERNAL_USER_EMAIL", "")
    module.validate_config()  # should not raise


def test_validate_config_dry_run_still_requires_email_identity(monkeypatch):
    monkeypatch.setattr(module, "DRY_RUN", True)
    monkeypatch.setattr(module, "MIRAMAR_FOLDER", ".")
    monkeypatch.setattr(module, "EMAIL_PROVIDER", "")
    monkeypatch.setattr(module, "EMAIL_FROM_ADDRESS", "from@example.com")
    with pytest.raises(RuntimeError, match="Missing required configuration"):
        module.validate_config()


def test_validate_config_queue_routes_satisfies_queue_identification(monkeypatch):
    monkeypatch.setattr(module, "CLIENT_ID", "id")
    monkeypatch.setattr(module, "CLIENT_SECRET", "secret")
    monkeypatch.setattr(module, "MIRAMAR_FOLDER", ".")
    monkeypatch.setattr(module, "EMAIL_PROVIDER", "provider")
    monkeypatch.setattr(module, "EMAIL_FROM_ADDRESS", "from@example.com")
    monkeypatch.setattr(module, "TARGET_QUEUE_ID", "")
    monkeypatch.setattr(module, "TARGET_QUEUE_NAME", "")
    monkeypatch.setattr(module, "INTERNAL_USER_ID", "id")
    monkeypatch.setattr(module, "INTERNAL_USER_EMAIL", "user@example.com")
    monkeypatch.setattr(module, "QUEUE_ROUTES", [("Approved Enrollment", "SomeQueue")])
    module.validate_config()  # should not raise


# =============================================================================
# Multi-queue routing (QUEUE_ROUTES)
# =============================================================================

def test_parse_queue_routes_parses_multiple_rules():
    routes = module._parse_queue_routes(
        "Approved Enrollment::HCSC_EGWP_NONCALL_Approved_Enrollment;"
        "Group Invoice Report::HCSC_EGWP_NONCALL_Group_Invoice"
    )
    assert routes == [
        ("Approved Enrollment", "HCSC_EGWP_NONCALL_Approved_Enrollment"),
        ("Group Invoice Report", "HCSC_EGWP_NONCALL_Group_Invoice"),
    ]


def test_parse_queue_routes_empty_string_yields_no_rules():
    assert module._parse_queue_routes("") == []
    assert module._parse_queue_routes("   ") == []


def test_parse_queue_routes_rejects_rule_without_separator():
    with pytest.raises(RuntimeError, match="Invalid QUEUE_ROUTES rule"):
        module._parse_queue_routes("Approved Enrollment -> SomeQueue")


def test_parse_queue_routes_rejects_empty_match_or_queue():
    with pytest.raises(RuntimeError, match="Invalid QUEUE_ROUTES rule"):
        module._parse_queue_routes("::SomeQueue")
    with pytest.raises(RuntimeError, match="Invalid QUEUE_ROUTES rule"):
        module._parse_queue_routes("SomeFile::")


def test_resolve_queue_name_for_file_matches_case_insensitive_substring(monkeypatch):
    monkeypatch.setattr(module, "QUEUE_ROUTES", [
        ("Approved Enrollment", "HCSC_EGWP_NONCALL_Approved_Enrollment"),
        ("Group Invoice Report", "HCSC_EGWP_NONCALL_Group_Invoice"),
    ])
    assert (
        module.resolve_queue_name_for_file("7282_approved enrollment.csv")
        == "HCSC_EGWP_NONCALL_Approved_Enrollment"
    )
    assert (
        module.resolve_queue_name_for_file("Group Invoice Report - 202605.csv")
        == "HCSC_EGWP_NONCALL_Group_Invoice"
    )


def test_resolve_queue_name_for_file_returns_none_when_unmatched(monkeypatch):
    monkeypatch.setattr(module, "QUEUE_ROUTES", [
        ("Approved Enrollment", "SomeQueue"),
    ])
    assert module.resolve_queue_name_for_file("SLA_Returned_Mail_Report_V6.csv") is None


def test_resolve_queue_name_for_file_falls_back_to_target_queue_name_when_no_routes(monkeypatch):
    monkeypatch.setattr(module, "QUEUE_ROUTES", [])
    monkeypatch.setattr(module, "TARGET_QUEUE_NAME", "NonCall Evaluation")
    assert module.resolve_queue_name_for_file("anything.csv") == "NonCall Evaluation"


def test_resolve_queue_id_cached_resolves_by_name_and_caches(monkeypatch):
    calls = {"count": 0}

    def fake_lookup(queue_name):
        calls["count"] += 1
        return "queue-id-123", queue_name

    monkeypatch.setattr(module, "TARGET_QUEUE_NAME", "")
    monkeypatch.setattr(module, "TARGET_QUEUE_ID", "")
    monkeypatch.setattr(module, "lookup_queue_by_name", fake_lookup)

    cache: dict = {}
    first = module.resolve_queue_id_cached("HCSC_PDP_NONCALL_Returned_Mail", cache)
    second = module.resolve_queue_id_cached("HCSC_PDP_NONCALL_Returned_Mail", cache)

    assert first == second == "queue-id-123"
    assert calls["count"] == 1  # second call served from cache


def test_resolve_queue_id_cached_raises_when_queue_not_found(monkeypatch):
    monkeypatch.setattr(module, "TARGET_QUEUE_NAME", "")
    monkeypatch.setattr(module, "TARGET_QUEUE_ID", "")
    monkeypatch.setattr(module, "lookup_queue_by_name", lambda name: (None, None))

    with pytest.raises(RuntimeError, match="Queue not found"):
        module.resolve_queue_id_cached("NoSuchQueue", {})


def test_resolve_queue_id_cached_uses_resolve_target_queue_for_default_queue(monkeypatch):
    calls = {"count": 0}

    def fake_resolve_target_queue():
        calls["count"] += 1
        return "default-queue-id", "NonCall Evaluation"

    monkeypatch.setattr(module, "TARGET_QUEUE_NAME", "NonCall Evaluation")
    monkeypatch.setattr(module, "TARGET_QUEUE_ID", "some-id")
    monkeypatch.setattr(module, "resolve_target_queue", fake_resolve_target_queue)

    queue_id = module.resolve_queue_id_cached("NonCall Evaluation", {})

    assert queue_id == "default-queue-id"
    assert calls["count"] == 1


def test_write_dry_run_preview_shows_unrouted_for_unmatched_files(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(module, "EMAIL_FROM_ADDRESS", "from@example.com")
    monkeypatch.setattr(module, "EMAIL_PROVIDER", "TestProvider")
    monkeypatch.setattr(module, "QUEUE_ROUTES", [
        ("routed_file", "SomeQueue"),
    ])

    routed = {
        "record_key": "key-1", "source_file": "routed_file.csv",
        "row_number": 2, "all_fields": {"Member_ID": "1"},
    }
    unrouted = {
        "record_key": "key-2", "source_file": "unknown_file.csv",
        "row_number": 2, "all_fields": {"Member_ID": "2"},
    }

    preview_path = module.write_dry_run_preview([routed, unrouted])

    with open(preview_path, newline="", encoding="utf-8-sig") as handle:
        rows = {row["record_key"]: row for row in csv.DictReader(handle)}
    assert rows["key-1"]["target_queue_name"] == "SomeQueue"
    assert rows["key-2"]["target_queue_name"] == "UNROUTED"


# =============================================================================
# Per-file record sampling (load_all_records)
# =============================================================================

def test_load_all_records_applies_per_file_limit(tmp_path):
    file_one = tmp_path / "one.csv"
    file_one.write_text(
        "Member_ID,Name\n101,Alice\n102,Bob\n103,Carol\n", encoding="utf-8"
    )
    file_two = tmp_path / "two.csv"
    file_two.write_text(
        "Member_ID,Name\n201,Dan\n202,Erin\n", encoding="utf-8"
    )

    records = module.load_all_records([str(file_one), str(file_two)], limit_per_file=1)

    assert len(records) == 2
    assert {r["source_file"] for r in records} == {"one.csv", "two.csv"}
    by_file = {r["source_file"]: r for r in records}
    assert by_file["one.csv"]["all_fields"]["Member_ID"] == "101"
    assert by_file["two.csv"]["all_fields"]["Member_ID"] == "201"


def test_load_all_records_no_limit_loads_everything(tmp_path):
    file_one = tmp_path / "one.csv"
    file_one.write_text(
        "Member_ID,Name\n101,Alice\n102,Bob\n103,Carol\n", encoding="utf-8"
    )

    records = module.load_all_records([str(file_one)], limit_per_file=0)

    assert len(records) == 3


# =============================================================================
# Dry run preview
# =============================================================================

def test_write_dry_run_preview_builds_payloads_without_api_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(module, "EMAIL_FROM_ADDRESS", "from@example.com")
    monkeypatch.setattr(module, "EMAIL_PROVIDER", "TestProvider")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("DRY_RUN must not make any API calls")

    monkeypatch.setattr(module, "api_request", fail_if_called)
    monkeypatch.setattr(module, "authenticate", fail_if_called)

    record = {
        "record_key": "key-1",
        "source_file": "sample.csv",
        "row_number": 2,
        "all_fields": {"Member_ID": "123"},
    }

    preview_path = module.write_dry_run_preview([record])

    assert Path(preview_path).exists()
    with open(preview_path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["source_file"] == "sample.csv"
    assert rows[0]["record_key"] == "key-1"


# =============================================================================
# Env parsing helpers
# =============================================================================

def test_env_int_raises_clear_error_for_bad_value(monkeypatch):
    monkeypatch.setenv("SOME_INT_VAR", "not-a-number")
    with pytest.raises(RuntimeError, match="Invalid integer value for SOME_INT_VAR"):
        module._env_int("SOME_INT_VAR", "10")


def test_env_float_raises_clear_error_for_bad_value(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT_VAR", "nope")
    with pytest.raises(RuntimeError, match="Invalid numeric value for SOME_FLOAT_VAR"):
        module._env_float("SOME_FLOAT_VAR", "1.5")


def test_env_int_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_INT_VAR", raising=False)
    assert module._env_int("SOME_INT_VAR", "42") == 42
