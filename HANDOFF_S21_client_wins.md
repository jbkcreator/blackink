# HANDOFF — S-21 Client Wins Dashboard (test on the server)

**Branch:** `feature/week2-3.2.5-client-wins-dashboard`
**Why on the server:** your laptop can't reach Postgres (`5.78.154.226:5432` is
firewalled to the office IP). On the server the DB is `localhost:5432`, so the
migration + e2e run with no tunnel.

**What S-21 delivers:** a per-client wins scoreboard — `compute_wins` →
`export_client_wins` writes each client's wins into its own Google Sheet →
Looker Studio renders it. Plus an admin CSV export endpoint (AC#5). Reads run
under the system role (no Looker-direct-DB, no RLS surface). Metrics with no
producer yet (saved doors, ancillary revenue) render `NOT RECORDED`, never a
fake 0.

---

## 0. One-time prerequisites on the server

- **Google creds file present:** copy `gsheets-sa.json` (the
  `blackink-sheets-export@blackink-507509.iam.gserviceaccount.com` key) to the
  server, e.g. `/root/blackink/gsheets-sa.json`. It is gitignored — it will NOT
  arrive via `git pull`, copy it manually (scp).
- **Sheets API enabled** in project `blackink-507509` (already done) and the
  target Sheet shared as **Editor** with that service-account email (already
  done for Sheet `1_HHeaCyXDxASDcHqv2tsPq_sDk4YX3CBqlgMx5hrruY`).
- **gspread installed** in the server venv (already a repo dependency; if not:
  `pip install gspread`).

---

## 1. Get onto the server and check out the branch

```bash
ssh root@5.78.154.226           # your normal server login
cd /root/blackink               # the deployed repo path
git fetch origin
git checkout feature/week2-3.2.5-client-wins-dashboard
git pull origin feature/week2-3.2.5-client-wins-dashboard
```

Use the server's real env (DB is localhost there). Do NOT set ENV_FILE to a
laptop value:

```bash
export PYTHONPATH=.
export GOOGLE_SHEETS_CREDENTIALS_PATH=/root/blackink/gsheets-sa.json
# ENV_FILE stays whatever the server normally uses (.env), pointing at localhost:5432
```

---

## 2. Apply the migration (additive — adds clients.wins_sheet_id)

```bash
PYTHONPATH=. python migrations/apply_clients_wins_sheet.py
# expect: apply_clients_wins_sheet: done
```

Safe/idempotent (`ADD COLUMN IF NOT EXISTS`). Rollback if ever needed:
`ALTER TABLE clients DROP COLUMN IF EXISTS wins_sheet_id;`

---

## 3. Run the end-to-end harness (self-cleaning)

The harness guard requires `ENV_FILE=.env.test`. On the server that file points
at the same `blackink` DB (there is only one DB). It seeds a throwaway
`E2E_WINS_<runid>` client, runs the export sweep to the REAL Sheet, hits the
admin endpoints, and deletes everything it created on exit.

```bash
ENV_FILE=.env.test PYTHONPATH=. \
  GOOGLE_SHEETS_CREDENTIALS_PATH=/root/blackink/gsheets-sa.json \
  python scripts/e2e_client_wins.py
```

**Expected:** a list of `PASS` lines ending in `Results: N passed, 0 failed`,
including:
- attended appointments = 2, meetings booked = 3, signed agreements = 1,
  total doors signed = 47
- `sweep exported at least this client`
- `sheet shows attended count` / `sheet shows doors signed` (read back from the
  live Google Sheet)
- JSON endpoint 200, CSV endpoint 200 + attachment, unknown client → 404,
  no-auth → 401 (these run only if `ADMIN_JWT_SECRET` is set on the server;
  otherwise that one check is flagged and skipped — not a failure of the export)

If the endpoint checks skip: set `ADMIN_JWT_SECRET` in the server env and
re-run to exercise them.

If anything fails, copy the `FAIL ...` line(s) back to me.

---

## 4. (Optional) wire a REAL client so its dashboard shows live data

Pick a real tenant and point it at a Sheet you created + shared with the
service account:

```bash
# list clients to choose a client_id
psql "$DATABASE_URL" -c "SELECT client_id, display_name, is_active FROM clients ORDER BY client_id;"

# set that client's wins sheet
psql "$DATABASE_URL" -c "UPDATE clients SET wins_sheet_id='<THE_SHEET_ID>' WHERE client_id='<CLIENT_ID>';"

# run the sweep for all active clients with a sheet
PYTHONPATH=. GOOGLE_SHEETS_CREDENTIALS_PATH=/root/blackink/gsheets-sa.json \
  python -m src.tasks.client_wins_sweep
```

Then in Looker Studio: Add data → Google Sheets connector → that Sheet →
Add a chart → **Table** → Dimension = Metric, Dimension = Value (remove the
default Record Count metric).

**Sheet ID** = the string in the Sheet URL between `/d/` and `/edit`.
**client_id** = the tenant key from the `clients` query above.

---

## 5. Schedule (already wired, verify only)

`scripts/crontab.txt` runs the sweep every 15 min:
```
*/15 * * * * ... -m src.tasks.client_wins_sweep >> .../logs/client_wins_sweep.log 2>&1
```
Confirm it's in the server crontab if you want automatic refresh.

---

## 6. Report back

Paste the `Results: N passed, M failed` line (and any `FAIL`). If green, I run
`blackink-review` and open the PR (per the rule: PR only after e2e passes).

## Files in this branch
- `migrations/apply_clients_wins_sheet.py` — clients.wins_sheet_id
- `src/services/client_wins.py` — compute_wins / export_client_wins / wins_csv
- `src/tasks/client_wins_sweep.py` — per-client export sweep (cron 15 min)
- `src/api/client_wins_router.py` — admin JSON + CSV endpoints
- `src/core/models.py` — Client.wins_sheet_id
- `scripts/e2e_client_wins.py` — this harness
- `tests/test_client_wins.py` — 7 unit tests (already green locally)
- CLAUDE.md / both CI workflows / run_migrations.sh — migration registered
