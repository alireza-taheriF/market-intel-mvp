"""Fault injection used to demonstrate controlled failure handling.

The clients for search, web fetching, the LLM and SMTP all receive a
FaultConfig. When a flag is set they raise the same typed error a real outage
would produce, so the whole error path (pipeline -> partial result -> UI
message) is exercised without having to unplug the network.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FaultConfig:
    network_down: bool = False
    search_timeout: bool = False
    llm_unavailable: bool = False
    llm_invalid_response: bool = False
    smtp_failure: bool = False

    @property
    def active(self) -> bool:
        return any(vars(self).values())


SCENARIOS: dict[str, FaultConfig] = {
    "None (real services)": FaultConfig(),
    "Internet down (search + web pages unreachable)": FaultConfig(network_down=True),
    "Search API timeout": FaultConfig(search_timeout=True),
    "AI model unavailable": FaultConfig(llm_unavailable=True),
    "AI model returns invalid response": FaultConfig(llm_invalid_response=True),
    "SMTP failure while sending email": FaultConfig(smtp_failure=True),
}
