"""
Assembles a full trace tree from individual spans stored in ClickHouse.
Reconstructs parent-child relationships.
"""

from typing import Optional

import structlog

from db.repository import Repository

log = structlog.get_logger(__name__)


class TraceAssembler:
    """Reconstructs a trace tree from flat span records."""

    def __init__(self, repository: Optional[Repository] = None) -> None:
        self._repo = repository or Repository()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def assemble(self, trace_id: str) -> dict:
        """
        Build a tree structure for a trace.

        Returns:
            {"span": span_dict, "children": [tree_node, ...]}
            or an empty dict if no spans found.
        """
        spans = self._repo.get_trace_spans(trace_id)
        if not spans:
            log.warning("assemble_no_spans", trace_id=trace_id)
            return {}

        span_by_id: dict[str, dict] = {s["span_id"]: s for s in spans}

        # Build child map
        children_map: dict[str, list[str]] = {s["span_id"]: [] for s in spans}
        root_ids: list[str] = []

        for span in spans:
            pid = span.get("parent_span_id", "")
            if pid and pid in span_by_id:
                children_map[pid].append(span["span_id"])
            else:
                root_ids.append(span["span_id"])

        if not root_ids:
            # Fallback: pick earliest span as root
            root_ids = [spans[0]["span_id"]]

        def _build_node(span_id: str) -> dict:
            return {
                "span": span_by_id[span_id],
                "children": [
                    _build_node(child_id) for child_id in children_map[span_id]
                ],
            }

        if len(root_ids) == 1:
            return _build_node(root_ids[0])

        # Multiple roots — wrap in a synthetic root
        return {
            "span": {
                "trace_id": trace_id,
                "span_id": "__root__",
                "span_name": "__synthetic_root__",
            },
            "children": [_build_node(rid) for rid in root_ids],
        }

    def get_task_spans(self, trace_id: str) -> list[dict]:
        """Return only agent.task spans for the trace."""
        spans = self._repo.get_trace_spans(trace_id)
        return [s for s in spans if s.get("span_name") == "agent.task"]

    def get_tool_spans(self, trace_id: str) -> list[dict]:
        """Return only agent.tool_call spans for the trace."""
        spans = self._repo.get_trace_spans(trace_id)
        return [s for s in spans if s.get("span_name") == "agent.tool_call"]

    def get_handoff_spans(self, trace_id: str) -> list[dict]:
        """Return only agent.handoff spans for the trace."""
        spans = self._repo.get_trace_spans(trace_id)
        return [s for s in spans if s.get("span_name") == "agent.handoff"]

    def extract_eval_targets(
        self,
        trace_id: str,
        hint_spans: list[dict] | None = None,
    ) -> dict:
        """
        Return categorised spans for the eval pipeline.

        Queries ClickHouse for spans. If the result is empty (ClickHouse batch
        hasn't flushed yet), falls back to hint_spans from the OTLP payload.

        Keys: task_spans, tool_spans, handoff_spans, llm_spans, all_spans
        """
        all_spans = self._repo.get_trace_spans(trace_id)

        # Fallback: use in-flight spans if ClickHouse hasn't flushed yet
        if not all_spans and hint_spans:
            all_spans = [
                s for s in hint_spans
                if s.get("trace_id") == trace_id
            ]
            if all_spans:
                log.info(
                    "extract_eval_targets_used_hint",
                    trace_id=trace_id,
                    span_count=len(all_spans),
                )

        task_spans = [s for s in all_spans if s.get("span_name") == "agent.task"]
        tool_spans = [s for s in all_spans if s.get("span_name") == "agent.tool_call"]
        handoff_spans = [s for s in all_spans if s.get("span_name") == "agent.handoff"]
        # call_llm is ADK's wrapper span — its child openai.chat carries the
        # same tokens. Excluding call_llm prevents double-counting when both
        # ADK and OpenAI instrumentation spans are present in the same trace.
        _llm_wrapper_spans = {"call_llm"}
        llm_spans = [
            s
            for s in all_spans
            if s.get("span_name") not in _llm_wrapper_spans
            and (
                s.get("span_name") == "agent.llm_call"
                or "llm" in s.get("span_name", "").lower()
                or "chat" in s.get("span_name", "").lower()
            )
        ]

        return {
            "task_spans": task_spans,
            "tool_spans": tool_spans,
            "handoff_spans": handoff_spans,
            "llm_spans": llm_spans,
            "all_spans": all_spans,
        }
