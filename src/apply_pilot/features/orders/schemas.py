"""Orders DTOs."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CreateOrderInput:
    customer_name: str


@dataclass(frozen=True)
class OrderDTO:
    id: int
    customer_name: str
    status: str
