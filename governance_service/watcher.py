"""Span watcher — background asyncio task for 100% trace governance coverage.

Polls ClickHouse every POLL_INTERVAL seconds for agent.task spans that have
not yet been governance-evaluated, then dispatches GovernanceRunner.evaluate()
for each unchecked trace.

Dedup is handled inside GovernanceRunner (gov_audit_log check), so the watcher
can poll aggressively without risk of double-evaluation.
"""

import asyncio
from datetime import datetime, timedelta

import structlog
from quality_gate_checker import run_quality_gate_checks

log = structlog.get_logger(__name__)

POLL_INTERVAL = 30       # seconds between watcher ticks
LOOKBACK_HOURS = 336     # how far back to look for unchecked traces (2 weeks)
BATCH_LIMIT    = 50      # max traces to process per tick


async def run_watcher(db, runner) -> None:
    """
    Infinite asyncio loop.

    Args:
        db:     GovernanceDB instance.
        runner: GovernanceRunner instance.
    """
    log.info("governance_watcher_started", poll_interval=POLL_INTERVAL)
    while True:
        try:
            await _tick(db, runner)
        except asyncio.CancelledError:
            log.info("governance_watcher_stopped")
            return
        except Exception as exc:
            log.error("governance_watcher_tick_error", error=str(exc))
        await asyncio.sleep(POLL_INTERVAL)


async def _tick(db, runner) -> None:
    """Single watcher iteration: find unchecked traces and evaluate them."""
    since = datetime.utcnow() - timedelta(hours=LOOKBACK_HOURS)

    try:
        unchecked = db.get_unchecked_traces(since_ts=since, limit=BATCH_LIMIT)
    except Exception as exc:
        log.error("watcher_fetch_unchecked_failed", error=str(exc))
        return

    # Quality gate check runs every tick on its own isolated DB connection
    # to avoid Broken pipe errors from sharing the connection with HTTP handlers.
    try:
        from db import GovernanceDB
        qg_decisions = await asyncio.get_event_loop().run_in_executor(
            None, _run_quality_gate_checks_isolated
        )
        if qg_decisions:
            log.info("quality_gate_decisions_written", count=qg_decisions)
    except Exception as exc:
        log.warning("quality_gate_check_error", error=str(exc))

    if not unchecked:
        log.debug("watcher_tick_nothing_new")
        return

    log.info("watcher_tick_found_traces", count=len(unchecked))

    for row in unchecked:
        trace_id = str(row.get("trace_id", ""))
        if not trace_id:
            continue
        try:
            # Run synchronously in a thread pool so we don't block the event loop
            result = await asyncio.get_event_loop().run_in_executor(
                None, _evaluate_sync, runner, trace_id
            )
            if result and not result.skipped:
                log.info(
                    "watcher_eval_complete",
                    trace_id=trace_id,
                    pii_leak_rate=round(result.pii_leak_rate, 4),
                    policy_blocks=result.policy_blocks,
                )
        except Exception as exc:
            log.error("watcher_eval_failed", trace_id=trace_id, error=str(exc))


def _run_quality_gate_checks_isolated() -> int:
    """Run quality gate checks with a fresh DB connection isolated from HTTP handlers."""
    from db import GovernanceDB
    qg_db = GovernanceDB()
    return run_quality_gate_checks(qg_db)


def _evaluate_sync(runner, trace_id: str):
    """Synchronous wrapper for GovernanceRunner.evaluate (run in thread pool)."""
    return runner.evaluate(trace_id=trace_id, run_id="")
