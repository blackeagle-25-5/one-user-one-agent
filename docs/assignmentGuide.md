# STAGE – Assignment Guide: One User, One Agent

This guide explains the STAGE take-home assignment in simple steps: what you have to build, what the data contains, which traps to watch for, how to plan your time and what to submit. Read it fully once before you start coding.

## 📎 Links

- **Assignment (PDF):** One User, One Agent – Assignment Brief
- **Data Pack (ZIP folder):** One User, One Agent – Data Pack

---

## 1. Assignment at a Glance

| Item | Details |
| --- | --- |
| Role level | AI Engineer / Applied LLM Engineer |
| Effort | 10–12 hours of work |
| Deadline | Submit within 4 calendar days |
| Format | Take-home build (code repository) + 60-minute technical discussion |
| AI tools | Claude, Codex etc. are allowed and encouraged, but you must check everything they produce |
| What they really judge | Good decisions, evidence, speed, safety and honest measurement. NOT recommendation accuracy |

---

## 2. What Is This Assignment? (In Simple Words)

STAGE is an OTT app that streams regional-dialect films, shows and micro-dramas. Many users open the app and leave without watching, many never finish their first episode, and many trial users never pay.

You have to build an **AI agent for every user**. Each time a user opens the app, their agent must:

1. Look at what is known about the user.
2. Pick **one goal** for the user (for example, "finish the first episode within 3 days").
3. Decide **what to do**: change the home page, send one message, or **do nothing**.
4. Do it **fast** (within 60 seconds) and **safely** (follow all rules).
5. Later, check honestly whether it **worked**.
6. Prove the agent made a real difference by comparing against a **holdout group** (users the agent never touches).

**Key point:** this is NOT a recommendation system task. A clever title recommender without a reliable decide → act → measure loop will score poorly.

---

## 3. Key Terms (Learn These First)

| Term | Simple meaning |
| --- | --- |
| Agent | The program that owns one user's goal, plan, actions and history |
| Trigger | An event (app open or login) that wakes up the agent to make a decision |
| Goal | One measurable target for the user, with a deadline and a success event |
| Intervention / Action | What the agent changes: home page, push, WhatsApp, in-app message (or nothing) |
| Dialect | Regional language variant (d1–d6). Content must match the dialect the user will actually see |
| Holdout | A random group of users who get no agent action. Used to measure real impact |
| Replay | Running your agent on a recorded day of triggers, as if it were live |
| Simulator | A given Python script that plays out what users do after your actions |
| Uplift | How much better the agent group did compared with the holdout group |
| Idempotent | Processing the same event twice gives the same result (no duplicate actions) |
| Provenance | Where a fact came from and how fresh it is |
| Latency | Time taken from trigger to published decision |
| Abstention | Choosing to do nothing, with a recorded reason |

---

## 4. Understand the Data Pack

The ZIP has 5 folders plus a README. **Read the README first.** Only this pack counts as evidence; never invent user facts.

| Folder / File | What it contains | What to watch for |
| --- | --- | --- |
| data/events.jsonl | 30 days of app events for 5,000 users | Duplicates (same event_id), late events, wrong phone clocks |
| data/profiles_snapshot.csv | Daily profile export (signup, subscription, dialect, consent) | One day stale, no rows for brand-new users, some users appear twice |
| data/catalogue.csv | Title records across 6 dialects | Language variants share a content_slug; availability windows; some descriptions contain fake instructions |
| data/episodes.csv | Episodes for every title | Needed for "next episode" and unfinished-series logic |
| config/goals.json | 12 candidate goals with eligibility and success rules | Some rules are unclear, and one has no measurable success event |
| config/interventions.json | Home page, push, WhatsApp, in-app message rules | Row limits, TTL, dialect rule, templates, quiet hours |
| config/pricing.json | The only prices that exist | Never mention any other price, discount or offer |
| config/messaging_policy.md | Consent, quiet hours, frequency, honesty, holdout, privacy rules | Breaking any of these is a serious fail |
| pilot/ | Results from an earlier pilot + an internal summary report | The report's headline uplift uses the wrong comparison |
| replay/ | The replay day: triggers, live events, control events, model faults, arms | Contains injected surprises your agent must handle |
| simulator/simulate_outcomes.py | Simulates user behaviour for 14 days after your actions | Evaluators rerun it with a different seed and an unseen replay day |

**Important data conventions**

- All timestamps are UTC. Users are in India (IST = UTC + 5:30). Quiet hours and "per day" rules use **IST**.
- `received_at` (server time) is trustworthy. `occurred_at` and `sent_at` (phone time) can be wrong.
- The same event can arrive more than once with the same `event_id`. Deduplicate.
- `app_dialect` on each event is the app's current setting.

---

## 5. What Your Agent Must Do (The 7-Step Loop)

### Step 1 – Understand the viewer

- Collect all facts about the user: signup, subscription, dialect, consent, watch history.
- Store each fact with its **source**, **time** and **freshness**.
- When sources disagree (profile vs events vs app settings vs campaign link), resolve it with a clear rule and **keep the conflict visible** in the output.
- Write down your **precedence rules per fact**. Example: a newer purchase event beats an old profile snapshot for subscription status; the app setting decides which dialect the user will actually see.

### Step 2 – Choose a goal

- Pick **one** goal from goals.json that the user is **eligible** for, based on real evidence.
- Set the **deadline** (goal start + window) and the **success signal**.
- If no goal fits or the evidence is too weak, say so. That is a valid decision.
- Only one active goal per user at a time. Revise it when the situation changes (for example, after a purchase).

### Step 3 – Decide the intervention

- Options: home page, push, WhatsApp, in-app message, or **no action**.
- Use only content that exists, is available, and is in the dialect the user will see.
- Keep actions proportionate. Don't message everyone just because you can.

### Step 4 – Act within budget

- Publish within **60 seconds** of the trigger. More than **5 minutes** counts as a failure.
- Maximum **8 model calls** and **20 tool calls** per decision (make these configurable).
- If the model or the data store fails, fall back to a smaller safe action or no action.

### Step 5 – Verify before acting

- Run **independent checks**, separate from the decision step: eligibility, dialect, availability, consent, quiet hours, frequency cap, policy, duplicates, holdout.
- The checker must be able to **reject** the decision. Model output is never proof.

### Step 6 – Close the loop

- When the deadline passes or new events arrive, judge each goal as **succeeded**, **failed** or **inconclusive**.
- Use only signals you can attribute to the goal window.
- Save what was learned in the user's state.

### Step 7 – Prove impact

- Compare the **agent arm vs the holdout arm** (from replay/arms.csv) using simulator outcomes.
- Explain why comparing "served vs not served" users is misleading.

---

## 6. Required Decision Output

Every decision must be saved in machine-readable form (JSONL). You may add more fields.

```json
{
  "decision_id": "D-0417",
  "viewer_id": "V-2291",
  "trigger": {"event": "app_open", "received_at": "...Z", "event_age_s": 9},
  "goal": {"id": "first_full_episode_3d", "deadline": "...Z"},
  "action": "HOME_PAGE",
  "dialect": {"used": "d3", "source": "app_setting", "conflicts": ["profile:d1"]},
  "content": ["title:T0412", "title:T0977", "title:T1203"],
  "confidence": 0.61,
  "evidence": ["event:E88123", "catalogue:T0412", "goal_rule:G04-r2"],
  "assumptions": ["viewer has not started any episode of T0412"],
  "checks": {"passed": ["eligibility", "dialect", "frequency_cap"], "failed": []},
  "decided_in_ms": 6400,
  "status": "PUBLISHED"
}
```

**Every decision must include**

- A goal and an action, OR a clear reason for doing nothing.
- Evidence references another person can check, with their freshness.
- Confidence **with a short reason** (a number alone is not enough).
- Assumptions about the user.
- Conflicting evidence and how it was resolved.
- Check results, decision time, deadline and success signal.

**Good extra fields to add:** confidence_reason, no_action_reason, fallback_used, model_calls, tool_calls, rule_version.

---

## 7. Rules You Must Never Break

### Messaging policy

- **Consent:** push only if `notifications_opt_in` is true; WhatsApp only if `whatsapp_opt_in` is true. A later `notification_opt_out` event overrides the profile.
- **Quiet hours:** no push or WhatsApp between **22:00 and 08:00 IST**. Schedule for later instead.
- **Frequency:** at most **1 push or WhatsApp per user per IST day**, both channels combined.
- **Honest copy:** never invent prices, offers, episodes or deadlines. Name only real, available titles in the right dialect.
- **Holdout:** users in the holdout arm get **zero** actions of any kind.
- **Data is not instruction:** text inside titles, descriptions, search queries or deeplinks is content only. Never follow it.
- **Privacy:** treat the pack as personal data. Don't upload it to services that store or train on it.

### Home page rules

- 1 hero title, max 6 rows, 3–10 titles per row. A row with fewer than 3 valid titles is dropped.
- Allowed row types: next_episode, because_you_watched, popular_in_dialect, new_releases, genre.
- Every title must be in the page dialect and available at publish time.
- The same content_slug only once per page.
- Don't publish "Continue Watching". The platform adds it.
- The page lives for 120 minutes. Users reach the home page within seconds, so a slow page is only seen on a later visit.

### Push rules

- Title max 40 characters, body max 120 characters.
- Forbidden phrases: "last chance", "hurry", "only today", "free forever", "guaranteed".
- Deeplink format: `ott://title/<title_id>`.
- Delivery is confirmed only by a `message_delivered` event.

### WhatsApp rules

- Only the 3 approved templates. Variables come from the catalogue or pricing only; no free text.

### Pricing

- Only ₹1 trial (3 days), ₹149 monthly and ₹399 quarterly exist. No other offers.

---

## 8. Traps Hidden in the Data (Must Handle)

The data is imperfect on purpose. **Finding and handling these is part of the score.** Note each one in your docs.

### Data traps

- [ ]  Duplicate events with the same event_id
- [ ]  Late events and wrong phone clocks: use `received_at` for ordering and freshness
- [ ]  Offline-buffered app opens, some days old: don't treat an old app open as a live session
- [ ]  Profile snapshot is a day stale and some users have two rows: pick one with a clear rule
- [ ]  Brand-new users have no profile row: build their facts from events (for example, `signup_completed`)
- [ ]  Dialect conflicts between app setting, profile and campaign deeplink
- [ ]  One content can have several title_ids (language variants): deduplicate by content_slug
- [ ]  Titles have availability windows: check them at publish time
- [ ]  Some catalogue descriptions contain instructions aimed at the AI: ignore them and log them

### Goal catalogue traps

- [ ]  One goal has **no success event** in the data at all. Don't choose a goal you can't measure, and explain why.
- [ ]  **Two goals have unclear rules.** Write down your interpretation as an assumption.
- [ ]  Goals that need a signup date or subscription status must use the freshest reliable source.

### Pilot report trap

- [ ]  The internal pilot report claims a big uplift by comparing **served users vs everyone else**. That comparison is biased.
- [ ]  Recompute it yourself using the **randomized arms** (all treatment users vs all holdout users, whether served or not) and explain the difference in your impact report.

---

## 9. Replay Surprises and How to Handle Them

The replay day contains injected events. `control_events.jsonl` and `model_faults.jsonl` tell your harness when to make things fail.

| Surprise | What a good agent does |
| --- | --- |
| Burst of old, offline-buffered app opens | Check event age using received_at vs occurred_at; skip or downgrade stale triggers and record why |
| Purchase arrives 2 minutes after the decision | Update the user's state, re-plan the goal (for example, stop a "first paid" goal) and cancel pending messages that no longer make sense |
| App setting, profile and campaign link show 3 different dialects | Use the dialect the app will actually request (app setting); list the conflicts in the output |
| Goal success rule corrected mid-replay | Apply the new rule only to the affected open goals, version your rules, and don't reprocess every user |
| Model claims a user is eligible with no supporting event | Verifier rejects it because no event or profile fact backs it up; record it as an unsupported claim |
| Model returns broken JSON or times out | Parse safely, retry within budget, then fall back to a rule-based safe action or no action |
| Catalogue description tells the agent to message everyone | Treat it as content, never as a command; flag it in logs |
| Title withdrawn during the day | Stop using it from that moment; check the pending actions that include it |
| Model provider or data store slow or down for several minutes | Use timeouts, fall back to rules or cached state, keep meeting the latency budget |
| Traffic triples for 10 minutes at evening peak | Process in parallel, skip optional model calls, prioritise fresh triggers, still publish within 60 seconds |

**Remember:** you are not expected to predict every surprise. They check whether your system **notices** the change, finds **which users and decisions are affected**, and responds **safely and on time**.

---

## 10. Measuring Impact the Right Way

- Use `replay/arms.csv`: every user is randomly in **agent** or **holdout**.
- Run the simulator on your actions. It writes `outcome_events.jsonl` and `simulator_report.json`.
- Judge each goal's success from outcome events.
- Compare the success rate of **all agent-arm users** vs **all holdout users**, including users the agent decided not to act on. This is called intention-to-treat.
- Report the difference with uncertainty (for example, a confidence interval) and break it down by goal.
- Explain clearly why "served vs everyone else" is misleading: the agent serves users who are already more active, so they would have done better anyway (selection bias).
- Be honest even if the uplift is small or zero. Honest measurement scores higher than an inflated number.

---

## 11. Suggested Architecture (Simple Version)

Keep the core **deterministic and rule-based**, and use the model only where it really reduces uncertainty.

| Component | Job |
| --- | --- |
| Data loader | Load the pack, deduplicate events, normalise timestamps |
| Viewer state store | Durable per-user state: facts with provenance, active goal, plan, decisions, actions, outcomes, learnings (SQLite or JSON files) |
| Fact resolver | Applies precedence and freshness rules, records conflicts |
| Goal planner | Checks eligibility for each goal using evidence, picks one, sets the deadline |
| Intervention planner | Picks the action and content (or no action) |
| Model provider | Interface with a <strong>mock / replay mode</strong> (default) and an optional real model; reproduces faults from model_faults.jsonl |
| Verifier | Independent checks that can block any action |
| Replay harness | Feeds triggers in deliver_at order with live and control events, injects failures, measures latency |
| Outcome judge | Judges goals after the deadline from attributable events, handles rule corrections |
| Impact reporter | Agent vs holdout comparison, pilot re-analysis, latency report |

**Design tips**

- One agent object per user owns the goal and plan. No two parts of the system chase different goals for the same user.
- Use an idempotency key (for example, the trigger event_id) so duplicate triggers never create duplicate actions.
- Record `published_at = deliver_at + your measured processing time`.
- Every model output goes through the verifier. Never trust it as evidence.

---

## 12. Step-by-Step Work Plan

### 1 - Understand

- [ ]  Read the brief PDF twice and the data pack README fully.
- [ ]  Read all config files: goals, interventions, pricing, messaging policy.
- [ ]  Explore the data: count duplicates, check timestamps, profile duplicates, dialect conflicts, catalogue variants.
- [ ]  List every data issue you find (you'll reuse this list in your docs).
- [ ]  Recompute the pilot uplift using the randomized arms.
- [ ]  Set up the repo structure.

### 2 - Build the core

- [ ]  Data loader + deduplication.
- [ ]  Viewer state store with provenance.
- [ ]  Fact resolver with precedence rules.
- [ ]  Goal eligibility + selection (skip unmeasurable goals, document unclear ones).
- [ ]  Intervention planner (home page builder, push/WhatsApp/in-app choice, no action).
- [ ]  Independent verifier.
- [ ]  Mock model provider.

### 3 – Replay and measure

- [ ]  Replay harness: deliver_at order, live events, control events, model faults, latency timing.
- [ ]  Write decisions.jsonl and actions.jsonl.
- [ ]  Run the simulator and judge outcomes.
- [ ]  Impact report (agent vs holdout) and latency report.

### 4 – Test, document, submit

- [ ]  Write the required tests.
- [ ]  Write README, ARCHITECTURE, AI_COLLABORATION and KNOWN_LIMITATIONS.
- [ ]  Write final_report.md.
- [ ]  Fresh clone → run setup → run the replay command → confirm it works end to end.

---

## 13. Tests You Must Include (Minimum)

- [ ]  A **late or duplicate trigger** (no duplicate action, stale event handled)
- [ ]  A **conflicting source** (for example, dialect or subscription conflict resolved correctly)
- [ ]  A **tool failure** (model or data store fails → safe fallback)
- [ ]  A **corrected goal rule** (only affected open goals are re-judged)

**Good extra tests:** quiet hours, frequency cap, holdout gets nothing, injected instruction ignored, unsupported model claim rejected, row with fewer than 3 titles dropped, withdrawn title not used.

---

## 14. Submission Checklist

```
submission/
  README.md
  ARCHITECTURE.md
  AI_COLLABORATION.md
  KNOWN_LIMITATIONS.md
  src/
  tests/
  sample_run/
    decisions.jsonl
    viewer_state/
    actions.jsonl
    outcomes.jsonl
    latency_report.json
    impact_report.md
    final_report.md
```

| File | What it must contain |
| --- | --- |
| README.md | Setup steps and <strong>one command</strong> to run the full replay |
| ARCHITECTURE.md | Ownership, planning, memory, verification, latency, failure recovery, trust boundaries |
| AI_COLLABORATION.md | How AI tools helped, how you checked their output, and what you rejected |
| KNOWN_LIMITATIONS.md | What you left incomplete on purpose and why |
| src/ | Runnable CLI or API with a mock model mode (no personal API key needed) |
| tests/ | The automated tests listed above |
| sample_run/ | Decisions, actions, no-action reasons, per-user state, outcomes, latency report, impact report, final report |

---

## 15. How You Will Be Scored

| Area | Weight | What strong work looks like |
| --- | --- | --- |
| Goal and decision quality | 20% | Goals fit the user, actions are proportionate, "do nothing" chosen when right |
| Evidence and state | 15% | Facts have source and freshness, conflicts visible, state durable |
| Real-time reliability | 15% | Meets latency, idempotent triggers, safe failures |
| Verification and safety | 15% | Checks catch bad goals, wrong dialect, consent and cap issues, injected instructions |
| Measurement and learning | 15% | Honest outcomes, holdout comparison, results fed back into state |
| Engineering quality | 10% | Clear, testable, reproducible project |
| Use of AI tools | 10% | You direct, check and sometimes reject AI-generated work |

**What scores poorly (avoid these)**

- One big prompt that picks titles for every user
- Goals or eligibility claimed without evidence
- Messages in the wrong dialect, at night or without consent
- Uplift reported from served vs unserved users
- Ignoring late or duplicate events
- Reprocessing every user after one rule change
- Submitting AI-generated code that was never run and tested

---

## 16. Prepare for the Technical Discussion (60 Minutes)

Be ready to **walk one user from app open to a judged outcome** using your own sample run. Practise answers to:

- How does your agent decide that what it knows is current enough to act on?
- When should the agent do nothing, and how do you know that was right?
- How do you prove the agent caused the change and didn't just pick users who would convert anyway?
- What happens when 1,000 users open the app in the same minute?
- Which decisions should never be automated?
- How would this work for 10 million users and 40 dialects?
- What did an AI coding assistant suggest that you rejected, and why?

---

## 17. Tips to Stand Out

- **Honesty beats a big number.** If the agent shows little or no uplift, say so and explain why.
- **Make "no action" a proper decision**, with a reason, not a missing row.
- **Show the data issues you found**: a short table in your final report (issue → how you handled it) is very convincing.
- **Keep your AI collaboration log real**: include at least 2–3 examples where you corrected or rejected AI output.
- **Prioritise.** A small, working, well-tested loop beats a big unfinished system. Put whatever you skipped in KNOWN_LIMITATIONS.md.
- **Make it easy to run.** Evaluators rerun the simulator with a new seed and an unseen replay day, so avoid hard-coding anything specific to this replay.

---

## 18. Final Check Before Submitting

- [ ]  The replay runs with one command on a fresh clone, without my API key
- [ ]  Holdout users received zero actions
- [ ]  No push or WhatsApp in quiet hours, without consent, or more than once per IST day
- [ ]  All titles are in the right dialect and available; no withdrawn titles after withdrawal
- [ ]  Every decision has evidence, confidence reason, assumptions, checks and timing
- [ ]  All decisions published within 60 seconds (latency report included)
- [ ]  Impact measured agent arm vs holdout arm, with the pilot report's mistake explained
- [ ]  Required tests pass
- [ ]  All 4 docs written: README, ARCHITECTURE, AI_COLLABORATION, KNOWN_LIMITATIONS
- [ ]  sample_run folder complete