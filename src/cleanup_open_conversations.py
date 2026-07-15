"""
Finds -- and, only when explicitly confirmed, closes -- open (not yet
ended) conversations sitting in a Genesys Cloud queue. Built for cleaning
up interactions orphaned by an interrupted qa_noncall_email.py run (a
Ctrl+C mid-run can leave a conversation open, occupying the agent's email
capacity and blocking every subsequent run).

Pass --config pointing at one of the configs/*.env files to load its
CLIENT_ID/CLIENT_SECRET/GENESYS_REGION (same credentials the matching
run_*.py launcher uses) -- this works regardless of working directory, so
it's the reliable way to run this from PyCharm or anywhere else.

SAFE BY DEFAULT: lists what it finds and does nothing else. Nothing is
closed unless you pass --close AND type "yes" at the confirmation prompt.

Usage:
    python cleanup_open_conversations.py --config ../configs/hcsc_pdp.env --queue "HCSC PDP NONCALL Approved Enrollment"
    python cleanup_open_conversations.py --config ../configs/hcsc_pdp.env --queue "HCSC PDP NONCALL Approved Enrollment" --days 3
    python cleanup_open_conversations.py --config ../configs/hcsc_pdp.env --queue "HCSC PDP NONCALL Approved Enrollment" --close
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _preload_config_from_argv() -> None:
    """
    Parses --config out of sys.argv and loads it *before* qa_noncall_email
    is imported, since that import reads CLIENT_ID/CLIENT_SECRET/etc from
    the environment immediately at module load time -- too early for a
    normal argparse.parse_args() call in main() to have any effect.
    """
    if "--config" not in sys.argv:
        return
    from dotenv import load_dotenv

    index = sys.argv.index("--config")
    if index + 1 >= len(sys.argv):
        raise SystemExit("--config requires a path argument")
    config_path = Path(sys.argv[index + 1])
    if not config_path.is_file():
        raise SystemExit(f"Config file not found: {config_path}")
    load_dotenv(config_path, override=True)


_preload_config_from_argv()

import qa_noncall_email as etl  # noqa: E402


def find_open_conversations(queue_id: str, days: int) -> list[dict[str, Any]]:
    """
    Queries the Conversation Detail Query API for conversations in the
    queue over the last `days` days, returning only those with no
    conversationEnd (still open).
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    interval = f"{start.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z/{now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z"

    open_conversations: list[dict[str, Any]] = []
    page_number = 1
    while True:
        response = etl.api_request(
            "POST", "/api/v2/analytics/conversations/details/query",
            expected_statuses=(200,),
            json={
                "interval": interval,
                "order": "desc",
                "orderBy": "conversationStart",
                "paging": {"pageSize": 100, "pageNumber": page_number},
                "segmentFilters": [
                    {
                        "type": "and",
                        "predicates": [
                            {"type": "dimension", "dimension": "queueId", "operator": "matches", "value": queue_id},
                        ],
                    }
                ],
            },
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Conversation query failed. status={response.status_code} body={response.text[:1000]}"
            )
        payload = etl.response_json(response)
        conversations = payload.get("conversations", [])
        for conversation in conversations:
            if not conversation.get("conversationEnd"):
                open_conversations.append(conversation)

        total_pages = int(payload.get("totalPages") or 1)
        if page_number >= total_pages or not conversations:
            break
        page_number += 1

    return open_conversations


def summarize(conversation: dict[str, Any]) -> str:
    conversation_id = conversation.get("conversationId", "?")
    started = conversation.get("conversationStart", "?")
    participant_names = [
        p.get("purpose", "?")
        for p in conversation.get("participants", [])
    ]
    return f"  {conversation_id}  started={started}  participants={participant_names}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, help="Genesys queue name to search")
    parser.add_argument("--days", type=int, default=7, help="How many days back to search (default 7)")
    parser.add_argument("--close", action="store_true", help="Actually close what's found (asks for confirmation)")
    parser.add_argument(
        "--config",
        help="Path to a configs/*.env file to load CLIENT_ID/CLIENT_SECRET/etc from "
        "(already applied before this point -- listed here only so it shows in --help "
        "and gets validated as a normal argument).",
    )
    args = parser.parse_args()

    etl.authenticate()

    queue_id, resolved_name = etl.lookup_queue_by_name(args.queue)
    if not queue_id:
        print(f"Queue not found: {args.queue}", file=sys.stderr)
        return 1
    print(f"Queue: {resolved_name} ({queue_id})")

    open_conversations = find_open_conversations(queue_id, args.days)
    if not open_conversations:
        print(f"No open conversations found in the last {args.days} day(s).")
        return 0

    print(f"\nFound {len(open_conversations)} open conversation(s):")
    for conversation in open_conversations:
        print(summarize(conversation))

    if not args.close:
        print("\nList-only (pass --close to close these). Nothing was changed.")
        return 0

    print(f"\nAbout to close {len(open_conversations)} conversation(s) in queue '{resolved_name}'.")
    confirmation = input("Type 'yes' to proceed: ").strip().lower()
    if confirmation != "yes":
        print("Aborted. Nothing was changed.")
        return 0

    closed = 0
    failed = 0
    for conversation in open_conversations:
        conversation_id = conversation.get("conversationId")
        agent_participant_id = None
        for participant in conversation.get("participants", []):
            if str(participant.get("purpose", "")).casefold() == "agent":
                sessions = participant.get("sessions", [{}])
                agent_participant_id = participant.get("participantId") or (
                    sessions[0].get("participantId") if sessions else None
                )
        try:
            success = etl.close_email_conversation(conversation_id, agent_participant_id, None, None)
            if success:
                closed += 1
                print(f"  Closed: {conversation_id}")
            else:
                failed += 1
                print(f"  Partial/failed close: {conversation_id} (check Genesys Cloud)")
        except Exception as exc:
            failed += 1
            print(f"  ERROR closing {conversation_id}: {exc}")

    print(f"\nDone. closed={closed} failed={failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
