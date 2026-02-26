"""The FTMS integration."""

import logging
from pathlib import Path

import pyftms
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_SENSORS,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_STRAVA_CLIENT_ID,
    CONF_STRAVA_CLIENT_SECRET,
    CONF_STRAVA_REFRESH_TOKEN,
    DOMAIN,
)
from .coordinator import DataCoordinator
from .models import FtmsData
from .session import SessionTracker, strava_configured

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)

type FtmsConfigEntry = ConfigEntry[FtmsData]


async def async_unload_entry(hass: HomeAssistant, entry: FtmsConfigEntry) -> bool:
    """Unload a config entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        if entry.runtime_data.coordinator.session:
            await entry.runtime_data.coordinator.session.close()
        await entry.runtime_data.ftms.disconnect()
        bluetooth.async_rediscover_address(hass, entry.runtime_data.ftms.address)

    return unload_ok


async def async_setup_entry(hass: HomeAssistant, entry: FtmsConfigEntry) -> bool:
    """Set up device from a config entry."""

    address: str = entry.data[CONF_ADDRESS]

    if not (srv_info := bluetooth.async_last_service_info(hass, address)):
        raise ConfigEntryNotReady(translation_key="device_not_found")

    def _on_disconnect(ftms_: pyftms.FitnessMachine) -> None:
        """Disconnect handler. Save any active session, then reload."""

        if coordinator.session:
            coordinator.session.on_disconnect()

        if ftms_.need_connect:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    try:
        ftms = pyftms.get_client(
            srv_info.device,
            srv_info.advertisement,
            on_disconnect=_on_disconnect,
        )

    except pyftms.NotFitnessMachineError:
        raise ConfigEntryNotReady(translation_key="ftms_error")

    coordinator = DataCoordinator(hass, ftms)

    try:
        await ftms.connect()

    except BleakError as exc:
        raise ConfigEntryNotReady(translation_key="connection_failed") from exc

    assert ftms.machine_type.name

    _LOGGER.debug(f"Device Information: {ftms.device_info}")
    _LOGGER.debug(f"Machine type: {ftms.machine_type.name}")
    _LOGGER.debug(f"Available sensors: {ftms.available_properties}")
    _LOGGER.debug(f"Supported settings: {ftms.supported_settings}")
    _LOGGER.debug(f"Supported ranges: {ftms.supported_ranges}")

    unique_id = "".join(
        x for x in ftms.device_info.get("serial_number", address) if x.isalnum()
    ).lower()

    _LOGGER.debug(f"Registered new FTMS device. UniqueID is '{unique_id}'.")

    device_info = dr.DeviceInfo(
        connections={(dr.CONNECTION_BLUETOOTH, ftms.address)},
        identifiers={(DOMAIN, unique_id)},
        translation_key=ftms.machine_type.name.lower(),
        **ftms.device_info,
    )

    entry.runtime_data = FtmsData(
        entry_id=entry.entry_id,
        unique_id=unique_id,
        device_info=device_info,
        ftms=ftms,
        coordinator=coordinator,
        sensors=entry.options[CONF_SENSORS],
    )

    # Set up session tracker if Strava is configured
    if strava_configured(entry):
        workout_dir = Path(hass.config.path("ftms_workouts"))
        device_name = ftms.device_info.get("model", "Fitness Machine")
        machine_type_name = ftms.machine_type.name if ftms.machine_type else "treadmill"

        session = SessionTracker(
            hass=hass,
            entry=entry,
            machine_type_name=machine_type_name,
            device_name=device_name,
            workout_dir=workout_dir,
        )
        coordinator.session = session
        session._coordinator = coordinator
        _LOGGER.info("Session tracker active for %s (%s)", device_name, machine_type_name)

        # Upload any pending TCX files from previous sessions
        hass.async_create_task(session.upload_pending())

    @callback
    def _async_on_ble_event(
        srv_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Update from a ble callback."""

        ftms.set_ble_device_and_advertisement_data(
            srv_info.device, srv_info.advertisement
        )

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_on_ble_event,
            BluetoothCallbackMatcher(address=address),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    # Platforms initialization
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_entry_update_handler))

    async def _async_hass_stop_handler(event: Event) -> None:
        """Close the connection."""

        await ftms.disconnect()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_hass_stop_handler)
    )

    return True


async def _async_entry_update_handler(
    hass: HomeAssistant, entry: FtmsConfigEntry
) -> None:
    """Options update handler. Reload on user-facing config changes only.

    Token rotation (internal refresh_token updates) should NOT trigger a reload,
    as that would kill any in-progress BLE session and workout recording.
    """
    # Compare current runtime sensors with new options
    sensors_changed = entry.options.get(CONF_SENSORS) != entry.runtime_data.sensors

    # Compare Strava credentials (not the refresh token, which rotates automatically)
    old_strava = (
        entry.runtime_data.coordinator.session is not None
        if hasattr(entry.runtime_data, "coordinator")
        else False
    )
    new_strava = bool(
        entry.options.get(CONF_STRAVA_CLIENT_ID)
        and entry.options.get(CONF_STRAVA_CLIENT_SECRET)
        and entry.options.get(CONF_STRAVA_REFRESH_TOKEN)
    )
    strava_changed = old_strava != new_strava

    if sensors_changed or strava_changed:
        hass.config_entries.async_schedule_reload(entry.entry_id)
