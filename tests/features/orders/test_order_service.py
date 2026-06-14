from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base
from job_apply.features.orders.models import Order
from job_apply.features.orders.repositories import OrdersRepository
from job_apply.features.orders.schemas import CreateOrderInput
from job_apply.features.orders.service import OrdersService


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
