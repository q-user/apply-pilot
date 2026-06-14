"""Orders business logic."""

from job_apply.features.orders.repositories import OrdersRepository
from job_apply.features.orders.schemas import CreateOrderInput, OrderDTO


class OrdersService:
    def __init__(self, repository: OrdersRepository) -> None:
        self._repository = repository

    def create_order(self, payload: CreateOrderInput) -> OrderDTO:
        order = self._repository.create(customer_name=payload.customer_name)
        return OrderDTO(id=order.id, customer_name=order.customer_name, status=order.status)
