"""Display the latest confirmed SEM meter index."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SEMConfigEntry, SEMDataUpdateCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: SEMConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Subscribe to the coordinator; do not trigger a second initial download."""
    async_add_entities([WaterIndexSensor(entry.runtime_data)])


class WaterIndexSensor(CoordinatorEntity[SEMDataUpdateCoordinator], SensorEntity):
    """Show the latest index while Energy uses dated external statistics."""

    _attr_has_entity_name = True
    _attr_translation_key = "water_index"
    _attr_device_class = SensorDeviceClass.WATER
    # SEM can correct an already published index downwards. TOTAL preserves
    # that correction; TOTAL_INCREASING would interpret a large drop as a reset
    # and manufacture consumption. Energy still uses the separate dated series.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS

    def __init__(self, coordinator: SEMDataUpdateCoordinator):
        """Identify the meter index within its water contract."""
        super().__init__(coordinator)
        self._attr_unique_id = f"sem_water_index_{coordinator.contract_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.contract_id))},
            name=f"SEM {coordinator.contract_id}",
            manufacturer="Eau de Marseille Métropole",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://espaceclients.eaudemarseille-metropole.fr",
        )

    @property
    def native_value(self) -> float:
        """Expose the native value so HA can perform its normal unit conversion."""
        return self.coordinator.data["index"]

    @property
    def extra_state_attributes(self) -> dict[str, str | float]:
        """Expose the reading date and dated statistics source for Energy."""
        return {
            "last_known_date": self.coordinator.data["date"],
            "energy_statistic_id": self.coordinator.statistics.statistic_id,
        }
