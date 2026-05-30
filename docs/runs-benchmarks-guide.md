# Runs & Benchmarks — Step-by-Step Guide

How to use the Runs and Benchmarks tabs in the Eval UI to create test cases, track agent versions, score traces, and compare regressions.

---

## Part 1 — Create a benchmark test case

1. Go to **Benchmarks** tab → **Benchmark Test Cases** sub-tab
2. Scroll to **Add Benchmark** form at the bottom
3. Fill in:
   - Suite: `integration`
   - Name: `Search and summarize test`
   - Difficulty: `medium`
   - Task Input: `Search for Virat Kohli cricket stats and summarize in 3 bullet points`
   - Expected Output: `Should return 3 bullet points covering career stats`
   - Rubric: `{"criteria": ["returns exactly 3 bullets", "mentions batting average", "factually accurate"]}`
4. Click **Save Benchmark** — it appears in the table above

Repeat for as many test cases as you want.

---

## Part 2 — Create a Run

1. Go to **Runs** tab
2. Fill in:
   - Run Name: `my-agent-v1`
   - Suite: `integration`
   - Agent Version: `v1.0`
3. Click **Create Run**
4. Copy the `run_id` shown in the success message

---

## Part 3 — Run your agent with that run_id

In your agent code, set `run.id = <the run_id you copied>` on your `agent.task` spans before running queries. This tags all traces from this session to that run.

Then run your agent normally — send it the same task inputs you defined in your benchmarks.

---

## Part 4 — Trigger evaluation

1. Go back to **Runs** tab → scroll to **Trigger Offline Evaluation**
2. Select `my-agent-v1` from the dropdown
3. Click **Trigger Evaluation**
4. The Eval Runner re-scores all traces in that run and writes results to ClickHouse

---

## Part 5 — View the scores

1. Go to **Scores** tab → select `my-agent-v1` from the run dropdown
2. You'll see the bar chart of average scores per metric and individual score records

---

## Part 6 — Set a baseline and compare regressions

1. Go to **Runs** tab → **Set Baseline** → select `my-agent-v1` → click **Set as Baseline**
2. Make a change to your agent, create a new run `my-agent-v2`, run it, trigger evaluation
3. Go to **Regression** tab → select `my-agent-v2` → it shows the delta vs v1 for every metric (green = improved, red = degraded)

---

## Part 7 — Review scores (optional)

1. Go to **Benchmarks** tab → **Human Review Queue** sub-tab
2. Any LLM judge scores that look off — expand, adjust the slider, add a note, submit

---

## Full loop summary

```
Define test cases (Benchmarks)
      ↓
Create a run (Runs)
      ↓
Execute agent with run.id set in spans
      ↓
Trigger evaluation (Runs → Trigger Offline Evaluation)
      ↓
Review scores (Scores tab)
      ↓
Set as baseline (Runs → Set Baseline)
      ↓
Run next agent version, compare regressions (Regression tab)
      ↓
Human review any scores that look off (Benchmarks → Human Review Queue)
```
