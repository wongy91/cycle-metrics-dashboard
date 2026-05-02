# Linear Cycle Metrics Dashboard

Per-week, per-member success-rate analysis across multiple Linear teams (e.g. ETL weekly + CD biweekly + EXL biweekly), with optional bug-view tracking from Linear custom views.

Two ways to use:

1. **Dashboard (`index.html`)** — open in a browser. Paste your Linear API key, pick params, click Analyze. Renders comparison and long-format tables, downloadable as CSV.
2. **CLI (`scripts/`)** — Python scripts for batch generation if you want CSVs without the browser.

## Dashboard

Open `index.html` in a browser. No build step.

```bash
open index.html
# or serve locally if your browser blocks file:// fetch:
python3 -m http.server -d . 8000
# then visit http://localhost:8000
```

**First run:**
1. Generate a Linear personal API key at <https://linear.app/settings/api>.
2. Paste into the **Linear API Key** field, click **Save** (stored in browser localStorage).
3. Set teams (default `ETL,CD,EXL`), date range, members to compare (default `jack,yishern,shenwei,mengyit`).
4. Optionally add custom bug views as `LABEL:slugId` (one per line). The slugId is the last hex chunk in the view's URL: `…/view/bugs-cd-b0fd1ffdeb3f` → `b0fd1ffdeb3f`.
5. Click **Analyze**.

**Output:**
- **Comparison view**: one row per week, columns grouped by member.
- **Long view**: one row per (week × member), all teams on one row.
- Both downloadable as CSV.

**Color cues** in the table:
- Success cells: green ≥80%, yellow 50–79%, red <50%.
- Bugs Open: red ≥6, yellow 3–5.

### Hosting (optional)

Drop the folder onto Vercel / Netlify / Cloudflare Pages. Add password protection via the host's built-in feature. Don't expose unprotected — performance data per individual is sensitive.

## CLI scripts

```bash
export LINEAR_API_KEY=lin_api_...

# Per-cycle CSV for one team
python3 scripts/linear_cycle_report.py --team ETL --from 30 --to 38

# Combined weekly view across teams + bug views
python3 scripts/linear_weekly_report.py \
  --teams ETL,CD,EXL \
  --bug-view ETL1:b0fd1ffdeb3f --bug-view ETL2:bff8b4260fff \
  --from 2026-03-02 --to 2026-05-03 \
  --compare jack,yishern,shenwei,mengyit
```

## How it works

For each cycle X with date range [start, end]:
- **Planned** = issues whose `cycleId == X` at end (reconstructed from `IssueHistory` events; counts issues that were committed to X then slipped to next cycle).
- **Completed** = subset that completed during the cycle.
- Issues retroactively moved back to X *after* close are excluded (Linear UI doesn't count those either).
- Unestimated issues count as 1 point.

For weekly view:
- ETL cycles align to weeks 1:1.
- CD/EXL cycles span 2 weeks → "Per-Week" columns split CD/EXL pts proportionally. "Active cycles" columns show the full cycle (helpful for "what's on the plate this week" but double-counts CD across two consecutive weeks).
