"""Chat transports. Each one is optional and started only when configured."""

from __future__ import annotations

from .base import ChatEvent, ChatRouter


def build_transport(name: str, gateway, settings: dict):
    if name == 'discord':
        try:
            from .discord import DiscordTransport
        except ImportError:
            raise RuntimeError(
                "the Discord transport needs: the 'discord' extra, e.g. pip install 'peerpass[discord] @ git+https://github.com/joey12725/peerpass'"
            ) from None
        return DiscordTransport(gateway, settings)
    if name == 'slack':
        from .slack import SlackTransport

        return SlackTransport(gateway, settings)
    if name == 'telegram':
        from .telegram import TelegramTransport

        return TelegramTransport(gateway, settings)
    if name == 'matrix':
        from .matrix import MatrixTransport

        return MatrixTransport(gateway, settings)
    raise ValueError(f'unknown transport: {name}')


__all__ = ['ChatEvent', 'ChatRouter', 'build_transport']
