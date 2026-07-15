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

### Run (single-queue / ad hoc)

```bash
python src/qa_noncall_email.py
```

### Run (per line-of-business, multi-queue)

Four report groups each run as their own scheduled job, sharing the same
core ETL but with their own `MIRAMAR_FOLDER`, output directory, and queue
routing (`configs/*.env`):

| Script | Line of Business | Queue routing |
|---|---|---|
| `src/run_medica.py` | MEDICA | single queue (`TARGET_QUEUE_NAME`) |
| `src/run_optum_egwp.py` | Optum EGWP | single queue (`TARGET_QUEUE_NAME`) |
| `src/run_hcsc_egwp.py` | HCSC EGWP | 2 report types -> 2 queues (`QUEUE_ROUTES`) |
| `src/run_hcsc_pdp.py` | HCSC PDP Individual | 6 report types -> 6 queues (`QUEUE_ROUTES`) |

Each `MIRAMAR_FOLDER` should contain **only** that line of business's
report files -- a file that doesn't match any configured queue route is
skipped and logged as an error rather than sent anywhere. Run one:

```bash
python src/run_hcsc_pdp.py
```

`QUEUE_ROUTES` format: `<filename substring>::<queue name>` pairs
separated by `;`, matched case-insensitively. See any `configs/*.env` for a
real example.

### Tests

```bash
pip install pytest
pytest
```

Tests run entirely offline — Genesys API calls are mocked, so no `.env` or
network access is required.
