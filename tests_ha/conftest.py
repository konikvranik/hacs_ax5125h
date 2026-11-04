"""Conftest pro HA harness testy (tests_ha/).

- phcc importuje vlastní testing_config/custom_components balíček (regular
  package s __init__.py), který přebije namespace package z repa — proto
  repo custom_components explicitně přidáme do __path__, aby loader
  (_get_custom_components -> import custom_components) našel ax5125h.
- phcc 0.13.375 dodává testing_config bez configuration.yaml, který MQTT
  mock entry setup čte — doplníme ho (autouse, po hass fixture).
"""

import sys
from pathlib import Path

pytest_plugins = "pytest_homeassistant_custom_component"

ROOT = Path(__file__).resolve().parents[1]

import custom_components  # noqa: E402  (phcc testing_config balíček)

custom_components.__path__.append(str(ROOT / "custom_components"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_configuration_yaml(hass):
    """Minimal configuration.yaml, pokud testing_config žádný nemá."""
    path = Path(hass.config.config_dir) / "configuration.yaml"
    if not path.exists():
        path.write_text("")
    yield
