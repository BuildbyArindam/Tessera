"""Shared fixtures for unit tests."""

import socket
from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Any attempt to open a socket fails the test. Day 9: no network call may happen in unit tests."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access attempted in a unit test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield
