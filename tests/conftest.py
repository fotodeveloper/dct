import pytest

from dct.db import get_engine, create_tables


@pytest.fixture
def engine():
    """In-memory SQLite engine with all tables created."""
    eng = get_engine("sqlite://")
    create_tables(eng)
    return eng


@pytest.fixture
def seed_project(engine):
    """Insert a test project and return its id."""
    from dct.core.projects import create_project

    return create_project(engine, slug="test", name="Test Project", path="/tmp/test")
