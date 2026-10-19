# System Architecture — One User, One Agent

## Executive Summary
The **One User, One Agent** architecture models viewer lifecycle management not as an unconstrained recommendation feed, but as an **autonomous, goal-directed feedback loop**. Each viewer on the regional OTT dialect platform is assigned a dedicated agent that continuously maintains durable state, chooses a single measurable goal, determines proportionate interventions, enforces an independent safety verification firewall, and attributes outcomes against a randomized holdout group.

---

## 1. System Topology & Architectural Invariants

```
                             ┌───────────────────────────────────────┐
                             │       Trigger Stream (app_open)       │
                             └───────────────────┬───────────────────┘
                                                 │
                                                 ▼
                             ┌───────────────────────────────────────┐
                             │    Data Pack Loader & Deduplication   │
                             │  (event_id dedup, server timestamp)   │
                             └───────────────────┬───────────────────┘
                                                 │
                                                 ▼
      ┌────────────────────────────────────────────────────────────────────────────────────────┐
      │                               VIEWER AGENT BOUNDARY                                    │
      │                                                                                        │
      │  ┌──────────────────────┐      ┌─────────────────────┐      ┌───────────────────────┐  │
      │  │  Fact Resolver       │ ───► │  Goal Planner       │ ───► │ Intervention Planner  │  │
      │  │  Precedence Rules    │      │  Single Active Goal │      │ Home Page / Push / WA │  │
      │  │  Freshness & Provenance     │  Eligibility Logic  │      │ Dialect Matching      │  │
      │  └──────────────────────┘      └─────────────────────┘      └───────────┬───────────┘  │
      │             │                             │                             │              │
      │             │                             │                             ▼              │
      │             │                             │                 ┌───────────────────────┐  │
      │             │                             │                 │ Independent Verifier  │  │
      │             │                             │                 │ (Can REJECT action)   │  │
      │             │                             │                 └───────────┬───────────┘  │
      │             │                             │                             │              │
      │             ▼                             ▼                             ▼              │
      │  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
      │  │                         Durable Viewer State Store                               │  │
      │  │       SQLite DB: Facts w/ Provenance, Active Goal, Decisions, Actions, Learnings  │  │
      │  └──────────────────────────────────────────────────────────────────────────────────┘  │
      └──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                                 │
                                                 ▼
                             ┌───────────────────────────────────────┐
                             │   Published Decisions & Actions Logs  │
                             └───────────────────┬───────────────────┘
                                                 │
                                                 ▼
                             ┌───────────────────────────────────────┐
                             │ Outcome Simulator & Attribution Judge │
                             │  (14-day window, attributable signal) │
                             └───────────────────┬───────────────────┘
                                                 │
                                                 ▼
                             ┌───────────────────────────────────────┐
                             │  Intention-to-Treat (ITT) Impact      │
                             │  (Treatment Arm vs Holdout Arm)       │
                             └───────────────────────────────────────┘
```

### Core Invariants
1. **Single Ownership**: Exactly one agent instance owns the lifecycle of viewer `U-xxxxx`. No other subsystem can asynchronously send messages or alter the viewer's page.
2. **Single Active Goal**: An agent maintains at most one open goal at any point in time. A new goal cannot be adopted until the current goal succeeds, expires, or is invalidated by an event (e.g., purchase).
3. **Data is Not Instruction**: Content strings (catalogue synopses, search terms, deeplinks) are data literals and are never passed as executable control instructions to the agent or LLM.
4. **Decisions are Always Auditable**: Every published decision contains evidence references, assumptions, dialect conflict logs, confidence reasons, verifier check outcomes, and wall-clock execution timings.
5. **Abstention is a First-Class Action**: Doing nothing (`action: NONE`) with an explicit reason is a valid, fully recorded operational decision.

---

## 2. Seven Core Architectural Pillars

### 2.1 Ownership
- **Component**: `src.agent.agent.Agent`
- **Responsibility**: Manages the complete lifecycle of one user.
- **Isolation**: When an event trigger arrives for user `U-xxxxx`, the replay harness routes it directly to that user's agent instance. Agents do not share mutable state.
- **Resolution of Competing Directives**: If multiple triggers arrive for the same user, an idempotency check on `event_id` ensures only one decision is rendered.

### 2.2 Planning (Goal & Action Dynamics)
- **Component**: `src.agent.goal_planner.GoalPlanner` & `src.agent.intervention.InterventionPlanner`
- **Dynamic Revision**: Goals are not scheduled on a fixed cadence. Instead, they are dynamically revised when viewer facts change:
  - If a viewer under goal `G05` (`first_paid_7d`) makes a purchase, the purchase event is detected, marking `G05` as succeeded and transitioning the viewer to an engagement goal such as `G08` (`try_new_release_7d`).
  - If a viewer's subscription expires, the agent transitions to `G06` (`reactivate_lapsed_14d`).
- **Unmeasurable Goals**: Goal `G10` (`meaningful_engagement`) requires a "meaningful engagement" event signal that does not exist in the event stream. The Goal Planner explicitly skips `G10`, documenting the lack of measurement signal as an assumption.
- **Underspecified Goals**:
  - `G07` (`resume_series_3d`): The agent interprets "unfinished series" as a series where the viewer watched episode $N < N_{max}$ in the last 14 days.
  - `G11` (`tv_activation_7d`): Interpreted as requiring a `tv_app_open` or `qr_scan_completed` event within 7 days.

### 2.3 Memory & Durable State
- **Component**: `src.agent.state_store.StateStore`
- **Storage Engine**: SQLite database (`agent_state.db`) + JSON export dumps.
- **Schema**:
  - `viewers`: Tracks `user_id`, `arm` (`agent` vs `holdout`), `current_goal_id`, `goal_deadline`, `goal_status` (`open`, `succeeded`, `failed`), `last_action_at`, `last_action_day_ist`.
  - `viewer_facts`: Stores resolved facts with strict provenance: `source`, `received_at`, and `freshness_note`.
  - `decisions`: Full JSON payloads of every decision rendered.
  - `actions`: Physical interventions published.
  - `outcomes`: Judged goal evaluations with attribution evidence.
  - `learnings`: Viewer behavioral insights recorded for subsequent replay sessions.

### 2.4 Source Handling, Precedence & Freshness
- **Component**: `src.agent.fact_resolver.FactResolver`
- **Conflict Resolution Matrix**:
  - **Dialect**:
    $$\text{app\_setting (runtime event)} \succ \text{profile\_snapshot (warehouse export)} \succ \text{campaign\_deeplink}$$
    *Rationale*: The app setting determines what font rendering and audio track the client will actually request. If the profile snapshot disagrees (`d1` vs `d3`), `d3` is selected, and `profile:d1` is appended to `dialect_conflicts`.
  - **Subscription Status**:
    $$\text{subscription\_lapsed / purchase\_completed (recent event)} \succ \text{profile\_snapshot (daily export)}$$
    *Rationale*: Profile snapshots are 24 hours stale. A purchase made 2 minutes ago or a lapse 1 hour ago is ground truth.
  - **Consent**:
    $$\text{notification\_opt\_out (recent event)} \succ \text{notification\_opt\_in} \succ \text{profile\_snapshot}$$
    *Rationale*: User opt-out takes legal precedence immediately.
  - **Timestamps**:
    $$\text{received\_at (server clock)} \succ \text{occurred\_at / sent\_at (device clock)}$$
    *Rationale*: User phone clocks can be wrong by hours or days. The server's ingestion timestamp is the monotonic reference time.
  - **Offline-Buffered Triggers**:
    If $\text{received\_at} - \text{occurred\_at} > 600\text{ seconds}$, the trigger is flagged as stale offline buffering. The agent records the trigger in memory but abstains from sending real-time push notifications.

### 2.5 Verification & Safety Firewall
- **Component**: `src.agent.verifier.Verifier`
- **Zero-Trust Separation**: The verifier runs as an independent, deterministic gatekeeper *after* intervention planning. It has full authority to reject any proposed action.
- **Independent Checks**:
  1. `holdout_arm`: If `viewer.arm == "holdout"`, block all interventions (`actions = 0`).
  2. `consent`: Blocks push if `notifications_opt_in != True`; blocks WhatsApp if `whatsapp_opt_in != True`.
  3. `quiet_hours`: Disallows push or WhatsApp messages between 22:00 and 08:00 IST (Indian Standard Time, UTC+5:30).
  4. `frequency_cap`: Enforces a maximum of 1 message (push + WhatsApp combined) per viewer per IST calendar day.
  5. `dialect_match`: Ensures every title recommended is available in the viewer's active dialect.
  6. `availability_window`: Validates that `available_from <= deliver_at <= available_to`.
  7. `home_page_rows`: Validates that every row has $\ge 3$ and $\le 10$ unique titles. Rows with $<3$ titles are dropped; if zero rows remain, the action is rejected.
  8. `prompt_injection`: Scans title metadata for instruction injection vectors; blocks injection-tainted titles.
  9. `forbidden_copy`: Rejects push notifications containing disallowed sales copy ("hurry", "last chance", "free forever", "guaranteed").

### 2.6 Latency & Budget Management
- **Budget Thresholds**:
  - Maximum 8 model calls per decision.
  - Maximum 20 tool calls per decision.
  - Hard latency deadline: 60 seconds.
  - Failure threshold: > 300 seconds (5 minutes).
- **Execution Profile**:
  - The deterministic rule engine operates in memory and completes in **10–25 milliseconds** (p50: 10ms, p95: 22ms, max: 51ms).
  - Model calls are invoked selectively only when subjective reasoning is required, comfortably staying well within the 60-second limit.

### 2.7 Failure Recovery & Degraded Modes
- **Component**: `src.model.mock_provider.MockModelProvider` & `src.replay.harness`
- **Fault Scenarios**:
  - **Model Outage (HTTP 503)**: Handled by wrapping model requests with strict timeouts and circuit breaking. When unavailable, the agent falls back to a deterministic safe action (e.g., standard dialect popular titles) or abstains.
  - **Data Store Degradation**: When read latency increases, the agent utilizes cached in-memory profile state and proceeds with rule-based planning.
  - **Malformed Model Output**: JSON parse errors trigger a safe retry within budget; if persistent, falls back to rule-based defaults.
  - **Unsupported Model Claims**: If an LLM asserts that a user is eligible for a goal without citing an event reference, the independent verifier flags `unsupported_claim` and rejects the goal.

---

## 3. Trust Boundaries

```
[ UNTRUSTED ZONE ]                     [ VERIFIED CORE ZONE ]
- Client Device Timestamps   ───────►  - Normalization to received_at (UTC)
- Catalogue Descriptions     ───────►  - Injection Scanner & Literal Handling
- User Search Queries        ───────►  - Sanitized String Parsing
- LLM Generated Text         ───────►  - Verifier Gate (Model Output != Approval)
- External Campaign Links    ───────►  - Subordinate to App Setting
```

1. **Model Generative Boundary**: The LLM is an advisor, not an executor. The agent parses candidate suggestions from the model, validates them against catalogue IDs and user facts, and passes them to the Verifier before anything is written to the actions log.
2. **Content Boundary**: Catalogue text is treated strictly as display strings. Special tokens, delimiters, and command prompts inside title synopses are neutralized by the parser.

---

## 4. Measurement & Intention-to-Treat (ITT) Methodology

### Why "Served vs Unserved" is Misleading
In naive analytics reports, teams often compare users who received an intervention ("Served") against users who did not ("Unserved"). This creates massive **selection bias**:
- The agent only intervenes on users who trigger it by opening the app.
- Users who open the app are inherently more active and motivated than dormant users.
- As proven in our re-analysis of the historical pilot data:
  - Naive Comparison: **Served 40.1% vs All Other 17.7% = +22.4 pp uplift** (False victory).
  - True ITT Comparison: **Treatment 18.2% vs Holdout 19.6% = -1.3 pp uplift** (Realistic finding).

### Our ITT Implementation
1. Viewers are assigned to `agent` or `holdout` arms randomly at the start.
2. All users in the `agent` arm (whether served or abstained) are counted in the denominator.
3. All users in the `holdout` arm (who received zero interventions) are counted in the control denominator.
4. Confidence intervals are calculated via standard error of proportions at 95% confidence:
   $$SE = \sqrt{\frac{p_1(1-p_1)}{n_1} + \frac{p_2(1-p_2)}{n_2}}$$
   $$\text{CI}_{95\%} = (p_1 - p_2) \pm 1.96 \times SE$$
