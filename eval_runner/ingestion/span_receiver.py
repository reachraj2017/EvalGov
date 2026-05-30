"""
Parses incoming OTLP payloads (JSON or protobuf) into normalized span dicts.
Maps to canonical schema attributes.
"""

import gzip
import json
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)


class SpanReceiver:
    """Converts OTLP payloads into a list of normalized span dicts."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_otlp_http_body(self, body: bytes, content_type: str) -> list[dict]:
        """
        Entry point for HTTP /v1/traces.

        Tries protobuf first when the content-type signals it; otherwise
        falls back to JSON parsing.
        """
        ct = (content_type or "").lower()

        # Decompress gzip body (otelcol sends compressed by default)
        if body[:2] == b'\x1f\x8b':
            try:
                body = gzip.decompress(body)
            except Exception as gz_exc:
                log.warning("gzip_decompress_failed", error=str(gz_exc))

        if "protobuf" in ct or "x-protobuf" in ct:
            try:
                return self._parse_protobuf(body)
            except Exception as proto_exc:
                log.warning(
                    "protobuf_parse_failed_falling_back_to_json",
                    error=str(proto_exc),
                )

        # JSON fallback
        try:
            payload = json.loads(body)
            return self.parse_otlp_json(payload)
        except Exception as json_exc:
            log.error("otlp_json_parse_failed", error=str(json_exc))
            return []

    def parse_otlp_json(self, payload: dict) -> list[dict]:
        """
        Parse OTLP JSON export format.

        Schema:
            payload.resourceSpans[].scopeSpans[].spans[]
        """
        spans: list[dict] = []
        try:
            resource_spans = payload.get("resourceSpans", [])
            for rs in resource_spans:
                service_name = self._extract_service_name(
                    rs.get("resource", {}).get("attributes", [])
                )
                scope_spans = rs.get("scopeSpans", [])
                for ss in scope_spans:
                    for raw_span in ss.get("spans", []):
                        normalized = self.normalize_span(raw_span, service_name)
                        spans.append(normalized)
        except Exception as exc:
            log.error("parse_otlp_json_error", error=str(exc))
        log.debug("otlp_json_parsed", span_count=len(spans))
        return spans

    def normalize_span(self, raw_span: dict, service_name: str = "") -> dict:
        """
        Convert a raw OTLP span dict to our canonical representation.

        Canonical keys:
            trace_id, span_id, parent_span_id, span_name,
            service_name, duration_ns, status_code,
            attributes (dict), timestamp (datetime)
        """
        try:
            start_ns = int(raw_span.get("startTimeUnixNano", 0) or 0)
            end_ns = int(raw_span.get("endTimeUnixNano", 0) or 0)
            duration_ns = max(0, end_ns - start_ns)

            timestamp = (
                datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc)
                if start_ns
                else datetime.now(timezone.utc)
            )

            status = raw_span.get("status", {})
            status_code = self._normalize_status_code(status)

            attributes = self._flatten_attributes(
                raw_span.get("attributes", [])
            )

            return {
                "trace_id": raw_span.get("traceId", ""),
                "span_id": raw_span.get("spanId", ""),
                "parent_span_id": raw_span.get("parentSpanId", ""),
                "span_name": raw_span.get("name", ""),
                "service_name": service_name,
                "duration_ns": duration_ns,
                "status_code": status_code,
                "attributes": attributes,
                "timestamp": timestamp,
            }
        except Exception as exc:
            log.error("normalize_span_error", error=str(exc), raw_span=str(raw_span)[:200])
            return {
                "trace_id": raw_span.get("traceId", ""),
                "span_id": raw_span.get("spanId", ""),
                "parent_span_id": "",
                "span_name": raw_span.get("name", "unknown"),
                "service_name": service_name,
                "duration_ns": 0,
                "status_code": "STATUS_CODE_UNSET",
                "attributes": {},
                "timestamp": datetime.now(timezone.utc),
            }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_protobuf(self, body: bytes) -> list[dict]:
        """
        Attempt protobuf parsing via opentelemetry-proto generated classes.
        Falls through to exception on failure so caller can try JSON.
        """
        try:
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                ExportTraceServiceRequest,
            )

            request = ExportTraceServiceRequest()
            request.ParseFromString(body)

            spans: list[dict] = []
            for rs in request.resource_spans:
                service_name = ""
                for attr in rs.resource.attributes:
                    if attr.key == "service.name":
                        service_name = attr.value.string_value
                        break

                for ss in rs.scope_spans:
                    for pb_span in ss.spans:
                        raw = {
                            "traceId": pb_span.trace_id.hex(),
                            "spanId": pb_span.span_id.hex(),
                            "parentSpanId": pb_span.parent_span_id.hex()
                            if pb_span.parent_span_id
                            else "",
                            "name": pb_span.name,
                            "startTimeUnixNano": pb_span.start_time_unix_nano,
                            "endTimeUnixNano": pb_span.end_time_unix_nano,
                            "status": {"code": pb_span.status.code},
                            "attributes": [
                                {
                                    "key": a.key,
                                    "value": self._pb_any_to_python(a.value),
                                }
                                for a in pb_span.attributes
                            ],
                        }
                        spans.append(self.normalize_span(raw, service_name))
            return spans
        except ImportError:
            raise RuntimeError(
                "opentelemetry-proto not installed; cannot parse protobuf"
            )

    @staticmethod
    def _pb_any_to_python(any_value) -> Any:
        """Convert a protobuf AnyValue to a Python native type."""
        kind = any_value.WhichOneof("value")
        if kind == "string_value":
            return any_value.string_value
        if kind == "int_value":
            return str(any_value.int_value)
        if kind == "double_value":
            return str(any_value.double_value)
        if kind == "bool_value":
            return str(any_value.bool_value).lower()
        return ""

    @staticmethod
    def _flatten_attributes(attributes: list) -> dict:
        """
        Convert OTLP attribute list [{key, value: {stringValue/intValue/...}}]
        into a plain str->str dict.
        """
        result: dict[str, str] = {}
        for attr in attributes:
            key = attr.get("key", "")
            val_container = attr.get("value", {})
            # OTLP JSON encodes AnyValue as one of these keys:
            for field in (
                "stringValue",
                "intValue",
                "doubleValue",
                "boolValue",
                "bytesValue",
            ):
                if field in val_container:
                    result[key] = str(val_container[field])
                    break
            else:
                # Already a string (pre-flattened)
                if isinstance(val_container, str):
                    result[key] = val_container
                else:
                    result[key] = json.dumps(val_container)
        return result

    @staticmethod
    def _extract_service_name(resource_attributes: list) -> str:
        """Pull service.name from resource attributes list."""
        for attr in resource_attributes:
            if attr.get("key") == "service.name":
                val = attr.get("value", {})
                return val.get("stringValue", "")
        return ""

    @staticmethod
    def _normalize_status_code(status: dict) -> str:
        """Map OTLP status to a human-readable string."""
        code = status.get("code", 0)
        # OTLP status codes: 0=UNSET, 1=OK, 2=ERROR
        mapping = {
            0: "STATUS_CODE_UNSET",
            1: "STATUS_CODE_OK",
            2: "STATUS_CODE_ERROR",
            "STATUS_CODE_UNSET": "STATUS_CODE_UNSET",
            "STATUS_CODE_OK": "STATUS_CODE_OK",
            "STATUS_CODE_ERROR": "STATUS_CODE_ERROR",
            "Ok": "STATUS_CODE_OK",
            "Error": "STATUS_CODE_ERROR",
        }
        return mapping.get(code, "STATUS_CODE_UNSET")
