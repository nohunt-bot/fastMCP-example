import pytest


@pytest.fixture
def anyio_backend():
    """Run the async tests on asyncio only (trio is not a dependency here)."""
    return "asyncio"
