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
