"""
SDK-specific attribute normalizer.
Maps framework-native span attributes to the canonical schema.
Called before writing spans to ClickHouse.
"""

import json
from typing import Dict, Any


class SpanNormalizer:
    """
    Normalizes raw OTel span attribute dicts (as captured from the collector)
    into the canonical aieval span schema.

    Typical usage inside an OTel span processor or ClickHouse writer:

        normalizer = SpanNormalizer()
        canonical_span = normalizer.normalize(raw_span_dict)
    """

    # Attribute key sets that are characteristic of each SDK
    _LANGCHAIN_KEYS = frozenset(
        ["langchain.request.type", "langchain.inputs", "langchain.outputs"]
    )
    _AUTOGEN_KEYS = frozenset(["autogen.agent_name", "autogen.agent_type"])
    _CREWAI_KEYS = frozenset(
        ["crewai.agent.role", "crewai.task.description", "crewai.task.output"]
    )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def normalize(self, span: Dict[str, Any]) -> Dict[str, Any]:
        """
        Detect the SDK source and apply the appropriate attribute mapping.

        Args:
            span: Raw span dict with an "attributes" sub-dict (or flat attributes).

        Returns:
            A new span dict with canonical attribute names added/overwritten.
            The original SDK-native keys are preserved alongside the canonical ones.
        """
        sdk = self._detect_sdk(span)
        dispatch = {
            "langchain": self._normalize_langchain,
            "autogen": self._normalize_autogen,
            "crewai": self._normalize_crewai,
            "generic": self._normalize_generic,
        }
        handler = dispatch.get(sdk, self._normalize_generic)
        normalized = handler(span)
        # Tag with the detected SDK for downstream queries
        normalized = self._set_attr(normalized, "aieval.sdk_source", sdk)
        return normalized

    def _detect_sdk(self, span: Dict[str, Any]) -> str:
        """
        Inspect span attributes to determine which SDK produced this span.

        Returns one of: "langchain", "autogen", "crewai", "anthropic", "openai", "generic"
        """
        attrs = self._get_attrs(span)

        if self._LANGCHAIN_KEYS & set(attrs):
            return "langchain"
        if self._AUTOGEN_KEYS & set(attrs):
            return "autogen"
        if self._CREWAI_KEYS & set(attrs):
            return "crewai"

        # OpenLLMetry uses "llm.vendor" or "gen_ai.system" to identify the backend
        vendor = str(attrs.get("llm.vendor", "") or attrs.get("gen_ai.system", "")).lower()
        if "anthropic" in vendor:
            return "anthropic"
        if "openai" in vendor:
            return "openai"

        return "generic"

    # -----------------------------------------------------------------------
    # SDK-specific normalizers
    # -----------------------------------------------------------------------

    def _normalize_langchain(self, span: Dict[str, Any]) -> Dict[str, Any]:
        """
        LangChain mapping:
          langchain.request.type  -> agent.role
          langchain.inputs        -> task.input
          langchain.outputs       -> task.output
        """
        result = self._copy_span(span)
        attrs = self._get_attrs(result)

        if "langchain.request.type" in attrs:
            attrs["agent.role"] = str(attrs["langchain.request.type"])

        if "langchain.inputs" in attrs:
            raw = attrs["langchain.inputs"]
            attrs["task.input"] = raw if isinstance(raw, str) else json.dumps(raw)

        if "langchain.outputs" in attrs:
            raw = attrs["langchain.outputs"]
            attrs["task.output"] = raw if isinstance(raw, str) else json.dumps(raw)

        result = self._put_attrs(result, attrs)
        return result

    def _normalize_autogen(self, span: Dict[str, Any]) -> Dict[str, Any]:
        """
        AutoGen mapping:
          autogen.agent_name -> agent.id
          autogen.agent_type -> agent.role
        """
        result = self._copy_span(span)
        attrs = self._get_attrs(result)

        if "autogen.agent_name" in attrs:
            attrs["agent.id"] = str(attrs["autogen.agent_name"])

        if "autogen.agent_type" in attrs:
            attrs["agent.role"] = str(attrs["autogen.agent_type"])

        result = self._put_attrs(result, attrs)
        return result

    def _normalize_crewai(self, span: Dict[str, Any]) -> Dict[str, Any]:
        """
        CrewAI mapping:
          crewai.agent.role        -> agent.role
          crewai.task.description  -> task.input
          crewai.task.output       -> task.output
        """
        result = self._copy_span(span)
        attrs = self._get_attrs(result)

        if "crewai.agent.role" in attrs:
            attrs["agent.role"] = str(attrs["crewai.agent.role"])

        if "crewai.task.description" in attrs:
            raw = attrs["crewai.task.description"]
            attrs["task.input"] = raw if isinstance(raw, str) else json.dumps(raw)

        if "crewai.task.output" in attrs:
            raw = attrs["crewai.task.output"]
            attrs["task.output"] = raw if isinstance(raw, str) else json.dumps(raw)

        result = self._put_attrs(result, attrs)
        return result

    def _normalize_generic(self, span: Dict[str, Any]) -> Dict[str, Any]:
        """
        Passthrough normalizer - returns the span unchanged (besides SDK tag added by normalize()).
        Used for Anthropic/OpenAI spans that are already canonical via OpenLLMetry,
        and for any unknown span types.
        """
        return self._copy_span(span)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _get_attrs(span: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract the attributes dict from a span.
        Supports both {"attributes": {...}} and flat dicts.
        """
        if "attributes" in span and isinstance(span["attributes"], dict):
            return dict(span["attributes"])
        # Flat span dict - treat everything except known meta-keys as attributes
        meta_keys = {"name", "trace_id", "span_id", "parent_span_id", "start_time",
                     "end_time", "status", "kind", "resource", "events", "links"}
        return {k: v for k, v in span.items() if k not in meta_keys}

    @staticmethod
    def _put_attrs(span: Dict[str, Any], attrs: Dict[str, Any]) -> Dict[str, Any]:
        """Write the updated attributes dict back into the span structure."""
        result = dict(span)
        if "attributes" in span and isinstance(span["attributes"], dict):
            result["attributes"] = attrs
        else:
            meta_keys = {"name", "trace_id", "span_id", "parent_span_id", "start_time",
                         "end_time", "status", "kind", "resource", "events", "links"}
            for k in list(result.keys()):
                if k not in meta_keys:
                    del result[k]
            result.update(attrs)
        return result

    @staticmethod
    def _set_attr(span: Dict[str, Any], key: str, value: Any) -> Dict[str, Any]:
        """Set a single attribute on a span regardless of its structure."""
        result = dict(span)
        if "attributes" in span and isinstance(span["attributes"], dict):
            result["attributes"] = dict(span["attributes"])
            result["attributes"][key] = value
        else:
            result[key] = value
        return result

    @staticmethod
    def _copy_span(span: Dict[str, Any]) -> Dict[str, Any]:
        """Shallow-copy a span (deep-copying the attributes dict)."""
        result = dict(span)
        if "attributes" in span and isinstance(span["attributes"], dict):
            result["attributes"] = dict(span["attributes"])
        return result
