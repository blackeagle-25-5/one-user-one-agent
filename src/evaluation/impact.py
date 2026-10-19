"""
Impact reporter: compares agent arm vs holdout arm using intention-to-treat (ITT).

ITT = compare ALL users in the agent arm against ALL users in the holdout arm,
regardless of whether the agent actually took an action.

This is the correct approach because:
- "Served vs not served" is biased: the agent serves users who are already more active
  (they triggered the agent by opening the app), so they would have done better anyway.
- The pilot report made exactly this mistake: served (217) vs "everyone else" (3,674).
- Correct comparison: all treatment arm (1,912) vs all holdout arm (1,979).

The reporter also re-computes the pilot uplift using randomized arms.
"""

import csv
import json
import logging
import math
import os
from collections import defaultdict
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


def compute_impact(
    outcomes_path: str,
    arms_path: str,
    goals: list,
    pilot_assignment_path: str,
    pilot_outcomes_path: str,
    out_report_path: str,
) -> dict:
    """
    Compute ITT impact for replay outcomes and re-analyse the pilot.
    Writes impact_report.md and returns a summary dict.
    """
    # ------------------------------------------------------------------ #
    # Load replay arms and outcomes                                        #
    # ------------------------------------------------------------------ #
    arms: Dict[str, str] = {}
    if os.path.exists(arms_path):
        with open(arms_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                arms[row["user_id"]] = row["arm"]

    outcomes: List[dict] = []
    if os.path.exists(outcomes_path):
        with open(outcomes_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    outcomes.append(json.loads(line))

    goals_map = {g["goal_id"]: g for g in goals}

    # ------------------------------------------------------------------ #
    # ITT analysis                                                         #
    # ------------------------------------------------------------------ #
    # All triggered users (agent arm) — from outcomes, which only covers triggered users
    agent_users = {uid for uid, arm in arms.items() if arm == "agent"}
    holdout_users = {uid for uid, arm in arms.items() if arm == "holdout"}

    # Build success map: user_id -> set of goal_ids that succeeded
    success_by_user: Dict[str, set] = defaultdict(set)
    for o in outcomes:
        if o.get("outcome") == "succeeded":
            success_by_user[o["user_id"]].add(o["goal_id"])

    # Triggered users per arm (users who appeared in replay triggers)
    triggered_in_outcomes = {o["user_id"] for o in outcomes}
    agent_triggered = triggered_in_outcomes & agent_users
    holdout_triggered = triggered_in_outcomes & holdout_users

    # ITT: only triggered users (since we only have outcomes for triggered users)
    # Note: for a full ITT, we'd need outcomes for ALL arm members including those
    # who never opened the app. We document this limitation.
    agent_successes = sum(1 for u in agent_triggered if success_by_user[u])
    holdout_successes = sum(1 for u in holdout_triggered if success_by_user[u])

    n_agent = len(agent_triggered)
    n_holdout = len(holdout_triggered)

    agent_rate = agent_successes / n_agent if n_agent > 0 else 0
    holdout_rate = holdout_successes / n_holdout if n_holdout > 0 else 0
    uplift_pct = (agent_rate - holdout_rate) * 100

    # 95% confidence interval using Wilson score interval difference
    agent_ci = _wilson_ci(agent_successes, n_agent)
    holdout_ci = _wilson_ci(holdout_successes, n_holdout)
    ci_lower = uplift_pct - 1.96 * _diff_se(agent_rate, n_agent, holdout_rate, n_holdout) * 100
    ci_upper = uplift_pct + 1.96 * _diff_se(agent_rate, n_agent, holdout_rate, n_holdout) * 100

    # Per-goal breakdown
    goal_breakdown = []
    all_goal_ids = set(o["goal_id"] for o in outcomes)
    for gid in sorted(all_goal_ids):
        gname = goals_map.get(gid, {}).get("name", gid)
        a_users = [u for u in agent_triggered if any(o["goal_id"] == gid and o["user_id"] == u for o in outcomes)]
        h_users = [u for u in holdout_triggered if any(o["goal_id"] == gid and o["user_id"] == u for o in outcomes)]
        a_suc = sum(1 for u in a_users if gid in success_by_user[u])
        h_suc = sum(1 for u in h_users if gid in success_by_user[u])
        a_rate = a_suc / len(a_users) if a_users else 0
        h_rate = h_suc / len(h_users) if h_users else 0
        goal_breakdown.append({
            "goal_id": gid, "name": gname,
            "agent_n": len(a_users), "agent_successes": a_suc, "agent_rate": round(a_rate, 3),
            "holdout_n": len(h_users), "holdout_successes": h_suc, "holdout_rate": round(h_rate, 3),
            "uplift_pct": round((a_rate - h_rate) * 100, 1),
        })

    # ------------------------------------------------------------------ #
    # Re-analyse pilot report                                              #
    # ------------------------------------------------------------------ #
    pilot_analysis = _reanalyse_pilot(pilot_assignment_path, pilot_outcomes_path)

    # ------------------------------------------------------------------ #
    # Write report                                                         #
    # ------------------------------------------------------------------ #
    report = {
        "replay_itt": {
            "agent_triggered_users": n_agent,
            "holdout_triggered_users": n_holdout,
            "agent_successes": agent_successes,
            "holdout_successes": holdout_successes,
            "agent_success_rate": round(agent_rate, 4),
            "holdout_success_rate": round(holdout_rate, 4),
            "uplift_pct": round(uplift_pct, 2),
            "ci_95_lower": round(ci_lower, 2),
            "ci_95_upper": round(ci_upper, 2),
            "goal_breakdown": goal_breakdown,
        },
        "pilot_reanalysis": pilot_analysis,
    }

    _write_markdown_report(report, out_report_path)
    return report


def _reanalyse_pilot(assignment_path: str, outcomes_path: str) -> dict:
    """Re-compute pilot uplift using randomized arms (correct ITT)."""
    if not os.path.exists(assignment_path) or not os.path.exists(outcomes_path):
        return {"error": "pilot data files not found"}

    assignment: Dict[str, dict] = {}
    with open(assignment_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            assignment[row["user_id"]] = row

    outcomes: List[dict] = []
    with open(outcomes_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            outcomes.append(row)

    treatment_users = {uid for uid, r in assignment.items() if r["arm"] == "treatment"}
    holdout_users = {uid for uid, r in assignment.items() if r["arm"] == "holdout"}
    served_users = {uid for uid, r in assignment.items() if r["served"] == "true"}

    success_map = {o["user_id"]: o["succeeded"] == "true" for o in outcomes}

    # Correct ITT: all treatment vs all holdout
    t_suc = sum(1 for u in treatment_users if success_map.get(u, False))
    h_suc = sum(1 for u in holdout_users if success_map.get(u, False))
    t_rate = t_suc / len(treatment_users) if treatment_users else 0
    h_rate = h_suc / len(holdout_users) if holdout_users else 0

    # Biased comparison (as done in the internal report): served vs everyone else
    not_served = {uid for uid in assignment if uid not in served_users}
    s_suc = sum(1 for u in served_users if success_map.get(u, False))
    ns_suc = sum(1 for u in not_served if success_map.get(u, False))
    s_rate = s_suc / len(served_users) if served_users else 0
    ns_rate = ns_suc / len(not_served) if not_served else 0

    return {
        "correct_itt": {
            "treatment_n": len(treatment_users),
            "holdout_n": len(holdout_users),
            "treatment_success_rate": round(t_rate, 4),
            "holdout_success_rate": round(h_rate, 4),
            "uplift_pct": round((t_rate - h_rate) * 100, 2),
            "ci_95": f"[{round((t_rate - h_rate - 1.96 * _diff_se(t_rate, len(treatment_users), h_rate, len(holdout_users))) * 100, 1)}, "
                     f"{round((t_rate - h_rate + 1.96 * _diff_se(t_rate, len(treatment_users), h_rate, len(holdout_users))) * 100, 1)}]",
        },
        "biased_served_vs_others": {
            "served_n": len(served_users),
            "others_n": len(not_served),
            "served_success_rate": round(s_rate, 4),
            "others_success_rate": round(ns_rate, 4),
            "uplift_pct": round((s_rate - ns_rate) * 100, 2),
            "why_biased": (
                "The agent serves only users who triggered it by opening the app. "
                "These users are already more active and more likely to convert — "
                "so comparing them to everyone else (including inactive users who never opened the app) "
                "inflates the apparent uplift. This is selection bias, not a treatment effect."
            ),
        },
    }


def _wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score confidence interval for a proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z ** 2 / n
    centre = (p + z ** 2 / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z ** 2 / (4 * n ** 2)) / denom
    return (max(0, centre - spread), min(1, centre + spread))


def _diff_se(p1: float, n1: int, p2: float, n2: int) -> float:
    """Standard error of the difference between two proportions."""
    v1 = p1 * (1 - p1) / n1 if n1 > 0 else 0
    v2 = p2 * (1 - p2) / n2 if n2 > 0 else 0
    return math.sqrt(v1 + v2)


def _write_markdown_report(report: dict, path: str):
    """Write a human-readable markdown impact report."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    r = report["replay_itt"]
    p = report.get("pilot_reanalysis", {})

    with open(path, "w", encoding="utf-8") as f:
        f.write("# Impact Report\n\n")
        f.write("## Replay Day: Intention-to-Treat Analysis\n\n")
        f.write("> **Method:** All agent-arm triggered users vs all holdout-arm triggered users.\n")
        f.write("> This is the correct comparison because it avoids selection bias.\n\n")

        f.write(f"| Group | Users | Successes | Success Rate |\n")
        f.write(f"|-------|-------|-----------|-------------|\n")
        f.write(f"| Agent arm | {r['agent_triggered_users']} | {r['agent_successes']} | {r['agent_success_rate']*100:.1f}% |\n")
        f.write(f"| Holdout arm | {r['holdout_triggered_users']} | {r['holdout_successes']} | {r['holdout_success_rate']*100:.1f}% |\n\n")

        f.write(f"**Uplift:** {r['uplift_pct']:+.1f} percentage points ")
        f.write(f"(95% CI: [{r['ci_95_lower']:+.1f}, {r['ci_95_upper']:+.1f}] pp)\n\n")

        if abs(r['uplift_pct']) < 1.0:
            f.write("> ⚠️ Small or zero uplift. This is an honest result. The agent may need more ")
            f.write("tuning, or the effect may emerge over more days of data.\n\n")

        # Goal breakdown
        if r.get("goal_breakdown"):
            f.write("### Per-Goal Breakdown\n\n")
            f.write("| Goal | Agent N | Agent Rate | Holdout N | Holdout Rate | Uplift |\n")
            f.write("|------|---------|------------|-----------|--------------|--------|\n")
            for g in r["goal_breakdown"]:
                f.write(f"| {g['goal_id']} {g['name']} | {g['agent_n']} | {g['agent_rate']*100:.1f}% | "
                        f"{g['holdout_n']} | {g['holdout_rate']*100:.1f}% | {g['uplift_pct']:+.1f}pp |\n")
            f.write("\n")

        # Pilot re-analysis
        if p and "correct_itt" in p:
            f.write("## Pilot Re-Analysis: Why the Internal Report Was Wrong\n\n")
            f.write("The internal pilot report headline said:\n\n")
            f.write("> *Served by the agent: 40.1% | All other users: 17.7% → 2.3× uplift*\n\n")
            f.write("This comparison is **biased (selection bias)**: the agent only served users ")
            f.write("who opened the app (already more active). Comparing them to everyone else — ")
            f.write("including users who never opened the app — inflates the apparent uplift.\n\n")

            c = p["correct_itt"]
            b = p["biased_served_vs_others"]
            f.write("### Correct ITT (all treatment vs all holdout)\n\n")
            f.write(f"| Group | N | Success Rate |\n")
            f.write(f"|-------|---|-------------|\n")
            f.write(f"| Treatment arm | {c['treatment_n']} | {c['treatment_success_rate']*100:.1f}% |\n")
            f.write(f"| Holdout arm | {c['holdout_n']} | {c['holdout_success_rate']*100:.1f}% |\n\n")
            f.write(f"**Correct uplift:** {c['uplift_pct']:+.1f} pp (95% CI: {c['ci_95']})\n\n")

            f.write("### Biased Comparison (as in the internal report)\n\n")
            f.write(f"| Group | N | Success Rate |\n")
            f.write(f"|-------|---|-------------|\n")
            f.write(f"| Served users | {b['served_n']} | {b['served_success_rate']*100:.1f}% |\n")
            f.write(f"| All other users | {b['others_n']} | {b['others_success_rate']*100:.1f}% |\n\n")
            f.write(f"**Biased uplift:** {b['uplift_pct']:+.1f} pp\n\n")
            f.write(f"*Why biased:* {b['why_biased']}\n\n")

        f.write("## Limitations\n\n")
        f.write("- This replay has only ~2,769 triggers; small samples mean wide confidence intervals.\n")
        f.write("- Outcome attribution is limited to events generated by the simulator (14-day window).\n")
        f.write("- True ITT would require outcome data for ALL arm members, including those who never triggered.\n")
        f.write("- G11 (tv_activation) outcomes may be incomplete as tv_app_open is rarely generated.\n")

    log.info("Wrote impact report to %s", path)
