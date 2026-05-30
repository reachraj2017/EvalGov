"""Shared sidebar controls: time window selector + refresh button."""

from datetime import datetime, timezone, timedelta
import streamlit as st

_PRESETS = {
    "Last 1 hour":   timedelta(hours=1),
    "Last 6 hours":  timedelta(hours=6),
    "Last 24 hours": timedelta(hours=24),
    "Last 7 days":   timedelta(days=7),
    "Last 30 days":  timedelta(days=30),
}
_PRESET_KEYS = list(_PRESETS.keys())


def render_time_controls(key_prefix: str = "page") -> tuple[datetime, datetime]:
    """
    Render time-window selector and refresh button in the sidebar.
    Returns (start, end) as UTC-aware datetimes.
    """
    with st.sidebar:
        st.divider()
        st.markdown("**Time Window**")
        preset = st.selectbox(
            "Show data from",
            _PRESET_KEYS,
            index=2,  # default: Last 24 hours
            key=f"{key_prefix}_time_preset",
        )
        if st.button("↻ Refresh", key=f"{key_prefix}_refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    now   = datetime.now(timezone.utc)
    start = now - _PRESETS[preset]
    return start, now
