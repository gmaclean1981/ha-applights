import pytest


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not connect to real Bluetooth")

    monkeypatch.setattr("custom_components.applights.controller.establish_connection", forbidden)
