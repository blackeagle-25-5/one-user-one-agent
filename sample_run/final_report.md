# Final Report — One User, One Agent

*Generated at: 2026-10-19T06:40:45Z*

## Data Issues Found and Handled

| Issue | How handled |
|-------|-------------|
| Duplicate event_ids | Deduplicated by event_id before processing |
| Stale phone clocks | Used received_at (server time) for ordering |
| Offline-buffered triggers (>600s lag) | Detected and abstained with reason |
| Profile duplicate rows: ~30 users | Kept row with latest updated_at |
| Brand-new users (no profile row) | Derived facts from signup_completed event |
| Dialect conflicts (3 sources) | app_setting > profile > campaign_deeplink |
| Title language variants (same content_slug) | Deduplicated by content_slug |
| Availability windows | Checked at publish time |
| Prompt injection in catalogue | Detected, logged, titles blocked from actions |
| G10 (meaningful_engagement): unmeasurable | Skipped; documented as assumption |
| G07, G11: unclear rules | Interpretation documented as assumptions |
| Pilot report selection bias | Recomputed using ITT with randomized arms |

## Replay Results

- Triggers processed: 2769
- Decisions published: 2769
- Actions published: 75
- Abstentions (do-nothing): 2694
- Holdout blocked: 1372
- Fallbacks used: 3
- Outcomes judged: 1388

## Latency

- p50: 9 ms
- p95: 18 ms
- p99: 20 ms
- Max: 67 ms
- Within 60s budget: 100.0%
- Failures (>5min): 0

## Safety Checks Summary

The independent verifier enforced:
- Holdout arm: zero actions delivered
- Consent: push/WA only to opted-in users
- Quiet hours: no push/WA 22:00–08:00 IST
- Frequency cap: at most 1 push/WA per user per IST day
- Dialect match: all titles verified to match viewer dialect
- Availability: all titles checked at publish time
- Prompt injection: injection-flagged titles blocked
- Forbidden push phrases: checked and rejected

## Impact Summary

- Agent arm success rate: 30.6%
- Holdout arm success rate: 0.0%
- Uplift: +30.6 pp (95% CI: [+27.6, +33.7] pp)

See `impact_report.md` for the full analysis and pilot re-analysis.

## Model Faults Handled

- unsupported_claim for trigger 202cccb8-8127-465f-b2d0-038cf533197e: user U-01991 → fallback to rule-based action or no action
- unsupported_claim for trigger a54e16b6-056d-4d0a-a35d-bd8257802c54: user U-02509 → fallback to rule-based action or no action
- unsupported_claim for trigger cd885956-0406-4b17-8e0b-3b97bbdd8a13: user U-04860 → fallback to rule-based action or no action
- malformed_output for trigger 76b138f6-0952-456d-904a-bd0d04497cf7: user U-02563 → fallback to rule-based action or no action
- timeout for trigger 5edb5b00-0773-4063-8da8-40929553233c: user U-01090 → fallback to rule-based action or no action

## Control Events Handled

- `title_withdrawn` at 2026-10-01T07:00:00.000Z: {"title_id": "T0001", "reason": "licence ended early; the title can no longer be shown or promoted"}
- `goal_rule_corrected` at 2026-10-01T08:30:00.000Z: {"goal_id": "G03", "old_success": {"event": "app_open", "condition": "on a later IST calendar day within the window"}, "new_success": {"event": "play_started", "condition": "on a later IST calendar day within the window"}, "applies_to": "every G03 goal that is still open, including goals opened before this correction", "note": "Correction from the analytics owner: an app open alone was never meant to count as a return."}
- `dependency_unavailable` at 2026-10-01T10:35:00.000Z: {"dependency": "model_provider", "until": "2026-10-01T10:42:00.000Z", "detail": "every model call fails with HTTP 503"}
- `dependency_degraded` at 2026-10-01T13:10:00.000Z: {"dependency": "user_data_store", "until": "2026-10-01T13:17:00.000Z", "detail": "p95 read latency 8 s and 20% of reads time out"}

