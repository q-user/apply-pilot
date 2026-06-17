"""Orders persistence layer inside feature slice."""

from sqlalchemy.orm import Session

from apply_pilot.features.orders.models import Order


class OrdersRepository:
    def __init__(self, db: Session) -> None:
        self._db = db

    def create(self, customer_name: str) -> Order:
        order = Order(customer_name=customer_name, status="new")
        self._db.add(order)
        self._db.commit()
        self._db.refresh(order)
        return order
