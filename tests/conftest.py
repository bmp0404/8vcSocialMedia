"""Unit tests never touch the network.

A test that reaches a real client (an fxtwitter reader, an HTTP source) fails here instead of
making a live call against a shared rate limit. Subprocess tests such as test_smoke.py run in
their own process and are not covered.
"""

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(self, address, *args, **kwargs):
        raise RuntimeError(f"unit test tried to open a network connection to {address}; inject a fake instead")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    # Once baml_sdk is generated, Action's veto calls Dhilan's post_judge for grounding, and its
    # default provider (claude_code) runs the local Claude Code client in a subprocess, which the
    # socket guard doesn't see: test_provider_without_prefill_uses_styled hung there. A test that
    # wants the grounding veto injects a fake checker.
    monkeypatch.setenv("HARNESS_GROUNDING", "off")
