"""Netz NO Smartmeter sensor platform."""

from datetime import timedelta

from homeassistant import config_entries, core

from .const import CONF_ENERGY_COMMUNITY, CONF_METERING_POINTS, DOMAIN
from .netznoe_sensor import NetzNoeSensor

# Time between updating data from Netz NO (every hour)
SCAN_INTERVAL = timedelta(hours=1)


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
):
    """Set up sensors from a config entry created in the integrations UI."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    async_smartmeter = entry_data["client"]
    config = entry_data["config"]
    energy_community = entry_data[CONF_ENERGY_COMMUNITY]

    entities = []
    for metering_point in config.get(CONF_METERING_POINTS, []):
        sensor = NetzNoeSensor(
            async_smartmeter, metering_point, energy_community=energy_community
        )
        entities.append(sensor)
        entities.extend(sensor.create_energy_community_sensors())

    async_add_entities(entities, update_before_add=False)
