#!/usr/bin/env python3
"""
Generate per-member cycle success-rate CSV from Linear.

For each cycle X with date range [start, end]:
  - "Planned" = issues whose cycleId == X at end (reconstructed from issue history,
    so issues that were committed to X but moved to next cycle are still counted).
  - "Completed" = subset that completed during the cycle.
  - Unestimated issues count as 1 point (team convention).

Usage:
  LINEAR_API_KEY=lin_api_... \\
    python3 scripts/linear_cycle_report.py --team ETL --from 33 --to 38

Outputs CSV to ./<team>_cycle_<from>-<to>_success_rate.csv unless --output is given.
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib import request as urlreq
from urllib import error as urlerror

ENDPOINT = "https://api.linear.app/graphql"


def gql(query: str, variables: dict | None = None) -> dict:
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        sys.exit("ERROR: LINEAR_API_KEY env var required")

    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urlreq.Request(
        ENDPOINT,
        data=payload,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(5):
        try:
            with urlreq.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
                if "errors" in body:
                    raise RuntimeError(f"GraphQL errors: {body['errors']}")
                return body["data"]
        except urlerror.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {e.code}: {err_body}") from e
    raise RuntimeError("retries exhausted")


def resolve_team(key_or_name: str) -> dict:
    """Find team by key (e.g. 'ETL') or by name."""
    data = gql(
        """
        query($key: String!) {
          teams(filter: { key: { eq: $key } }, first: 1) {
            nodes { id name key }
          }
        }
        """,
        {"key": key_or_name.upper()},
    )
    nodes = data["teams"]["nodes"]
    if nodes:
        return nodes[0]
    # Fall back to name search
    data = gql(
        """
        query($name: String!) {
          teams(filter: { name: { containsIgnoreCase: $name } }, first: 5) {
            nodes { id name key }
          }
        }
        """,
        {"name": key_or_name},
    )
    nodes = data["teams"]["nodes"]
    if not nodes:
        sys.exit(f"ERROR: no team found for '{key_or_name}'")
    if len(nodes) > 1:
        names = ", ".join(f"{n['key']} ({n['name']})" for n in nodes)
        sys.exit(f"ERROR: ambiguous team '{key_or_name}'. Matches: {names}")
    return nodes[0]


def fetch_cycles(team_id: str) -> dict:
    data = gql(
        """
        query($teamId: String!) {
          team(id: $teamId) {
            cycles(first: 100) {
              nodes { id number startsAt endsAt
                      completedScopeHistory scopeHistory }
            }
          }
        }
        """,
        {"teamId": team_id},
    )
    return {c["number"]: c for c in data["team"]["cycles"]["nodes"]}


def fetch_all_team_issues(team_id: str) -> list:
    issues = []
    cursor = None
    while True:
        data = gql(
            """
            query($teamId: String!, $cursor: String) {
              team(id: $teamId) {
                issues(first: 25, after: $cursor, includeArchived: true) {
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    id identifier estimate completedAt canceledAt createdAt
                    state { type name }
                    assignee { id displayName name }
                    cycle { id number }
                    history(first: 50) {
                      nodes {
                        id createdAt
                        fromCycle { id number }
                        toCycle { id number }
                      }
                    }
                  }
                }
              }
            }
            """,
            {"teamId": team_id, "cursor": cursor},
        )
        page = data["team"]["issues"]
        issues.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return issues


def cycle_id_at_time(issue: dict, t_iso: str) -> str | None:
    """Reconstruct cycleId at instant `t_iso` by undoing events from `now`."""
    events = sorted(
        [h for h in issue.get("history", {}).get("nodes", [])
         if h.get("fromCycle") or h.get("toCycle")],
        key=lambda h: h["createdAt"],
    )

    if not (issue.get("createdAt", "") <= t_iso):
        return None

    cur = issue.get("cycle")
    cur_id = cur["id"] if cur else None
    for h in reversed(events):
        if h["createdAt"] <= t_iso:
            break
        fc = h.get("fromCycle")
        cur_id = fc["id"] if fc else None
    return cur_id


def is_completed_at_time(issue: dict, t_iso: str) -> bool:
    ca = issue.get("completedAt")
    return ca is not None and ca <= t_iso


def is_canceled_at_time(issue: dict, t_iso: str) -> bool:
    cx = issue.get("canceledAt")
    return cx is not None and cx <= t_iso


def assignee_name(issue: dict) -> str:
    a = issue.get("assignee")
    if not a:
        return "Unassigned"
    return a.get("displayName") or a.get("name") or "Unknown"


def points(estimate):
    """Treat unestimated issues as 1 point (team convention)."""
    return estimate if estimate is not None else 1


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--team", required=True,
                   help="Team key (e.g. ETL, CD) or name")
    p.add_argument("--from", dest="from_cycle", type=int, required=True,
                   metavar="N", help="First cycle number (inclusive)")
    p.add_argument("--to", dest="to_cycle", type=int, required=True,
                   metavar="N", help="Last cycle number (inclusive)")
    p.add_argument("--output", default=None,
                   help="Output CSV path (default: ./<team>_cycle_<from>-<to>_success_rate.csv)")
    args = p.parse_args()
    if args.from_cycle > args.to_cycle:
        sys.exit("ERROR: --from must be <= --to")
    return args


def main():
    args = parse_args()

    team = resolve_team(args.team)
    print(f"Team: {team['key']} ({team['name']})  id={team['id']}")

    print("Fetching cycles...")
    cycles_by_num = fetch_cycles(team["id"])
    print(f"  {len(cycles_by_num)} cycles found")

    cycle_range = list(range(args.from_cycle, args.to_cycle + 1))
    missing = [n for n in cycle_range if n not in cycles_by_num]
    if missing:
        print(f"  WARNING: cycles not found for team {team['key']}: {missing}")

    print("Fetching all team issues + history...")
    issues = fetch_all_team_issues(team["id"])
    print(f"  {len(issues)} issues")

    rows = []
    debug_lines = []

    for cyc_num in cycle_range:
        cyc = cycles_by_num.get(cyc_num)
        if not cyc:
            continue
        cyc_id = cyc["id"]
        end = cyc["endsAt"]

        in_cycle = []
        for iss in issues:
            cid = cycle_id_at_time(iss, end)
            if cid != cyc_id:
                continue
            if is_canceled_at_time(iss, end):
                continue
            # Skip issues that were retroactively moved back to this cycle
            # after it closed — Linear doesn't treat those as commitments
            # (they're typically reporting reclassifications).
            moved_back = any(
                h["createdAt"] > end
                and h.get("toCycle")
                and h["toCycle"]["id"] == cyc_id
                for h in iss.get("history", {}).get("nodes", [])
            )
            if moved_back:
                continue
            in_cycle.append(iss)

        per_member = defaultdict(lambda: {"planned": 0, "completed": 0,
                                          "planned_pts": 0, "completed_pts": 0})
        team_planned_pts = 0
        team_completed_pts = 0

        for iss in in_cycle:
            est = points(iss.get("estimate"))
            done = is_completed_at_time(iss, end)
            who = assignee_name(iss)

            per_member[who]["planned"] += 1
            per_member[who]["planned_pts"] += est
            team_planned_pts += est

            if done:
                per_member[who]["completed"] += 1
                per_member[who]["completed_pts"] += est
                team_completed_pts += est

        team_success = (team_completed_pts / team_planned_pts) if team_planned_pts else 0.0

        sh = cyc.get("scopeHistory") or []
        csh = cyc.get("completedScopeHistory") or []
        debug_lines.append(
            f"Cycle {cyc_num}: my(scope={team_planned_pts}, done={team_completed_pts}, "
            f"succ={team_success:.0%}) vs api(scope={sh[-1] if sh else '-'}, "
            f"done={csh[-1] if csh else '-'})"
        )

        for who in sorted(per_member, key=lambda k: -per_member[k]["planned_pts"]):
            m = per_member[who]
            pt_rate = (m["completed_pts"] / m["planned_pts"]) if m["planned_pts"] else 0.0
            incomp_pts = m["planned_pts"] - m["completed_pts"]
            iv = (incomp_pts / team_planned_pts) if team_planned_pts else 0.0
            rows.append({
                "Cycle": f"Cycle {cyc_num}",
                "Member": who,
                "Planned": m["planned_pts"],
                "Completed": m["completed_pts"],
                "Individual Success Rate": f"{pt_rate*100:.0f}%",
                "Team Success Rate": f"{team_success*100:.0f}%",
                "Incomplete vs Team Effort Committed": f"{incomp_pts}/{team_planned_pts} = {iv*100:.0f}%",
                "Issues (planned/done)": f"{m['planned']}/{m['completed']}",
            })

    if args.output:
        out = Path(args.output)
    else:
        out = Path(f"./{team['key'].lower()}_cycle_{args.from_cycle}-{args.to_cycle}_success_rate.csv")

    fieldnames = ["Cycle", "Member", "Planned", "Completed",
                  "Individual Success Rate", "Team Success Rate",
                  "Incomplete vs Team Effort Committed", "Issues (planned/done)"]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"\nWrote {len(rows)} rows to {out}")
    print("\n--- Sanity check vs Linear API scopeHistory ---")
    for line in debug_lines:
        print(line)


if __name__ == "__main__":
    main()
