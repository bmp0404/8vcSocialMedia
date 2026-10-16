"""Unit tests never touch the network.

A test that reaches a real client (an fxtwitter reader, an HTTP source) fails here instead of
making a live call against a shared rate limit. Subprocess tests such as test_smoke.py run in
their own process and are not covered.
"""

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def loopback(address) -> bool:
        # Context's cache tests connect to 127.0.0.1:9 to get a real "connection refused". Loopback
        # never leaves the machine or touches a shared rate limit, so it stays allowed.
        return isinstance(address, tuple) and str(address[0]) in ("127.0.0.1", "::1", "localhost")

    def refuse(self, address, *args, **kwargs):
        if loopback(address):
            return real_connect(self, address, *args, **kwargs)
        raise RuntimeError(f"unit test tried to open a network connection to {address}; inject a fake instead")

    def refuse_ex(self, address, *args, **kwargs):
        if loopback(address):
            return real_connect_ex(self, address, *args, **kwargs)
        return refuse(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_ex)
    # Once baml_sdk is generated, Action's veto calls Dhilan's post_judge for grounding, and its
    # default provider (claude_code) runs the local Claude Code client in a subprocess, which the
    # socket guard doesn't see: test_provider_without_prefill_uses_styled hung there. A test that
    # wants the grounding veto injects a fake checker.
    monkeypatch.setenv("HARNESS_GROUNDING", "off")


@pytest.fixture(autouse=True)
def no_account_settings(monkeypatch):
    # A developer's .env can name another brand account or framing file, and config.py loads it.
    # Unit tests read the repo's mission/observe.toml and action/reply_framing.json instead.
    for name in ("HARNESS_ACCOUNT_HANDLE", "HARNESS_REPLY_FRAMING", "HARNESS_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
