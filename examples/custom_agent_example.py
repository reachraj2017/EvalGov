"""
Example: Custom Python Agent with Manual Span Instrumentation
============================================================
Demonstrates using the canonical span wrappers directly
for a fully custom agent implementation not using any framework.

Shows all span types: task, tool_call, handoff, memory, decision
"""

import os
import sys
import time
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Initialise telemetry first
# ---------------------------------------------------------------------------
from instrumentation import (
    init_telemetry,
    agent_task,
    agent_tool_call,
    agent_handoff,
    agent_memory,
    agent_decision,
)

run_id = init_telemetry(
    service_name="custom-agent-example",
)
print(f"[telemetry] run_id={run_id}")


# ---------------------------------------------------------------------------
# Simulated tool functions  (no real API calls - just print + sleep)
# ---------------------------------------------------------------------------

def fetch_weather_api(city: str, unit: str) -> dict:
    """Fake weather API call."""
    time.sleep(0.05)  # simulate network latency
    temp = round(random.uniform(5, 35), 1)
    if unit == "fahrenheit":
        temp = round(temp * 9 / 5 + 32, 1)
    return {"city": city, "temperature": temp, "unit": unit, "condition": "partly cloudy"}


def fetch_forecast_api(city: str, days: int) -> dict:
    """Fake multi-day forecast API call."""
    time.sleep(0.05)
    return {
        "city": city,
        "forecast": [
            {"day": i + 1, "high": round(random.uniform(10, 30), 1), "low": round(random.uniform(0, 15), 1)}
            for i in range(days)
        ],
    }


# ---------------------------------------------------------------------------
# WeatherAgent
# ---------------------------------------------------------------------------

class WeatherAgent:
    """
    A fully custom weather reporting agent that demonstrates all span types.
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        # In-memory store simulating shared memory
        self._memory: dict = {}

    def run(self, city: str) -> str:
        """
        Fetch weather for *city*, make a unit decision, store results, and
        return a human-readable weather report string.

        Emits: agent.task, agent.decision, agent.tool_call (x2), agent.memory (x2)
        """
        print(f"\n[{self.agent_id}] Starting weather task for city='{city}'")

        with agent_task(
            agent_id=self.agent_id,
            role="weather_reporter",
            task_input=f"Get current weather and 3-day forecast for {city}",
        ) as task:

            # --- Decision: which temperature unit? ---
            print(f"  [{self.agent_id}] Making unit decision...")
            unit = self._decide_unit(city)

            # --- Tool call 1: current weather ---
            print(f"  [{self.agent_id}] Calling fetch_weather_api (tool_call span)...")
            with agent_tool_call(
                agent_id=self.agent_id,
                tool_name="fetch_weather_api",
                tool_input={"city": city, "unit": unit},
            ) as tool:
                weather = fetch_weather_api(city, unit)
                tool.set_output(weather)
                tool.set_success(True)
            print(f"  [{self.agent_id}] Current weather: {weather}")

            # --- Tool call 2: 3-day forecast ---
            print(f"  [{self.agent_id}] Calling fetch_forecast_api (tool_call span)...")
            with agent_tool_call(
                agent_id=self.agent_id,
                tool_name="fetch_forecast_api",
                tool_input={"city": city, "days": 3},
            ) as tool:
                forecast = fetch_forecast_api(city, days=3)
                tool.set_output(forecast)
                tool.set_success(True)
            print(f"  [{self.agent_id}] Forecast: {forecast['forecast']}")

            # --- Memory write: store result in local memory ---
            print(f"  [{self.agent_id}] Writing result to memory (memory span)...")
            with agent_memory(
                agent_id=self.agent_id,
                op="write",
                key=f"weather:{city}",
                scope="local",
            ):
                self._memory[f"weather:{city}"] = {
                    "current": weather,
                    "forecast": forecast,
                }

            # --- Memory read: read it back (simulates another component reading) ---
            print(f"  [{self.agent_id}] Reading result from memory (memory span)...")
            with agent_memory(
                agent_id=self.agent_id,
                op="read",
                key=f"weather:{city}",
                scope="local",
            ):
                stored = self._memory.get(f"weather:{city}", {})

            # Build report string
            report = (
                f"Weather for {city}: "
                f"{stored['current']['temperature']}°{unit[0].upper()} "
                f"{stored['current']['condition']}. "
                f"3-day forecast highs: "
                + ", ".join(
                    f"Day {d['day']}: {d['high']}°"
                    for d in stored["forecast"]["forecast"]
                )
            )

            task.set_output(report)
            task.set_status("success")

        print(f"  [{self.agent_id}] Task complete.")
        return report

    def _decide_unit(self, city: str) -> str:
        """
        Decide whether to use Celsius or Fahrenheit.
        Uses US/non-US heuristic as the decision logic.
        Wrapped in an agent.decision span.
        """
        options = ["celsius", "fahrenheit"]
        us_cities = {"new york", "los angeles", "chicago", "houston", "phoenix"}
        chosen = "fahrenheit" if city.lower() in us_cities else "celsius"
        reason = (
            "City is in the United States; using Fahrenheit per local convention."
            if chosen == "fahrenheit"
            else "City is outside the United States; using Celsius (SI standard)."
        )

        with agent_decision(
            agent_id=self.agent_id,
            decision_input=f"Which temperature unit should be used for {city}?",
            options=options,
            chosen=chosen,
            reason=reason,
        ):
            pass  # Decision is instantaneous; span just records the reasoning

        return chosen


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class WeatherOrchestrator:
    """
    Runs two WeatherAgent instances for different cities,
    then hands off the first agent's result to the second.
    """

    def __init__(self):
        self._agent_a = WeatherAgent(agent_id="weather-agent-us-1")
        self._agent_b = WeatherAgent(agent_id="weather-agent-eu-1")

    def run(self, city_a: str, city_b: str) -> None:
        print(f"\n[Orchestrator] Running WeatherOrchestrator for '{city_a}' and '{city_b}'")

        # Run first agent
        report_a = self._agent_a.run(city_a)

        # Handoff: pass city_a summary to the EU agent as context
        print(f"\n[Orchestrator] Emitting handoff span: {self._agent_a.agent_id} → {self._agent_b.agent_id}")
        with agent_handoff(
            from_agent_id=self._agent_a.agent_id,
            to_agent_id=self._agent_b.agent_id,
            reason="cross_regional_comparison",
            context_payload={"reference_city": city_a, "reference_report": report_a},
        ):
            # Run second agent inside handoff span so it nests correctly
            report_b = self._agent_b.run(city_b)

        print("\n" + "=" * 60)
        print("WEATHER REPORTS")
        print("=" * 60)
        print(f"{city_a}: {report_a}")
        print(f"{city_b}: {report_b}")
        print("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    orchestrator = WeatherOrchestrator()
    orchestrator.run(city_a="New York", city_b="London")
    print(f"\n[done] run_id={run_id}")
    print(f"       Search for run_id='{run_id}' in Grafana to see all spans:")
    print("         agent.task, agent.tool_call, agent.handoff, agent.memory, agent.decision")


if __name__ == "__main__":
    main()
