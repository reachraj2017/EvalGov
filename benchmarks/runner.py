"""
Benchmark Runner CLI
====================
Runs agent benchmark suites and triggers evaluation.

Usage:
    python benchmarks/runner.py --suite unit --name "my-run" --agent-version "v1.0"
    python benchmarks/runner.py --suite all --compare-baseline
    python benchmarks/runner.py --list-suites
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SUITES = ["unit", "integration", "collaboration"]
SUITES_DIR = Path(__file__).parent / "suites"


# ---------------------------------------------------------------------------
# BenchmarkRunner
# ---------------------------------------------------------------------------
class BenchmarkRunner:
    """
    Loads benchmark cases, creates eval runs, drives the agent under test
    (stubbed), and reports results via the eval runner API.
    """

    def __init__(
        self,
        eval_runner_url: str = "http://localhost:8000",
        clickhouse_host: str = "localhost",
    ) -> None:
        self.eval_runner_url = eval_runner_url.rstrip("/")
        self.clickhouse_host = clickhouse_host
        self.client = httpx.Client(timeout=30.0)

    # ------------------------------------------------------------------
    # Suite loading
    # ------------------------------------------------------------------

    def load_suite(self, suite_name: str) -> list[dict]:
        """
        Load benchmark cases from benchmarks/suites/{suite_name}/cases.json.
        Returns a list of case dicts.
        """
        cases_path = SUITES_DIR / suite_name / "cases.json"
        if not cases_path.exists():
            raise FileNotFoundError(
                f"Suite file not found: {cases_path}\n"
                f"Available suites: {SUITES}"
            )
        with cases_path.open("r", encoding="utf-8") as fh:
            cases = json.load(fh)
        print(f"[runner] Loaded {len(cases)} case(s) from suite '{suite_name}'")
        return cases

    # ------------------------------------------------------------------
    # Run management
    # ------------------------------------------------------------------

    def create_run(self, name: str, suite: str, agent_version: str) -> str:
        """
        Create a new eval run via the eval runner API.
        Falls back to a random UUID if the API is unreachable.
        Returns the run_id string.
        """
        payload = {"name": name, "suite": suite, "agent_version": agent_version}
        try:
            resp = self.client.post(f"{self.eval_runner_url}/runs", json=payload)
            resp.raise_for_status()
            run_id = resp.json().get("run_id", str(uuid.uuid4()))
            print(f"[runner] Created run via API — run_id: {run_id}")
            return run_id
        except (httpx.ConnectError, httpx.HTTPStatusError) as e:
            run_id = str(uuid.uuid4())
            print(
                f"[runner] WARNING: Could not reach eval runner ({e}). "
                f"Using local run_id: {run_id}"
            )
            return run_id

    # ------------------------------------------------------------------
    # Single case execution (stub)
    # ------------------------------------------------------------------

    def run_benchmark_case(self, case: dict, run_id: str) -> None:
        """
        Placeholder for agent invocation.

        In a real system this would:
          1. Send task_input to the agent under test.
          2. Collect the OpenTelemetry trace produced by the agent.
          3. Register the completed trace with the eval runner for scoring.

        For now it prints case details and sends a fake completed-trace
        notification to the eval runner so the rest of the pipeline can
        be exercised end-to-end.
        """
        name = case.get("name", "unknown")
        task_input = case.get("task_input", "")
        print(f"  Running case: {name} | Input: {task_input[:100]}")

        fake_trace_id = str(uuid.uuid4()).replace("-", "")

        payload = {
            "run_id": run_id,
            "trace_id": fake_trace_id,
            "benchmark_id": case.get("benchmark_id", ""),
            "task_input": task_input,
            "expected_output": case.get("expected_output", ""),
            "status": "completed",
        }

        try:
            resp = self.client.post(
                f"{self.eval_runner_url}/traces/register", json=payload
            )
            if resp.status_code in (200, 201, 202):
                print(f"    -> Trace {fake_trace_id[:12]}... registered for evaluation.")
            else:
                print(
                    f"    -> WARNING: eval runner returned {resp.status_code}: {resp.text[:200]}"
                )
        except httpx.ConnectError:
            print(
                f"    -> WARNING: eval runner unreachable. "
                f"Trace {fake_trace_id[:12]}... not registered."
            )

    # ------------------------------------------------------------------
    # Suite runner
    # ------------------------------------------------------------------

    def run_suite(
        self, suite: str, run_name: str, agent_version: str
    ) -> str:
        """
        Creates a run, iterates all cases in the suite, executes each one,
        and returns the run_id.
        """
        cases = self.load_suite(suite)
        run_id = self.create_run(run_name, suite, agent_version)

        print(f"\n[runner] Starting suite '{suite}' ({len(cases)} cases)  run_id={run_id}")
        print("-" * 70)

        for i, case in enumerate(cases, start=1):
            print(f"[{i}/{len(cases)}] ", end="")
            self.run_benchmark_case(case, run_id)

        print("-" * 70)
        print(f"[runner] Suite '{suite}' complete. run_id={run_id}\n")
        return run_id

    # ------------------------------------------------------------------
    # Comparison & reporting
    # ------------------------------------------------------------------

    def compare_with_baseline(self, run_id: str) -> None:
        """
        Fetch regression data from the eval runner and print a formatted
        comparison table.
        """
        print(f"\n[runner] Fetching regression report for run {run_id[:12]}...")
        try:
            resp = self.client.get(f"{self.eval_runner_url}/runs/{run_id}/regression")
            resp.raise_for_status()
            data = resp.json()
        except httpx.ConnectError:
            print("ERROR: Cannot reach eval runner.")
            return
        except httpx.HTTPStatusError as e:
            print(f"ERROR: {e.response.status_code} — {e.response.text[:200]}")
            return

        if not data:
            print("No regression data returned.")
            return

        print(f"\n{'Metric':<30} {'Baseline':>10} {'Current':>10} {'Delta':>10}  Status")
        print("-" * 70)
        for row in data:
            metric = row.get("metric", "?")
            baseline = float(row.get("baseline_score") or 0)
            current = float(row.get("current_score") or 0)
            delta = float(row.get("delta") or 0)
            if delta <= -0.02:
                status = "REGRESSED 🔴"
            elif delta >= 0.02:
                status = "IMPROVED  🟢"
            else:
                status = "UNCHANGED ⚪"
            print(f"{metric:<30} {baseline:>10.4f} {current:>10.4f} {delta:>+10.4f}  {status}")
        print()

    def print_results(self, run_id: str) -> None:
        """
        Fetch scores for a run from the eval runner and print a formatted
        score table.
        """
        print(f"\n[runner] Fetching scores for run {run_id[:12]}...")
        try:
            resp = self.client.get(f"{self.eval_runner_url}/runs/{run_id}/scores")
            resp.raise_for_status()
            data = resp.json()
        except httpx.ConnectError:
            print("ERROR: Cannot reach eval runner.")
            return
        except httpx.HTTPStatusError as e:
            print(f"ERROR: {e.response.status_code} — {e.response.text[:200]}")
            return

        if not data:
            print("No score data returned.")
            return

        print(f"\n{'Metric':<30} {'Avg Score':>10} {'Count':>8} {'Min':>8} {'Max':>8}")
        print("-" * 68)
        for row in data:
            metric = row.get("metric", "?")
            avg_score = float(row.get("avg_score") or 0)
            count = row.get("num_scores", row.get("count", "?"))
            min_s = float(row.get("min_score") or 0)
            max_s = float(row.get("max_score") or 0)
            print(f"{metric:<30} {avg_score:>10.4f} {count:>8} {min_s:>8.4f} {max_s:>8.4f}")
        print()

    def __del__(self):
        try:
            self.client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark Runner CLI for the AI Eval Platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--suite",
        choices=SUITES + ["all"],
        help="Which benchmark suite to run (or 'all' to run every suite).",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Human-readable name for this run (defaults to '<suite>-run').",
    )
    parser.add_argument(
        "--agent-version",
        default="v0.0.0",
        help="Version string for the agent under test (default: v0.0.0).",
    )
    parser.add_argument(
        "--compare-baseline",
        action="store_true",
        help="After running, compare results with the current baseline run.",
    )
    parser.add_argument(
        "--list-suites",
        action="store_true",
        help="List available benchmark suites and exit.",
    )
    parser.add_argument(
        "--eval-runner-url",
        default=os.environ.get("EVAL_RUNNER_URL", "http://localhost:8000"),
        help="Base URL of the eval runner API (default: http://localhost:8000).",
    )
    parser.add_argument(
        "--clickhouse-host",
        default=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        help="ClickHouse hostname (default: localhost).",
    )

    args = parser.parse_args()

    runner = BenchmarkRunner(
        eval_runner_url=args.eval_runner_url,
        clickhouse_host=args.clickhouse_host,
    )

    # ------------------------------------------------------------------
    # --list-suites
    # ------------------------------------------------------------------
    if args.list_suites:
        print("Available benchmark suites:")
        for suite in SUITES:
            cases_path = SUITES_DIR / suite / "cases.json"
            if cases_path.exists():
                with cases_path.open() as fh:
                    count = len(json.load(fh))
                print(f"  {suite:<20} {count} case(s)  [{cases_path}]")
            else:
                print(f"  {suite:<20} (cases.json not found at {cases_path})")
        return

    # ------------------------------------------------------------------
    # --suite required beyond this point
    # ------------------------------------------------------------------
    if not args.suite:
        parser.error("--suite is required unless --list-suites is specified.")

    suites_to_run = SUITES if args.suite == "all" else [args.suite]
    run_ids: list[str] = []

    for suite in suites_to_run:
        run_name = args.name or f"{suite}-run"
        try:
            run_id = runner.run_suite(
                suite=suite,
                run_name=run_name,
                agent_version=args.agent_version,
            )
            run_ids.append(run_id)
            runner.print_results(run_id)
            if args.compare_baseline:
                runner.compare_with_baseline(run_id)
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
