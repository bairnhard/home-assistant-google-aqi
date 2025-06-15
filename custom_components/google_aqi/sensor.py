from homeassistant.components.air_quality import AirQualityEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from datetime import datetime, timedelta, timezone
import logging
import asyncio

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

API_URL = "https://airquality.googleapis.com/v1/currentConditions:lookup"
FORECAST_URL = "https://airquality.googleapis.com/v1/forecast:lookup"
SCAN_INTERVAL = timedelta(hours=3)


class GoogleAQIAirQualityEntity(AirQualityEntity):
    """Representation of a Google AQI air quality entity."""

    def __init__(
        self,
        hass,
        entry_id,
        api_key,
        latitude,
        longitude,
        get_additional_info,
        update_interval,
        forecast_interval,
        forecast_length,
    ):
        self.hass = hass
        self._entry_id = entry_id
        self._api_key = api_key
        self._latitude = latitude
        self._longitude = longitude
        self._get_additional_info = get_additional_info
        self._update_interval = update_interval
        self._forecast_interval = forecast_interval
        self._forecast_length = forecast_length

        self._state = None
        self._attributes = {}
        self._pollutants = {}
        self._indices = []
        self._forecast = []
        # self._pm25 = None  # Tracks PM2.5 state

        # Timestamps and statuses
        self._last_current_api_call = None
        self._next_current_api_call = None
        self._current_api_status = None
        self._last_forecast_api_call = None
        self._next_forecast_api_call = None
        self._forecast_api_status = None

    @property
    def name(self):
        """Return the name of the sensor."""
        return "Google AQI Sensor"

    @property
    def device_info(self):
        """Return information about the device this entity belongs to."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name="Google AQI",
            manufacturer="Google",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def state(self):
        """Return the current PM2.5 value as the state."""
        return self._pm25

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        attributes = {
            "region": self._attributes.get("region"),
            "last_update": self._attributes.get("last_update"),
            "forecast": self._forecast,
            "last_current_api_call": self._last_current_api_call,
            "next_current_api_call": self._next_current_api_call,
            "current_api_status": self._current_api_status,
            "last_forecast_api_call": self._last_forecast_api_call,
            "next_forecast_api_call": self._next_forecast_api_call,
            "forecast_api_status": self._forecast_api_status,
        }

        # Include air quality indices
        for index in self._indices:
            attributes[f"index_{index['code']}"] = {
                "aqi": index.get("aqi"),
                "category": index.get("category"),
                "dominant_pollutant": index.get("dominantPollutant"),
            }

        # Include pollutants
        for pollutant, details in self._pollutants.items():
            pollutant_data = {
                "value": details.get("value"),
                "unit": details.get("units"),
            }
            if self._get_additional_info:
                pollutant_data.update(
                    {
                        "sources": details.get("sources"),
                        "effects": details.get("effects"),
                    }
                )
            attributes[pollutant] = pollutant_data

        return attributes

    @property
    def particulate_matter_2_5(self):
        """Return the PM2.5 value."""
        return self._pollutants.get("pm25", {}).get("value")

    @property
    def particulate_matter_10(self):
        """Return the PM10 value."""
        return self._pollutants.get("pm10", {}).get("value")

    @property
    def carbon_monoxide(self):
        """Return the CO value."""
        return self._pollutants.get("co", {}).get("value")

    @property
    def nitrogen_dioxide(self):
        """Return the NO2 value."""
        return self._pollutants.get("no2", {}).get("value")

    @property
    def ozone(self):
        """Return the O3 value."""
        return self._pollutants.get("o3", {}).get("value")

    @property
    def sulfur_dioxide(self):
        """Return the SO2 value."""
        return self._pollutants.get("so2", {}).get("value")

    async def async_update(self):
        """Fetch both current and forecast data with optimized API calls."""
        now = datetime.now()
        self._next_current_api_call = now + timedelta(hours=self._update_interval)
        self._next_forecast_api_call = now + timedelta(hours=self._forecast_interval)

        tasks = [self._fetch_current_data()]

        if (
            not self._last_forecast_api_call
            or now - self._last_forecast_api_call
            > timedelta(hours=self._forecast_interval)
        ):
            tasks.append(self._fetch_forecast_data())

        await asyncio.gather(*tasks)

    def _should_update_forecast(self):
        """Determine whether a forecast update is needed."""
        if not self._last_forecast_update:
            return True
        elapsed_time = datetime.now(timezone.utc) - self._last_forecast_update
        return elapsed_time > timedelta(hours=self._forecast_interval)

    async def _fetch_current_data(self):
        """Fetch current conditions data."""
        self._last_current_api_call = datetime.now()
        payload = self._create_current_payload()
        params = {"key": self._api_key}

        try:
            session = async_get_clientsession(self.hass)
            async with session.post(API_URL, json=payload, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    self._process_current_data(data)
                    self._forecast_api_status = "successful"
                else:
                    _LOGGER.error(
                        f"Failed to fetch current AQI data: {response.status} - {await response.text()}"
                    )
        except Exception as e:
            _LOGGER.error(f"Error fetching current AQI data: {e}")
            self._current_api_status = "error"

    async def _fetch_forecast_data(self):
        """Fetch forecast data with correct start and end time calculation."""
        self._last_forecast_api_call = datetime.now()
        now = datetime.now(timezone.utc)
        # Align `startTime` with the next full hour
        next_full_hour = (now + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        end_time = next_full_hour + timedelta(hours=48)  # Max allowed range is 96 hours

        payload = {
            "universalAqi": "true",
            "location": {
                "latitude": str(self._latitude),
                "longitude": str(self._longitude),
            },
            "period": {
                "startTime": next_full_hour.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "languageCode": "en",
            "extraComputations": [
                "HEALTH_RECOMMENDATIONS",
                "DOMINANT_POLLUTANT_CONCENTRATION",
                "POLLUTANT_ADDITIONAL_INFO",
            ],
            "uaqiColorPalette": "RED_GREEN",
        }
        params = {"key": self._api_key}

        try:
            session = async_get_clientsession(self.hass)
            async with session.post(
                FORECAST_URL, json=payload, params=params
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    self._process_forecast_data(data)
                    self._last_forecast_update = datetime.now(timezone.utc)
                    self._forecast_api_status = "successful"
                else:
                    error_text = await response.text()
                    _LOGGER.error(
                        f"Failed to fetch forecast data: {response.status} - {error_text}"
                    )
                    self._forecast_api_status = "error"
        except Exception as e:
            _LOGGER.error(f"Error fetching forecast data: {e}")
            self._forecast_api_status = "error"

    def _process_forecast_data(self, data):
        """Process forecast data for AQI and dominant pollutant."""
        if not data or "hourlyForecasts" not in data:
            _LOGGER.warning("Received empty or invalid forecast data from API")
            self._forecast = []
            return

        self._forecast = [
            {
                "datetime": forecast.get("dateTime"),
                "aqi": next(
                    (
                        index["aqi"]
                        for index in forecast.get("indexes", [])
                        if index["code"] == "uaqi"
                    ),
                    None,
                ),
                "dominant_pollutant": next(
                    (
                        index["dominantPollutant"]
                        for index in forecast.get("indexes", [])
                        if index["code"] == "uaqi"
                    ),
                    None,
                ),
            }
            for forecast in data["hourlyForecasts"]
        ]

    def _process_current_data(self, data):
        """Process and store current conditions data."""
        if not data:
            _LOGGER.warning("Received empty current data from API")
            return

        self._indices = data.get("indexes", [])
        self._attributes["region"] = data.get("regionCode")
        self._attributes["last_update"] = data.get("dateTime")

        # Store all pollutants
        self._pollutants = {
            pollutant["code"]: {
                "value": pollutant["concentration"]["value"],
                "units": pollutant["concentration"]["units"],
                "sources": pollutant.get("additionalInfo", {}).get("sources"),
                "effects": pollutant.get("additionalInfo", {}).get("effects"),
            }
            for pollutant in data.get("pollutants", [])
        }

        # Update PM2.5 state for the main entity
        if "pm25" in self._pollutants:
            self._pm25 = self._pollutants["pm25"]["value"]

    def _process_forecast_data(self, data):
        """Process forecast data for AQI and dominant pollutant."""
        if not data or "hourlyForecasts" not in data:
            _LOGGER.warning("Received empty or invalid forecast data from API")
            self._forecast = []
            return

        # Extract relevant forecast data
        self._forecast = [
            {
                "datetime": forecast.get("dateTime"),
                "aqi": next(
                    (
                        index["aqi"]
                        for index in forecast.get("indexes", [])
                        if index["code"] == "uaqi"
                    ),
                    None,
                ),
                "dominant_pollutant": next(
                    (
                        index["dominantPollutant"]
                        for index in forecast.get("indexes", [])
                        if index["code"] == "uaqi"
                    ),
                    None,
                ),
            }
            for forecast in data["hourlyForecasts"]
        ]

    def _create_current_payload(self):
        """Create the payload for the current conditions API call."""
        return {
            "location": {"latitude": self._latitude, "longitude": self._longitude},
            "extraComputations": [
                "HEALTH_RECOMMENDATIONS",
                "LOCAL_AQI",
                "POLLUTANT_ADDITIONAL_INFO",
                "DOMINANT_POLLUTANT_CONCENTRATION",
                "POLLUTANT_CONCENTRATION",
            ],
        }

    def _create_forecast_payload(self):
        """Create the payload for the forecast API call."""
        now = datetime.now(timezone.utc)
        end_time = now + timedelta(hours=self._forecast_length)
        return {
            "location": {"latitude": self._latitude, "longitude": self._longitude},
            "period": {
                "startTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        }


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities
):
    """Set up Google AQI air quality sensor from a config entry."""
    config = config_entry.data
    async_add_entities(
        [
            GoogleAQIAirQualityEntity(
                hass,
                config_entry.entry_id,
                api_key=config["api_key"],
                latitude=config["latitude"],
                longitude=config["longitude"],
                get_additional_info=config.get("get_additional_info", False),
                update_interval=config.get("interval", 3),
                forecast_interval=config.get("forecast_interval", 6),
                forecast_length=config.get("forecast_length", 24),
            )
        ],
        update_before_add=True,
    )
