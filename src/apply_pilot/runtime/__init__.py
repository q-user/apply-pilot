"""Runtime helpers: Redis client factory and long-running process skeleton.

This package hosts the small infrastructure pieces that background workers
(arbitrage scanner, scheduler, notification sender, ...) share: a single
Redis client factory and a ``BaseProcess`` helper that wires up graceful
SIGINT/SIGTERM shutdown.
"""

from apply_pilot.runtime.process import BaseProcess
from apply_pilot.runtime.redis_client import create_redis_client

__all__ = ["BaseProcess", "create_redis_client"]
