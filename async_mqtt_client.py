"""Async wrappings for mqtt client."""

from __future__ import annotations

from functools import lru_cache
from types import TracebackType
from typing import Self

from paho.mqtt.client import Client as MQTTClient
import aiohttp
import logging

_MQTT_LOCK_COUNT = 7
_LOGGER = logging.getLogger(__name__)


class NullLock:
    """Null lock."""

    @lru_cache(maxsize=_MQTT_LOCK_COUNT)
    def __enter__(self) -> Self:
        """Enter the lock."""
        return self

    @lru_cache(maxsize=_MQTT_LOCK_COUNT)
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Exit the lock."""

    @lru_cache(maxsize=_MQTT_LOCK_COUNT)
    def acquire(self, blocking: bool = False, timeout: int = -1) -> None:
        """Acquire the lock."""

    @lru_cache(maxsize=_MQTT_LOCK_COUNT)
    def release(self) -> None:
        """Release the lock."""


class AsyncMQTTClient(MQTTClient):
    """Async MQTT Client.

    Wrapper around paho.mqtt.client.Client to remove the locking
    that is not needed since we are running in an async event loop.
    """

    def setup(self) -> None:
        """Set up the client.

        All the threading locks are replaced with NullLock
        since the client is running in an async event loop
        and will never run in multiple threads.
        """
        self._in_callback_mutex = NullLock()
        self._callback_mutex = NullLock()
        self._msgtime_mutex = NullLock()
        self._out_message_mutex = NullLock()
        self._in_message_mutex = NullLock()
        self._reconnect_delay_mutex = NullLock()
        self._mid_generate_mutex = NullLock()

    async def check_plugin_version(self, url):
        """检查插件版本"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{url}/api/plugin/version", ssl=False) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("version", "unknown")
                    else:
                        _LOGGER.error(f"Version check failed with status {response.status}")
                        return None
        except Exception as e:
            _LOGGER.error(f"check plugin version err: {str(e)}")
            return None
