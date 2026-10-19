# AI Collaboration Log — One User, One Agent

## Overview

This project was developed through pair-programming between a human AI engineer and state-of-the-art AI coding assistants (**Claude 3.7 / Sonnet 4.6 in Antigravity IDE** and **Gemini 3.8 Flash**).

In accordance with the evaluation criteria, the human engineer maintained active architectural direction, strictly verified all generated code against domain constraints, and explicitly caught and corrected several AI failure modes.

---

## 1. How AI Tools Were Directed

1. **Specification-First Decomposition**: The prompt context was grounded by parsing the official assignment brief (`assignment_one_user_one_agent.pdf`) and candidate guide (`assignmentGuid.md`). The system was partitioned into modular components: `data.loader`, `agent.fact_resolver`, `agent.goal_planner`, `agent.intervention`, `agent.verifier`, `agent.state_store`, `model.mock_provider`, `replay.harness`, and `evaluation.impact`.
2. **Test-Driven Verification**: Before deploying changes to the full 2,769-trigger replay, automated unit tests (`tests/test_all.py`) were established covering edge cases: duplicate events, stale phone clocks, three-source dialect conflicts, quiet hours, frequency caps, prompt injections, and mid-replay rule corrections.
3. **Reproducibility Without Personal API Keys**: The AI was directed to construct a deterministic `MockModelProvider` that accurately simulates latency, timeouts, malformed JSON, and unsupported claims, enabling evaluators to run the entire pipeline offline with zero external dependencies.

---

## 2. Verification Methodology: How AI Work Was Checked

All AI-generated code underwent three levels of verification:
1. **Static Analysis & Linting**: Ensuring type annotations, proper imports, and standard library adherence.
2. **Automated Unit Testing (`pytest`)**: 23 targeted test cases asserting edge case handling, idempotency, and verifier vetoes.
3. **Full End-to-End Replay Run**: Running the entire pipeline across 2,769 real triggers and 31,194 simulated outcome events, logging all decisions, actions, and exceptions to disk.

---

## 3. Concrete Examples of Corrected or Rejected AI Output

### Example 1: Dialect Conflicts Dictionary Key Mismatch (Bug Caught & Fixed)
- **What the AI Generated**: In `src/agent/agent.py`, the AI wrote:
  ```python
  "conflicts": [f"{c['conflict_source']}:{c['conflict_value']}" for c in facts.dialect_conflicts],
  ```
  However, in `src/agent/fact_resolver.py`, the AI had populated `dialect_conflicts` using keys `"source"` and `"value"`:
  ```python
  facts.dialect_conflicts.append({"source": src, "value": val})
  ```
- **How It Was Discovered**: During the initial execution of the full 2,769-trigger replay, the background harness log showed:
  `ERROR src.replay.harness Unhandled agent error for user U-04556 trigger 6f321743-...: 'conflict_source'`
- **Correction Applied**: The running task was immediately killed. Two safety fixes were implemented:
  1. Updated `fact_resolver.py` to populate bidirectional aliases:
     ```python
     facts.dialect_conflicts.append({
         "source": src, "value": val,
         "conflict_source": src, "conflict_value": val
     })
     ```
  2. Updated `agent.py` to safely extract keys using `.get()` fallbacks:
     ```python
     "conflicts": [
         f"{c.get('conflict_source') or c.get('source')}:{c.get('conflict_value') or c.get('value')}"
         for c in facts.dialect_conflicts
     ]
     ```
  Re-ran tests and verified 23 passed, and the full replay completed with zero unhandled exceptions.

---

### Example 2: Naive "Served vs Unserved" Uplift (Statistical Bias Rejected)
- **What the AI Suggested**: In early draft discussions of the impact report, the assistant suggested calculating uplift by comparing viewers who received an active intervention against viewers who received no action.
- **Why It Was Rejected**: This is the classic **selection bias** trap identified in the assignment brief. Viewers who receive an intervention are viewers who actively opened the app during peak hours; comparing them against inactive or holdout viewers falsely inflates apparent conversion.
- **Correction Applied**: Mandated strict **Intention-To-Treat (ITT)** evaluation across randomized arms:
  - **Treatment Arm**: All 855 triggered viewers assigned to the agent arm, regardless of whether the agent acted or abstained.
  - **Holdout Arm**: All triggered viewers assigned to the holdout arm (guaranteed zero interventions).
  - Explicitly wrote a dedicated pilot re-analysis in `sample_run/impact_report.md` proving how the historical report's "2.3x uplift" was a selection bias artifact that vanished under proper ITT analysis (-1.3 pp).

---

### Example 3: Goal G10 Unmeasurable Signal Trap (Proxy Rejected)
- **What the AI Suggested**: For Goal G10 (`meaningful_engagement`), the AI initially proposed computing a synthetic proxy using cumulative watch time or app open frequency.
- **Why It Was Rejected**: The assignment instructions specifically highlight that "one success signal does not exist in the event stream at all" and warn: *"Goals or eligibility asserted without evidence will score poorly."* Creating a synthetic proxy violates the principle of attributable evidence.
- **Correction Applied**: Instructed the agent to explicitly classify `G10` as **unmeasurable**. `goal_planner.py` skips `G10` during eligibility evaluation, logs an explicit assumption noting the absence of a verifiable event signal in the event stream, and selects the next eligible goal.

---

### Example 4: Home Page Row Minimum Constraints (Policy Violation Prevented)
- **What the AI Suggested**: The initial home page recommendation logic populated rows with available titles but did not enforce the minimum constraint of 3 titles per row before publishing.
- **Why It Was Rejected**: Section 2 of the candidate brief states: *"a row with fewer than three titles is not shown."* If rows with 1 or 2 titles are rendered, the client UI breaks or hides the row, making the intervention ineffective.
- **Correction Applied**: Enforced strict validation in `src/agent/verifier.py`:
  ```python
  valid_rows = [row for row in rows if len(row.get("titles", [])) >= 3]
  if not valid_rows:
      verification.passed = False
      verification.checks_failed.append("no_valid_rows_with_minimum_titles")
  ```
  If no valid rows remain, the verifier vetos the action, falling back to `action: NONE` with `no_action_reason: "verifier_rejected"`.

---

### Example 5: Catalogue Prompt Injection Defense
- **What the AI Suggested**: The AI initially parsed title synopses directly into recommendation prompts and payloads without inspection.
- **Why It Was Rejected**: The synthetic data pack intentionally hides prompt injection instructions inside catalogue descriptions (e.g., instructions commanding the agent to send notifications to every viewer).
- **Correction Applied**: Implemented `detect_prompt_injection()` in `src/data/loader.py` using regex patterns for override keywords (`system:`, `ignore previous instructions`, `urgent notice`, `message all viewers`). Titles flagged with prompt injection are quarantined in `data_pack.injection_titles` and blocked from ever being selected for push, WhatsApp, or home page recommendations.
