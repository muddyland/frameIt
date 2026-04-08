"""Fixtures for the FrameIT agent test suite."""
import os
import pytest

# Env vars must be set before agent.py is imported
os.environ.setdefault('FRAMEIT_SERVER', 'http://localhost:5000')
os.environ.setdefault('FRAMEIT_TOKEN', 'test-token-abc123')
os.environ.setdefault('AGENT_PORT', '5001')

from agent.agent import app as agent_app  # noqa: E402


@pytest.fixture(scope='session')
def app():
    agent_app.config['TESTING'] = True
    yield agent_app


@pytest.fixture
def client(app):  # noqa: redefined-outer-name
    return app.test_client()


@pytest.fixture
def auth_headers():
    """Valid Bearer token header."""
    return {'Authorization': f'Bearer {os.environ["FRAMEIT_TOKEN"]}'}


@pytest.fixture
def bad_headers():
    """Invalid Bearer token header."""
    return {'Authorization': 'Bearer wrong-token'}
