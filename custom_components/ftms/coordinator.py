"""Data coordinator for receiving FTMS events."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pyftms import FitnessMachine, FtmsEvents

from .const import DOMAIN

if TYPE_CHECKING:
    from .session import SessionTracker

_LOGGER = logging.getLogger(__name__)


class DataCoordinator(DataUpdateCoordinator[FtmsEvents]):
    """FTMS events coordinator."""

    session: SessionTracker | None = None

    def __init__(self, hass: HomeAssistant, ftms: FitnessMachine) -> None:
        """Initialize the coordinator."""

        def _on_ftms_event(data: FtmsEvents):
            _LOGGER.debug(f"Event data: {data}")
            self.async_set_updated_data(data)
            if self.session:
                self.session.on_coordinator_update(data)

        super().__init__(hass, _LOGGER, name=DOMAIN)

        ftms.set_callback(_on_ftms_event)
