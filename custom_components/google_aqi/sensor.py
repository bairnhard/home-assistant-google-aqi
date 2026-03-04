from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import asyncio
import logging
from typing import Any, Callable

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

API_URL = "https://airquality.googleapis.com/v1/currentConditions:lookup"
FORECAST_URL = "https://airquality.googleapis.com/v1/forecast:lookup"
SCAN_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True, kw_only=True)
class GoogleAQISensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    unit_fn: Callable[[dict[str, Any]], str | None] | None = None


class GoogleAQICoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch and store Google AQI data once for all entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        api_key: str,
        latitude: float,
        longitude: float,
        get_additional_info: bool,
        update_interval: int,
        forecast_interval: int,
        forecast_length: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Google AQI",
            update_interval=SCAN_INTERVAL,
        )
        self._api_key = api_key
        self._latitude = latitude
        self._longitude = longitude
        self._get_additional_info = get_additional_info
        self._update_interval = update_interval
        self._forecast_interval = forecast_interval
        self._forecast_length = forecast_length
        self._session = async_get_clientsession(hass)

        self._data: dict[str, Any] = {
            "region": None,
            "last_update": None,
            "pollutants": {},
            "indices": {},
            "forecast": [],
            "last_current_api_call": None,
            "next_current_api_call": None,
            "current_api_status": None,
            "last_forecast_api_call": None,
            "next_forecast_api_call": None,
            "forecast_api_status": None,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)

        last_current = self._data["last_current_api_call"]
        last_forecast = self._data["last_forecast_api_call"]

        if last_current:
            self._data["next_current_api_call"] = last_current + timedelta(
                hours=self._update_interval
            )
        else:
            self._data["next_current_api_call"] = now

        if last_forecast:
            self._data["next_forecast_api_call"] = last_forecast + timedelta(
                hours=self._forecast_interval
            )
        else:
            self._data["next_forecast_api_call"] = now

        tasks = []
        if not last_current or now - last_current >= timedelta(hours=self._update_interval):
            tasks.append(self._fetch_current_data())

        if not last_forecast or now - last_forecast >= timedelta(hours=self._forecast_interval):
            tasks.append(self._fetch_forecast_data())

        if tasks:
            await asyncio.gather(*tasks)

        return self._data

    async def _fetch_current_data(self) -> None:
        self._data["last_current_api_call"] = datetime.now(timezone.utc)
        payload = self._create_current_payload()
        params = {"key": self._api_key}

        try:
            async with self._session.post(API_URL, json=payload, params=params) as response:
                if response.status == 200:
                    self._process_current_data(await response.json())
                    self._data["current_api_status"] = "successful"
                else:
                    _LOGGER.error(
                        "Failed to fetch current AQI data: %s - %s",
                        response.status,
                        await response.text(),
                    )
                    self._data["current_api_status"] = "error"
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Error fetching current AQI data: %s", err)
            self._data["current_api_status"] = "error"

    async def _fetch_forecast_data(self) -> None:
        self._data["last_forecast_api_call"] = datetime.now(timezone.utc)

        now = datetime.now(timezone.utc)
        next_full_hour = (now + timedelta(hours=1)).replace(
            minute=0,
            second=0,
            microsecond=0,
        )

        payload = self._create_forecast_payload(next_full_hour)
        params = {"key": self._api_key}

        try:
            async with self._session.post(
                FORECAST_URL,
                json=payload,
                params=params,
            ) as response:
                if response.status == 200:
                    self._process_forecast_data(await response.json())
                    self._data["forecast_api_status"] = "successful"
                else:
                    _LOGGER.error(
                        "Failed to fetch forecast data: %s - %s",
                        response.status,
                        await response.text(),
                    )
                    self._data["forecast_api_status"] = "error"
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Error fetching forecast data: %s", err)
            self._data["forecast_api_status"] = "error"

    def _process_current_data(self, data: dict[str, Any]) -> None:
        if not data:
            _LOGGER.warning("Received empty current data from API")
            return

        indices: dict[str, dict[str, Any]] = {}
        for index in data.get("indexes", []):
            code = index.get("code")
            if code:
                indices[code] = index

        pollutants: dict[str, dict[str, Any]] = {}
        for pollutant in data.get("pollutants", []):
            code = pollutant.get("code")
            concentration = pollutant.get("concentration", {})
            if not code:
                continue
            pollutants[code] = {
                "value": concentration.get("value"),
                "units": concentration.get("units"),
                "sources": pollutant.get("additionalInfo", {}).get("sources"),
                "effects": pollutant.get("additionalInfo", {}).get("effects"),
            }

        self._data["indices"] = indices
        self._data["pollutants"] = pollutants
        self._data["region"] = data.get("regionCode")
        self._data["last_update"] = data.get("dateTime")

    def _process_forecast_data(self, data: dict[str, Any]) -> None:
        if not data or "hourlyForecasts" not in data:
            _LOGGER.warning("Received empty or invalid forecast data from API")
            self._data["forecast"] = []
            return

        forecasts = []
        for forecast in data["hourlyForecasts"]:
            index_uaqi = next(
                (item for item in forecast.get("indexes", []) if item.get("code") == "uaqi"),
                {},
            )
            forecasts.append(
                {
                    "datetime": forecast.get("dateTime"),
                    "aqi": index_uaqi.get("aqi"),
                    "dominant_pollutant": index_uaqi.get("dominantPollutant"),
                }
            )

        self._data["forecast"] = forecasts

    def _create_current_payload(self) -> dict[str, Any]:
        extra_computations = [
            "HEALTH_RECOMMENDATIONS",
            "LOCAL_AQI",
            "DOMINANT_POLLUTANT_CONCENTRATION",
            "POLLUTANT_CONCENTRATION",
        ]
        if self._get_additional_info:
            extra_computations.append("POLLUTANT_ADDITIONAL_INFO")

        return {
            "location": {
                "latitude": self._latitude,
                "longitude": self._longitude,
            },
            "extraComputations": extra_computations,
        }

    def _create_forecast_payload(self, start_time: datetime) -> dict[str, Any]:
        end_time = start_time + timedelta(hours=self._forecast_length)
        payload = {
            "universalAqi": "true",
            "location": {
                "latitude": self._latitude,
                "longitude": self._longitude,
            },
            "period": {
                "startTime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "languageCode": "en",
            "extraComputations": [
                "HEALTH_RECOMMENDATIONS",
                "DOMINANT_POLLUTANT_CONCENTRATION",
            ],
            "uaqiColorPalette": "RED_GREEN",
        }

        if self._get_additional_info:
            payload["extraComputations"].append("POLLUTANT_ADDITIONAL_INFO")

        return payload

    @property
    def get_additional_info(self) -> bool:
        return self._get_additional_info


SENSOR_DESCRIPTIONS: tuple[GoogleAQISensorDescription, ...] = (
    GoogleAQISensorDescription(
        key="aqi",
        name="AQI",
        value_fn=lambda data: data.get("indices", {}).get("uaqi", {}).get("aqi"),
        icon="mdi:air-filter",
    ),
    GoogleAQISensorDescription(
        key="dominant_pollutant",
        name="Dominant Pollutant",
        value_fn=lambda data: data.get("indices", {})
        .get("uaqi", {})
        .get("dominantPollutant"),
        icon="mdi:chemical-weapon",
    ),
    GoogleAQISensorDescription(
        key="pm25",
        name="PM2.5",
        value_fn=lambda data: data.get("pollutants", {}).get("pm25", {}).get("value"),
        unit_fn=lambda data: data.get("pollutants", {}).get("pm25", {}).get("units"),
        icon="mdi:blur",
    ),
    GoogleAQISensorDescription(
        key="pm10",
        name="PM10",
        value_fn=lambda data: data.get("pollutants", {}).get("pm10", {}).get("value"),
        unit_fn=lambda data: data.get("pollutants", {}).get("pm10", {}).get("units"),
        icon="mdi:blur",
    ),
    GoogleAQISensorDescription(
        key="co",
        name="CO",
        value_fn=lambda data: data.get("pollutants", {}).get("co", {}).get("value"),
        unit_fn=lambda data: data.get("pollutants", {}).get("co", {}).get("units"),
        icon="mdi:molecule",
    ),
    GoogleAQISensorDescription(
        key="no2",
        name="NO2",
        value_fn=lambda data: data.get("pollutants", {}).get("no2", {}).get("value"),
        unit_fn=lambda data: data.get("pollutants", {}).get("no2", {}).get("units"),
        icon="mdi:molecule",
    ),
    GoogleAQISensorDescription(
        key="o3",
        name="O3",
        value_fn=lambda data: data.get("pollutants", {}).get("o3", {}).get("value"),
        unit_fn=lambda data: data.get("pollutants", {}).get("o3", {}).get("units"),
        icon="mdi:molecule",
    ),
    GoogleAQISensorDescription(
        key="so2",
        name="SO2",
        value_fn=lambda data: data.get("pollutants", {}).get("so2", {}).get("value"),
        unit_fn=lambda data: data.get("pollutants", {}).get("so2", {}).get("units"),
        icon="mdi:molecule",
    ),
)


class GoogleAQISensor(CoordinatorEntity[GoogleAQICoordinator], SensorEntity):
    """Sensor for one Google AQI measurement."""

    entity_description: GoogleAQISensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GoogleAQICoordinator,
        entry_id: str,
        description: GoogleAQISensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name="Google AQI",
            manufacturer="Google",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.unit_fn is None:
            return None
        return self.entity_description.unit_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data

        if self.entity_description.key == "aqi":
            return {
                "category": data.get("indices", {}).get("uaqi", {}).get("category"),
                "dominant_pollutant": data.get("indices", {})
                .get("uaqi", {})
                .get("dominantPollutant"),
                "region": data.get("region"),
                "last_update": data.get("last_update"),
                "forecast": data.get("forecast"),
                "last_current_api_call": data.get("last_current_api_call"),
                "next_current_api_call": data.get("next_current_api_call"),
                "current_api_status": data.get("current_api_status"),
                "last_forecast_api_call": data.get("last_forecast_api_call"),
                "next_forecast_api_call": data.get("next_forecast_api_call"),
                "forecast_api_status": data.get("forecast_api_status"),
            }

        pollutant = data.get("pollutants", {}).get(self.entity_description.key)
        if not pollutant:
            return None

        attributes = {
            "unit": pollutant.get("units"),
        }

        if self.coordinator.get_additional_info:
            attributes["sources"] = pollutant.get("sources")
            attributes["effects"] = pollutant.get("effects")

        return attributes


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up Google AQI sensors from a config entry."""
    config = config_entry.data

    coordinator = GoogleAQICoordinator(
        hass,
        api_key=config["api_key"],
        latitude=config["latitude"],
        longitude=config["longitude"],
        get_additional_info=config.get("get_additional_info", False),
        update_interval=config.get("interval", 3),
        forecast_interval=config.get("forecast_interval", 6),
        forecast_length=config.get("forecast_length", 24),
    )
    await coordinator.async_config_entry_first_refresh()

    entities = [
        GoogleAQISensor(coordinator, config_entry.entry_id, description)
        for description in SENSOR_DESCRIPTIONS
    ]

    async_add_entities(entities)
