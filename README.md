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
3. Set teams (e.g. `ETL,CD`), date range, members to compare (comma-separated).
4. Optionally add custom bug views as `LABEL:slugId` (one per line). The slugId is the last hex chunk in the view's URL: `…/view/<slug>-<slugId>`.
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
python3 scripts/linear_cycle_report.py --team <KEY> --from <N> --to <N>

# Combined weekly view across teams + bug views
python3 scripts/linear_weekly_report.py \
  --teams <KEY1>,<KEY2> \
  --bug-view <LABEL>:<slugId> \
  --from 2026-03-02 --to 2026-05-03 \
  --compare <name1>,<name2>
```

## How it works

For each cycle X with date range [start, end]:
- **Planned** = issues whose `cycleId == X` at end (reconstructed from `IssueHistory` events; counts issues that were committed to X then slipped to next cycle).
- **Completed** = subset that completed during the cycle.
- Issues retroactively moved back to X *after* close are excluded (Linear UI doesn't count those either).
- Unestimated issues count as 1 point.

For weekly view:
- 1-week cycles align to weeks 1:1.
- 2-week (biweekly) cycles span 2 weeks → "Per-Week" columns split their pts proportionally (cycle_pts / 2). "Active cycles" columns show the full cycle pts (useful for "what's on the plate this week" but double-counts biweekly cycles across two consecutive weeks).
