"""FTMS integration button platform."""

import logging
from typing import override

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pyftms.client import const as c

from . import FtmsConfigEntry
from .const import UPLOAD_WORKOUT
from .entity import FtmsEntity
from .session import strava_configured

_LOGGER = logging.getLogger(__name__)

_ENTITIES = (
    c.RESET,
    c.STOP,
    c.START,
    c.PAUSE,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FtmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a FTMS button entry."""

    entities: list[ButtonEntity] = [
        FtmsButtonEntity(
            entry=entry,
            description=ButtonEntityDescription(key=description),
        )
        for description in _ENTITIES
    ]

    # Add upload button when Strava is configured
    if strava_configured(entry):
        entities.append(
            UploadWorkoutButton(
                entry=entry,
                description=ButtonEntityDescription(key=UPLOAD_WORKOUT),
            )
        )

    async_add_entities(entities)


class FtmsButtonEntity(FtmsEntity, ButtonEntity):
    """Representation of FTMS control buttons."""

    @override
    async def async_press(self) -> None:
        """Handle the button press."""
        if self.key == c.RESET:
            await self.ftms.reset()

        elif self.key == c.START:
            await self.ftms.start_resume()

        elif self.key == c.STOP:
            await self.ftms.stop()

        elif self.key == c.PAUSE:
            await self.ftms.pause()


class UploadWorkoutButton(FtmsEntity, ButtonEntity):
    """Button to manually upload the last recorded workout to Strava."""

    @override
    async def async_press(self) -> None:
        """Handle the button press."""
        session = self._data.coordinator.session
        if session:
            await session.upload_last()
