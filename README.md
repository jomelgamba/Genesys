# genesys

## NonCall QA Email ETL

`src/qa_noncall_email.py` reads CSV files from a folder, and for every row
creates an inbound email interaction on a Genesys Cloud queue (via the
Genesys Cloud API), waits for it to be assigned to a configured system
user, then closes it out with a wrap-up code. Each row's full contents are
placed in the interaction body/attributes with no column mapping required.

The script is idempotent: a CSV file is only moved to `processed/` once
every row in it has a recorded success, and prior successes (tracked in the
`qa_email_insert_results_*.csv` ledgers written to `OUTPUT_DIR`) are skipped
on re-run.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in CLIENT_ID, CLIENT_SECRET, MIRAMAR_FOLDER, etc.
```

### Run

```bash
python src/qa_noncall_email.py
```

### Tests

```bash
pip install pytest
pytest
```

Tests run entirely offline — Genesys API calls are mocked, so no `.env` or
network access is required.
