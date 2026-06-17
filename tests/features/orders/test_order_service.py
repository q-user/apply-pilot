from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.orders.models import Order
from apply_pilot.features.orders.repositories import OrdersRepository
from apply_pilot.features.orders.schemas import CreateOrderInput
from apply_pilot.features.orders.service import OrdersService


def test_create_order_returns_created_entity() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=[Order.__table__])
    testing_session_factory = sessionmaker(bind=engine, class_=Session)

    try:
        with testing_session_factory() as db:
            service = OrdersService(OrdersRepository(db))

            result = service.create_order(CreateOrderInput(customer_name="Mikhail"))

            assert result.id > 0
            assert result.customer_name == "Mikhail"
            assert result.status == "new"
    finally:
        engine.dispose()
