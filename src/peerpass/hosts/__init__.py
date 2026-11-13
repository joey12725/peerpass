"""Host adapters: how the gateway reaches the agent it protects."""

from __future__ import annotations

from .base import HostAdapter, HostError, HostRequest, build_host

__all__ = ['HostAdapter', 'HostError', 'HostRequest', 'build_host']
