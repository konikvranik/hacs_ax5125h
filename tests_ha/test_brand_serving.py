"""Servírování brand ikon přes /api/brands/integration/... (HA 2026.10+).

Reprodukuje živý dotaz frontendu na integration tile. Ověří, že:
- icon.png/logo.png/icon@2x.png/dark_icon.png se servírují z lokální
  brand/ složky custom integrace (has_branding),
- zasetý 404 marker v disk cache (z doby před 0.2.4, kdy brand/ nebyla)
  NEVYHRADÍ nad lokální souborem (step 1 custom → step 2 cache/CDN).
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_brands_view_serves_local_brand_files(
    hass: HomeAssistant,
    enable_custom_integrations,
    hass_client,
) -> None:
    """Všechny 8 obrazů z brand/ jsou servírované lokálně."""
    assert await async_setup_component(hass, "brands", {})
    await hass.async_block_till_done()

    token = hass.data["brands"][-1]
    client = await hass_client()

    for image in (
        "icon.png",
        "logo.png",
        "icon@2x.png",
        "dark_icon.png",
        "dark_logo.png",
    ):
        resp = await client.get(
            f"/api/brands/integration/ax5125h/{image}?token={token}"
        )
        assert resp.status == 200, f"{image}: HTTP {resp.status}"
        assert resp.content_type == "image/png"
        data = await resp.read()
        assert len(data) > 500, f"{image}: podezřele malý obsah ({len(data)} B)"


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_brands_view_local_wins_over_cached_404(
    hass: HomeAssistant,
    enable_custom_integrations,
    hass_client,
) -> None:
    """404 marker v cache (prázdný soubor) nebere přednost lokálnímu souboru."""
    from pathlib import Path

    assert await async_setup_component(hass, "brands", {})
    await hass.async_block_till_done()

    # zaseť 404 marker — přesně co vzniklo při běhu 0.2.3 bez brand/ složky
    cache_file = Path(hass.config.cache_path("brands")) / (
        "integrations/ax5125h/icon.png"
    )
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"")

    token = hass.data["brands"][-1]
    client = await hass_client()
    resp = await client.get(
        f"/api/brands/integration/ax5125h/icon.png?token={token}"
    )
    assert resp.status == 200
    assert resp.content_type == "image/png"
    assert len(await resp.read()) > 500, "vrátil se 404 marker místo lokální ikony"
