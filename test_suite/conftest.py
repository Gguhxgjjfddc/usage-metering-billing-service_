from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Tenant


@pytest.fixture()
def db_session():
    """Fresh in-memory SQLite DB per test -- fully isolated, no shared state."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def free_tenant(db_session):
    tenant = Tenant(name="Acme Free", email="free@acme.test", plan_name="free")
    db_session.add(tenant)
    db_session.commit()
    db_session.refresh(tenant)
    return tenant


@pytest.fixture()
def pro_tenant(db_session):
    tenant = Tenant(name="Acme Pro", email="pro@acme.test", plan_name="pro")
    db_session.add(tenant)
    db_session.commit()
    db_session.refresh(tenant)
    return tenant
