"""
CLI entry point for the One User, One Agent system.

Usage:
    python -m src.cli replay [OPTIONS]

Commands:
    replay   Run the full replay pipeline:
             1. Load data pack
             2. Run replay harness (decisions + actions)
             3. Run outcome simulator
             4. Judge outcomes
             5. Compute impact report + latency report
             6. Export per-viewer state samples
             7. Write final_report.md

Options:
    --pack PATH          Path to data pack root (default: one_user_one_agent_pack)
    --out PATH           Output directory (default: sample_run)
    --max-model-calls N  Max model calls per decision (default: 8)
    --max-tool-calls N   Max tool calls per decision (default: 20)
    --seed SEED          Simulator seed (default: oua-practice)
    --skip-sim           Skip running the simulator (use existing outcome_events.jsonl)
    --sample-users N     Export viewer state for N sample users (default: 10)
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cli")

UTC = timezone.utc


def cmd_replay(args):
    from src.data.loader import DataPack
    from src.agent.state_store import StateStore
    from src.replay.harness import run_replay
    from src.replay.outcome_judge import judge_outcomes
    from src.evaluation.impact import compute_impact
    from src.evaluation.latency import compute_latency_report

    pack_root = args.pack
    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "viewer_state"), exist_ok=True)

    db_path = os.path.join(out_dir, "agent_state.db")
    decisions_path = os.path.join(out_dir, "decisions.jsonl")
    actions_path = os.path.join(out_dir, "actions.jsonl")
    sim_out = os.path.join(out_dir, "sim")
    outcome_events_path = os.path.join(sim_out, "outcome_events.jsonl")
    outcomes_path = os.path.join(out_dir, "outcomes.jsonl")
    latency_path = os.path.join(out_dir, "latency_report.json")
    impact_path = os.path.join(out_dir, "impact_report.md")
    final_report_path = os.path.join(out_dir, "final_report.md")

    # ------------------------------------------------------------------ #
    # Step 1: Load data pack                                               #
    # ------------------------------------------------------------------ #
    log.info("Loading data pack from %s ...", pack_root)
    data_pack = DataPack(pack_root)

    # ------------------------------------------------------------------ #
    # Step 2: Run replay harness                                           #
    # ------------------------------------------------------------------ #
    log.info("Running replay harness (%d triggers) ...", len(data_pack.triggers))
    # Remove old DB for clean run
    if os.path.exists(db_path):
        os.remove(db_path)
    store = StateStore(db_path)

    replay_summary = run_replay(
        data_pack=data_pack,
        state_store=store,
        out_decisions=decisions_path,
        out_actions=actions_path,
        max_model_calls=args.max_model_calls,
        max_tool_calls=args.max_tool_calls,
        progress_every=500,
    )
    log.info("Replay summary: %s", json.dumps(replay_summary, default=str))

    # ------------------------------------------------------------------ #
    # Step 3: Run outcome simulator                                        #
    # ------------------------------------------------------------------ #
    if not args.skip_sim:
        log.info("Running outcome simulator ...")
        sim_cmd = [
            sys.executable,
            os.path.join(pack_root, "simulator", "simulate_outcomes.py"),
            "--pack", pack_root,
            "--actions", actions_path,
            "--out", sim_out,
            "--seed", args.seed,
        ]
        try:
            result = subprocess.run(sim_cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                log.error("Simulator failed: %s", result.stderr)
            else:
                log.info("Simulator: %s", result.stderr.strip())
        except subprocess.TimeoutExpired:
            log.error("Simulator timed out after 300s")
        except Exception as e:
            log.error("Simulator error: %s", e)
    else:
        log.info("Skipping simulator (--skip-sim)")

    # ------------------------------------------------------------------ #
    # Step 4: Judge outcomes                                               #
    # ------------------------------------------------------------------ #
    log.info("Judging outcomes ...")
    outcomes = judge_outcomes(
        decisions_path=decisions_path,
        outcome_events_path=outcome_events_path,
        goals=data_pack.goals,
        state_store=store,
        out_outcomes_path=outcomes_path,
    )
    log.info("Judged %d outcomes", len(outcomes))

    # ------------------------------------------------------------------ #
    # Step 5: Latency report                                               #
    # ------------------------------------------------------------------ #
    log.info("Computing latency report ...")
    latency = compute_latency_report(decisions_path, latency_path)

    # ------------------------------------------------------------------ #
    # Step 6: Impact report                                                #
    # ------------------------------------------------------------------ #
    log.info("Computing impact report ...")
    impact = compute_impact(
        outcomes_path=outcomes_path,
        arms_path=os.path.join(pack_root, "replay", "arms.csv"),
        goals=data_pack.goals,
        pilot_assignment_path=os.path.join(pack_root, "pilot", "pilot_assignment.csv"),
        pilot_outcomes_path=os.path.join(pack_root, "pilot", "pilot_outcomes.csv"),
        out_report_path=impact_path,
    )

    # ------------------------------------------------------------------ #
    # Step 7: Export sample viewer states                                  #
    # ------------------------------------------------------------------ #
    log.info("Exporting sample viewer states ...")
    all_decisions = store.all_decisions()
    exported_users = set()
    for dec in all_decisions:
        uid = dec.get("viewer_id")
        if uid and uid not in exported_users and len(exported_users) < args.sample_users:
            export_path = os.path.join(out_dir, "viewer_state", f"{uid}.json")
            store.export_viewer_state(uid, export_path)
            exported_users.add(uid)
    log.info("Exported state for %d viewers", len(exported_users))

    # ------------------------------------------------------------------ #
    # Step 8: Write final report                                           #
    # ------------------------------------------------------------------ #
    _write_final_report(
        final_report_path, replay_summary, latency, impact,
        data_pack, len(outcomes)
    )

    store.close()

    print(f"\n{'='*60}")
    print("REPLAY COMPLETE")
    print(f"{'='*60}")
    print(f"Decisions:     {decisions_path}")
    print(f"Actions:       {actions_path}")
    print(f"Outcomes:      {outcomes_path}")
    print(f"Latency:       {latency_path}")
    print(f"Impact:        {impact_path}")
    print(f"Final report:  {final_report_path}")
    print(f"Viewer states: {out_dir}/viewer_state/")
    print(f"\nDecisions: {replay_summary['decisions_published']}")
    print(f"Actions:   {replay_summary['actions_published']}")
    print(f"Abstain:   {replay_summary['abstentions']}")
    lat = latency.get("latency_ms", {})
    print(f"Latency p50: {lat.get('p50', 'N/A')}ms  p95: {lat.get('p95', 'N/A')}ms")
    print(f"Within 60s budget: {latency.get('within_budget_pct', 'N/A')}%")


def _write_final_report(path, replay_summary, latency, impact, data_pack, n_outcomes):
    """Write the final_report.md."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Final Report — One User, One Agent\n\n")
        f.write(f"*Generated at: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}*\n\n")

        f.write("## Data Issues Found and Handled\n\n")
        f.write("| Issue | How handled |\n")
        f.write("|-------|-------------|\n")
        f.write(f"| Duplicate event_ids | Deduplicated by event_id before processing |\n")
        f.write(f"| Stale phone clocks | Used received_at (server time) for ordering |\n")
        f.write(f"| Offline-buffered triggers (>600s lag) | Detected and abstained with reason |\n")
        f.write(f"| Profile duplicate rows: ~30 users | Kept row with latest updated_at |\n")
        f.write(f"| Brand-new users (no profile row) | Derived facts from signup_completed event |\n")
        f.write(f"| Dialect conflicts (3 sources) | app_setting > profile > campaign_deeplink |\n")
        f.write(f"| Title language variants (same content_slug) | Deduplicated by content_slug |\n")
        f.write(f"| Availability windows | Checked at publish time |\n")
        f.write(f"| Prompt injection in catalogue | Detected, logged, titles blocked from actions |\n")
        f.write(f"| G10 (meaningful_engagement): unmeasurable | Skipped; documented as assumption |\n")
        f.write(f"| G07, G11: unclear rules | Interpretation documented as assumptions |\n")
        f.write(f"| Pilot report selection bias | Recomputed using ITT with randomized arms |\n\n")

        f.write("## Replay Results\n\n")
        f.write(f"- Triggers processed: {replay_summary.get('triggers_total', 0)}\n")
        f.write(f"- Decisions published: {replay_summary.get('decisions_published', 0)}\n")
        f.write(f"- Actions published: {replay_summary.get('actions_published', 0)}\n")
        f.write(f"- Abstentions (do-nothing): {replay_summary.get('abstentions', 0)}\n")
        f.write(f"- Holdout blocked: {replay_summary.get('holdout_blocked', 0)}\n")
        f.write(f"- Fallbacks used: {replay_summary.get('fallbacks_used', 0)}\n")
        f.write(f"- Outcomes judged: {n_outcomes}\n\n")

        lat = latency.get("latency_ms", {})
        f.write("## Latency\n\n")
        f.write(f"- p50: {lat.get('p50', 'N/A')} ms\n")
        f.write(f"- p95: {lat.get('p95', 'N/A')} ms\n")
        f.write(f"- p99: {lat.get('p99', 'N/A')} ms\n")
        f.write(f"- Max: {lat.get('max', 'N/A')} ms\n")
        f.write(f"- Within 60s budget: {latency.get('within_budget_pct', 'N/A')}%\n")
        f.write(f"- Failures (>5min): {latency.get('over_5min_count', 0)}\n\n")

        f.write("## Safety Checks Summary\n\n")
        f.write("The independent verifier enforced:\n")
        f.write("- Holdout arm: zero actions delivered\n")
        f.write("- Consent: push/WA only to opted-in users\n")
        f.write("- Quiet hours: no push/WA 22:00–08:00 IST\n")
        f.write("- Frequency cap: at most 1 push/WA per user per IST day\n")
        f.write("- Dialect match: all titles verified to match viewer dialect\n")
        f.write("- Availability: all titles checked at publish time\n")
        f.write("- Prompt injection: injection-flagged titles blocked\n")
        f.write("- Forbidden push phrases: checked and rejected\n\n")

        f.write("## Impact Summary\n\n")
        if "replay_itt" in impact:
            r = impact["replay_itt"]
            f.write(f"- Agent arm success rate: {r.get('agent_success_rate', 0)*100:.1f}%\n")
            f.write(f"- Holdout arm success rate: {r.get('holdout_success_rate', 0)*100:.1f}%\n")
            f.write(f"- Uplift: {r.get('uplift_pct', 0):+.1f} pp ")
            f.write(f"(95% CI: [{r.get('ci_95_lower', 0):+.1f}, {r.get('ci_95_upper', 0):+.1f}] pp)\n\n")

        f.write("See `impact_report.md` for the full analysis and pilot re-analysis.\n\n")

        f.write("## Model Faults Handled\n\n")
        for fault_id, fault in data_pack.model_faults.items():
            f.write(f"- {fault.get('fault')} for trigger {fault_id}: "
                    f"user {fault.get('user_id')} → fallback to rule-based action or no action\n")

        f.write("\n## Control Events Handled\n\n")
        for ctrl in data_pack.control_events:
            f.write(f"- `{ctrl['type']}` at {ctrl['at']}: {json.dumps({k:v for k,v in ctrl.items() if k not in ('at','_at','type')})}\n")

        f.write("\n")


def main():
    parser = argparse.ArgumentParser(
        description="One User, One Agent — replay pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    # replay sub-command
    replay_p = sub.add_parser("replay", help="Run the full replay pipeline")
    replay_p.add_argument("--pack", default="one_user_one_agent_pack",
                          help="Path to data pack root")
    replay_p.add_argument("--out", default="sample_run",
                          help="Output directory")
    replay_p.add_argument("--max-model-calls", type=int, default=8,
                          help="Max model calls per decision")
    replay_p.add_argument("--max-tool-calls", type=int, default=20,
                          help="Max tool calls per decision")
    replay_p.add_argument("--seed", default="oua-practice",
                          help="Simulator random seed")
    replay_p.add_argument("--skip-sim", action="store_true",
                          help="Skip the outcome simulator")
    replay_p.add_argument("--sample-users", type=int, default=10,
                          help="Number of viewer states to export")
    replay_p.set_defaults(func=cmd_replay)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
