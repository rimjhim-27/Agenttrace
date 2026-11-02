import pytest

from agenttrace import SQLiteStore, Tracer, clear_prices


@pytest.fixture
def tracer():
    t = Tracer(SQLiteStore(":memory:"))
    yield t
    t.close()
    t.store.close()
    clear_prices()
