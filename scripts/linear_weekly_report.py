#!/usr/bin/env python3
"""
Per-week managerial view across multiple Linear teams (e.g., ETL weekly +
CD biweekly + EXL biweekly). For each (week, member) combo, surface each
team's active cycle commitment, bug backlog from configured custom views,
and combined totals.

Usage:
  LINEAR_API_KEY=lin_api_... \\
    python3 scripts/linear_weekly_report.py \\
      --teams TEAM1,TEAM2 \\
      --from 2026-03-09 --to 2026-05-03 \\
      [--member <name>] \\
      [--bug-view <label>:<slugId>] ...

--bug-view LABEL:slugId can be repeated. Adds two columns per view:
  '<LABEL> Bugs Open (end of wk)' and '<LABEL> Bugs Resolved (this wk)'.

When mixing weekly and biweekly cycles, two "totals" are produced:
  - Total (active cycle): full pts of every active cycle this week.
    Shows 'what's on the plate this week'. Double-counts biweekly cycles
    if you sum across two consecutive weeks of the same cycle.
  - Per-week (proportional): cycle pts / cycle_weeks summed.
    Sums correctly across weeks but understates each individual week's
    nominal commitment.
"""
import argparse
import csv
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# Reuse helpers from the per-cycle script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from linear_cycle_report import (  # noqa: E402
    gql, resolve_team, fetch_cycles, fetch_all_team_issues,
    cycle_id_at_time, is_completed_at_time, is_canceled_at_time,
    assignee_name, points,
)


def fetch_view_issues(view_slug_or_id: str) -> list:
    """Fetch all issues from a Linear custom view (paginated)."""
    issues = []
    cursor = None
    while True:
        data = gql(
            """
            query($id: String!, $cursor: String) {
              customView(id: $id) {
                issues(first: 100, after: $cursor, includeArchived: true) {
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    identifier createdAt completedAt canceledAt
                    state { type }
                    assignee { id displayName name }
                  }
                }
              }
            }
            """,
            {"id": view_slug_or_id, "cursor": cursor},
        )
        page = data["customView"]["issues"]
        issues.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return issues


def bug_metrics_for_week(bugs: list, week_start_iso: str, week_end_iso: str):
    """Return {member: {open, resolved}}.
    open    = assignee count of bugs that exist at week_end and aren't done/canceled.
    resolved = bugs whose completedAt is within [week_start, week_end].
    Uses current assignee for attribution (doesn't reconstruct history)."""
    from collections import defaultdict
    out = defaultdict(lambda: {"open": 0, "resolved": 0})
    for b in bugs:
        a = b.get("assignee")
        if not a:
            continue
        who = a.get("displayName") or a.get("name")
        created = b.get("createdAt") or ""
        done = b.get("completedAt")
        canceled = b.get("canceledAt")
        if created > week_end_iso:
            continue  # didn't exist yet
        is_open_at_end = (
            (done is None or done > week_end_iso)
            and (canceled is None or canceled > week_end_iso)
        )
        if is_open_at_end:
            out[who]["open"] += 1
        if done and week_start_iso <= done <= week_end_iso:
            out[who]["resolved"] += 1
    return dict(out)

MYT_OFFSET = timedelta(hours=8)


def parse_iso_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def to_myt_date(s: str) -> date:
    """Linear's UTC ISO timestamp → MYT calendar date."""
    return (parse_iso_utc(s) + MYT_OFFSET).date()


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def per_member_stats(team_id, cycles):
    """For each cycle: { member: {planned_pts, completed_pts, planned, completed} }."""
    issues = fetch_all_team_issues(team_id)
    out = {}
    for num, cyc in cycles.items():
        cyc_id = cyc["id"]
        end = cyc["endsAt"]
        per = defaultdict(lambda: {"planned_pts": 0, "completed_pts": 0,
                                   "planned": 0, "completed": 0})
        for iss in issues:
            if cycle_id_at_time(iss, end) != cyc_id:
                continue
            if is_canceled_at_time(iss, end):
                continue
            if any(h["createdAt"] > end and h.get("toCycle")
                   and h["toCycle"]["id"] == cyc_id
                   for h in iss.get("history", {}).get("nodes", [])):
                continue
            est = points(iss.get("estimate"))
            done = is_completed_at_time(iss, end)
            who = assignee_name(iss)
            per[who]["planned"] += 1
            per[who]["planned_pts"] += est
            if done:
                per[who]["completed"] += 1
                per[who]["completed_pts"] += est
        out[num] = dict(per)
    return out


def week_label(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.strftime('%b %-d')}–{end.strftime('%-d')}"
    return f"{start.strftime('%b %-d')}–{end.strftime('%b %-d')}"


def cycle_overlapping_week(cycles_with_stats: dict, week_start: date, week_end: date):
    """Return list of (cyc_num, cyc, stats, cycle_weeks, week_idx) for cycles
    overlapping [week_start, week_end] inclusive (MYT)."""
    hits = []
    for num, cyc in cycles_with_stats["cycles"].items():
        cs = to_myt_date(cyc["startsAt"])
        ce = to_myt_date(cyc["endsAt"]) - timedelta(days=1)  # inclusive end
        if cs > week_end or ce < week_start:
            continue
        cyc_weeks = max(1, ((ce - cs).days + 1) // 7)
        # Which "week of the cycle" does this calendar week represent?
        days_in = (week_start - cs).days
        wk_idx = days_in // 7 + 1 if days_in >= 0 else 1
        hits.append((num, cyc, cycles_with_stats["stats"][num], cyc_weeks, wk_idx))
    return hits


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--teams", required=True,
                   help="Comma-separated team keys, e.g. ETL,CD")
    p.add_argument("--from", dest="from_date", required=True, metavar="YYYY-MM-DD",
                   help="Start date (any date in the first week to include)")
    p.add_argument("--to", dest="to_date", required=True, metavar="YYYY-MM-DD",
                   help="End date (any date in the last week to include)")
    p.add_argument("--member", help="Filter to one member (case-insensitive substring)")
    p.add_argument("--compare", help="Comma-separated members for side-by-side wide CSV")
    p.add_argument("--bug-view", action="append", default=[], metavar="LABEL:slugId",
                   help="Bug custom-view to include, formatted LABEL:slugId. Repeatable.")
    p.add_argument("--output", default=None, help="Output CSV path")
    return p.parse_args()


def main():
    args = parse_args()
    teams = [t.strip().upper() for t in args.teams.split(",")]

    teams_data = {}
    for tk in teams:
        team = resolve_team(tk)
        print(f"Loading {team['key']} ({team['name']})...")
        cycles = fetch_cycles(team["id"])
        stats = per_member_stats(team["id"], cycles)
        teams_data[tk] = {"team": team, "cycles": cycles, "stats": stats}

    bug_views = []  # list of (label, issues)
    for spec in args.bug_view:
        if ":" not in spec:
            sys.exit(f"--bug-view must be LABEL:slugId, got: {spec}")
        label, slug = spec.split(":", 1)
        print(f"Loading bug view {label} ({slug})...")
        bug_views.append((label.strip(), fetch_view_issues(slug.strip())))

    start = monday_of(date.fromisoformat(args.from_date))
    end = date.fromisoformat(args.to_date)
    weeks = []
    cur = start
    while cur <= end:
        weeks.append(cur)
        cur += timedelta(days=7)

    rows = []
    for w_start in weeks:
        w_end = w_start + timedelta(days=6)

        # Collect every member that has activity in any team's overlapping cycle this week
        members_this_week = set()
        team_cycle_map = {}  # team_key -> (num, cyc, stats, cycle_weeks, wk_idx) or None
        for tk in teams:
            hits = cycle_overlapping_week(teams_data[tk], w_start, w_end)
            # Expect 0 or 1 hit per team per week
            team_cycle_map[tk] = hits[0] if hits else None
            if hits:
                members_this_week.update(hits[0][2].keys())

        # Compute bug metrics per view for this week, indexed by member
        wk_start_iso = w_start.isoformat() + "T00:00:00Z"
        wk_end_iso = (w_end + timedelta(days=1)).isoformat() + "T00:00:00Z"
        bug_metrics_by_view = {}
        for label, issues in bug_views:
            m = bug_metrics_for_week(issues, wk_start_iso, wk_end_iso)
            bug_metrics_by_view[label] = m
            members_this_week.update(m.keys())

        if args.member:
            members_this_week = {m for m in members_this_week
                                 if args.member.lower() in m.lower()}

        for who in sorted(members_this_week):
            row = {
                "Week Start": w_start.isoformat(),
                "Week End": w_end.isoformat(),
                "Week": week_label(w_start, w_end),
                "Member": who,
            }
            active_planned = 0
            active_completed = 0
            perweek_planned = 0.0
            perweek_completed = 0.0
            any_planned = False

            for tk in teams:
                hit = team_cycle_map[tk]
                if not hit:
                    row[f"{tk} Cycle"] = ""
                    row[f"{tk} Cycle Week"] = ""
                    row[f"{tk} Planned"] = ""
                    row[f"{tk} Completed"] = ""
                    row[f"{tk} Success Rate"] = ""
                    continue
                num, cyc, stats, cyc_weeks, wk_idx = hit
                ms = stats.get(who, {"planned_pts": 0, "completed_pts": 0,
                                     "planned": 0, "completed": 0})
                pp = ms["planned_pts"]
                cp = ms["completed_pts"]
                rate = (cp / pp) if pp else None
                cyc_label = f"Cycle {num}"
                wk_label = f"{wk_idx}/{cyc_weeks}" if cyc_weeks > 1 else "—"
                row[f"{tk} Cycle"] = cyc_label
                row[f"{tk} Cycle Week"] = wk_label
                row[f"{tk} Planned"] = pp
                row[f"{tk} Completed"] = cp
                row[f"{tk} Success Rate"] = f"{rate*100:.0f}%" if rate is not None else ""
                active_planned += pp
                active_completed += cp
                perweek_planned += pp / cyc_weeks
                perweek_completed += cp / cyc_weeks
                if pp > 0:
                    any_planned = True

            # Bug columns: include even if member has no cycle commitments this week
            any_bug_activity = False
            for label, _ in bug_views:
                m = bug_metrics_by_view.get(label, {}).get(who, {"open": 0, "resolved": 0})
                row[f"{label} Bugs Open (eow)"] = m["open"]
                row[f"{label} Bugs Resolved (this wk)"] = m["resolved"]
                if m["open"] or m["resolved"]:
                    any_bug_activity = True

            if not any_planned and not any_bug_activity:
                continue  # skip if member has zero signal this week
            active_rate = (active_completed / active_planned) if active_planned else 0
            perweek_rate = (perweek_completed / perweek_planned) if perweek_planned else 0
            row["Total Planned (active cycles)"] = active_planned
            row["Total Completed (active cycles)"] = active_completed
            row["Total Success (active)"] = f"{active_rate*100:.0f}%" if active_planned else ""
            row["Per-Week Planned"] = round(perweek_planned, 1)
            row["Per-Week Completed"] = round(perweek_completed, 1)
            row["Per-Week Success"] = f"{perweek_rate*100:.0f}%" if perweek_planned else ""
            rows.append(row)

    if not rows:
        print("No rows produced.")
        return

    if args.compare:
        members_to_compare = [m.strip() for m in args.compare.split(",") if m.strip()]
        out = Path(args.output) if args.output else Path(
            f"./weekly_compare_{'_'.join(m.lower() for m in members_to_compare)}_"
            f"{args.from_date}_to_{args.to_date}.csv"
        )
        write_compare_csv(rows, members_to_compare, out, bug_views, teams)
        print(f"Wrote comparison CSV to {out}")
        return

    out = Path(args.output) if args.output else Path(
        f"./weekly_view_{'_'.join(t.lower() for t in teams)}_"
        f"{args.from_date}_to_{args.to_date}.csv"
    )
    fieldnames = list(rows[0].keys())
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out}")


def write_compare_csv(rows: list, members: list, out_path: Path,
                      bug_views: list, teams: list):
    """Pivot long-format rows into a side-by-side wide CSV, one row per week."""
    by_wk_member = {(r["Week Start"], r["Member"]): r for r in rows}

    # Use a stable week order from input rows
    weeks_seen = []
    seen = set()
    for r in rows:
        if r["Week Start"] not in seen:
            seen.add(r["Week Start"])
            weeks_seen.append((r["Week Start"], r["Week"]))

    out_rows = []
    for w_start, w_label in weeks_seen:
        row = {"Week Start": w_start, "Week": w_label}
        for m in members:
            r = by_wk_member.get((w_start, m), {})
            for tk in teams:
                p = r.get(f"{tk} Planned")
                c = r.get(f"{tk} Completed")
                row[f"{m} {tk} P/C"] = f"{p}/{c}" if p not in (None, "", 0) else ""
            row[f"{m} Per-Week Planned"] = r.get("Per-Week Planned", 0) or 0
            row[f"{m} Per-Week Completed"] = r.get("Per-Week Completed", 0) or 0
            row[f"{m} Success"] = r.get("Per-Week Success", "")
            # Sum bug counts across all configured bug views
            open_b = 0
            res_b = 0
            for label, _ in bug_views:
                open_b += int(r.get(f"{label} Bugs Open (eow)", 0) or 0)
                res_b += int(r.get(f"{label} Bugs Resolved (this wk)", 0) or 0)
            row[f"{m} Bugs Open"] = open_b
            row[f"{m} Bugs Resolved"] = res_b
        out_rows.append(row)

    fieldnames = list(out_rows[0].keys()) if out_rows else []
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)


if __name__ == "__main__":
    main()
