"""Workout session detection, recording, and Strava upload orchestration.

Passively observes coordinator data updates to detect workout start/end,
accumulates trackpoints during recording, generates TCX, and triggers
Strava upload. Only instantiated when Strava is configured.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from pyftms import FtmsEvents, TrainingStatusCode
from pyftms.client import const as c

if TYPE_CHECKING:
    from .coordinator import DataCoordinator

from .const import (
    ACTIVITY_TYPE_MAP,
    CONF_STRAVA_ACTIVITY_TYPE,
    CONF_STRAVA_CLIENT_ID,
    CONF_STRAVA_CLIENT_SECRET,
    CONF_STRAVA_GEAR_ID,
    CONF_STRAVA_HIDE_FROM_HOME,
    CONF_STRAVA_NAME_TEMPLATE,
    CONF_STRAVA_PRIVATE,
    CONF_STRAVA_REFRESH_TOKEN,
    DEFAULT_NAME_TEMPLATE,
    SESSION_IDLE_TIMEOUT,
    SESSION_MIN_POINTS,
    SESSION_START_COUNT,
)
from .strava import StravaUploader
from .tcx import generate_tcx

_LOGGER = logging.getLogger(__name__)


def _write_tcx(content: str, path: Path) -> None:
    """Write TCX content to disk. Called via executor from async context."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def strava_configured(entry: ConfigEntry) -> bool:
    """Return True if Strava credentials are present in the config entry."""
    opts = entry.options
    return bool(
        opts.get(CONF_STRAVA_CLIENT_ID)
        and opts.get(CONF_STRAVA_CLIENT_SECRET)
        and opts.get(CONF_STRAVA_REFRESH_TOKEN)
    )


def detect_activity_type(
    machine_type_name: str, avg_speed_kmh: float
) -> str:
    """Auto-detect Strava activity type from FTMS machine type and speed."""
    key = machine_type_name.lower()
    if key not in ACTIVITY_TYPE_MAP:
        return "Workout"

    slow_type, fast_type, threshold = ACTIVITY_TYPE_MAP[key]
    if threshold is not None and avg_speed_kmh >= threshold:
        return fast_type
    return slow_type


class SessionTracker:
    """Tracks workout sessions by observing coordinator data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        machine_type_name: str,
        device_name: str,
        workout_dir: Path,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._machine_type_name = machine_type_name
        self._device_name = device_name
        self._workout_dir = workout_dir

        # Will be set by __init__.py after coordinator.session = self
        self._coordinator: DataCoordinator | None = None

        # Session state: idle / recording / uploading
        self.state: str = "idle"
        self.last_workout_summary: str = ""

        # Recording state
        self._trackpoints: list[dict] = []
        self._start_time: datetime | None = None
        self._consecutive_active: int = 0  # consecutive time_elapsed > 0 readings
        self._last_active_time: float = 0  # monotonic time of last activity
        self._last_speed: float = 0.0
        self._last_distance: float = 0.0
        self._last_elapsed: int = -1  # last seen time_elapsed value
        self._session_end_time: float = 0  # cooldown after session end

        # Last saved TCX for manual upload
        self._last_tcx_path: Path | None = None

        # Cached summary from last finished recording (survives _reset)
        self._last_avg_speed: float = 0.0
        self._last_total_distance_m: float = 0.0
        self._last_total_seconds: float = 0.0

        self._uploader: StravaUploader | None = None
        self._init_uploader()

    def _init_uploader(self) -> None:
        """Create the Strava uploader from config entry options."""
        opts = self._entry.options
        client_id = opts.get(CONF_STRAVA_CLIENT_ID, "")
        client_secret = opts.get(CONF_STRAVA_CLIENT_SECRET, "")
        refresh_token = opts.get(CONF_STRAVA_REFRESH_TOKEN, "")

        if not (client_id and client_secret and refresh_token):
            return

        async def _persist_token(new_token: str) -> None:
            new_opts = dict(self._entry.options)
            new_opts[CONF_STRAVA_REFRESH_TOKEN] = new_token
            self._hass.config_entries.async_update_entry(
                self._entry, options=new_opts
            )

        self._uploader = StravaUploader(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            on_token_refresh=_persist_token,
        )

    def _set_state(self, new_state: str) -> None:
        old = self.state
        self.state = new_state
        if old != new_state:
            _LOGGER.warning("Session: %s -> %s", old, new_state)
            self._notify_entities()

    def _notify_entities(self) -> None:
        """Trigger coordinator listeners so session entities update in HA."""
        if self._coordinator and self._coordinator.data is not None:
            # Re-fire the last coordinator data to trigger _handle_coordinator_update
            # on all entities (including session sensors)
            self._coordinator.async_set_updated_data(self._coordinator.data)

    def on_coordinator_update(self, data: FtmsEvents) -> None:
        """Called by the coordinator when new FTMS data arrives."""
        if data.event_id != "update":
            return

        event_data = data.event_data

        # Extract available fields. Most notifications only have time_elapsed
        # and distance_total. speed_instant appears rarely (once at start).
        raw_speed = event_data.get(c.SPEED_INSTANT)
        if raw_speed is not None and isinstance(raw_speed, (int, float)):
            self._last_speed = float(raw_speed)

        if self.state == "idle":
            self._check_workout_start(event_data)
        elif self.state == "recording":
            self._record_and_check_end(event_data)

    def _check_workout_start(self, event_data: dict) -> None:
        # 15s cooldown after session end to avoid re-triggering
        if self._session_end_time:
            now = asyncio.get_running_loop().time()
            if now - self._session_end_time < 15:
                return
            self._session_end_time = 0

        # Detect workout start from time_elapsed (present in every notification,
        # unlike speed_instant which only appears once at start).
        elapsed = event_data.get(c.TIME_ELAPSED)
        if elapsed is not None and int(elapsed) > 0:
            self._consecutive_active += 1
            if self._consecutive_active >= SESSION_START_COUNT:
                self._start_recording()
        else:
            self._consecutive_active = 0

    def _start_recording(self) -> None:
        self._set_state("recording")
        self._trackpoints = []
        self._start_time = datetime.now(timezone.utc)
        self._last_active_time = asyncio.get_running_loop().time()
        self._idle_check_task = self._hass.async_create_task(self._idle_watchdog())
        _LOGGER.warning("Workout started at %s", self._start_time.isoformat())

    async def _idle_watchdog(self) -> None:
        """Periodic check for session timeout when notifications stop arriving."""
        while self.state == "recording":
            await asyncio.sleep(10)
            if self.state != "recording":
                break
            now = asyncio.get_running_loop().time()
            if now - self._last_active_time >= SESSION_IDLE_TIMEOUT:
                _LOGGER.warning("Session ended (watchdog): no activity for %ds", SESSION_IDLE_TIMEOUT)
                await self._finish_and_upload()
                break

    def _record_and_check_end(self, event_data: dict) -> None:
        now = asyncio.get_running_loop().time()

        # Track time_elapsed — increments every second during active workout.
        # This is the most reliable activity signal from FTMS.
        elapsed = event_data.get(c.TIME_ELAPSED)
        if elapsed is not None:
            new_elapsed = int(elapsed)
            if new_elapsed > self._last_elapsed:
                self._last_active_time = now
            self._last_elapsed = new_elapsed

        # Carry forward last known distance
        raw_distance = event_data.get(c.DISTANCE_TOTAL)
        if raw_distance is not None:
            new_dist = float(raw_distance)
            if new_dist > self._last_distance:
                self._last_active_time = now
            if new_dist > 0:
                self._last_distance = new_dist
        calories = int(event_data.get(c.ENERGY_TOTAL, 0) or 0)

        self._trackpoints.append({
            "time": datetime.now(timezone.utc),
            "distance_m": self._last_distance,
            "speed_kmh": self._last_speed,
            "calories": calories,
        })

        # End session when neither time_elapsed nor distance has increased
        if now - self._last_active_time >= SESSION_IDLE_TIMEOUT:
            _LOGGER.warning("Session ended: no activity for %ds", SESSION_IDLE_TIMEOUT)
            self._hass.async_create_task(self._finish_and_upload())

    async def _finish_and_upload(self) -> None:
        """Finish recording, save TCX (non-blocking), and upload to Strava."""
        self._session_end_time = asyncio.get_running_loop().time()
        result = self._finish_recording()
        if result:
            tcx_content, tcx_path = result
            await self._hass.async_add_executor_job(
                _write_tcx, tcx_content, tcx_path
            )
            _LOGGER.warning("Saved TCX: %s", tcx_path.name)
            if self._uploader:
                await self._do_upload(tcx_path)
        self._set_state("idle")

    def _finish_recording(self) -> tuple[str, Path] | None:
        """Generate TCX from trackpoints. Returns (content, path) or None.

        Does NOT write to disk — callers handle I/O (async or sync).
        """
        if len(self._trackpoints) < SESSION_MIN_POINTS:
            _LOGGER.info(
                "Only %d points (need %d), discarding",
                len(self._trackpoints),
                SESSION_MIN_POINTS,
            )
            self._reset()
            return None

        assert self._start_time is not None
        last = self._trackpoints[-1]
        total_seconds = (last["time"] - self._start_time).total_seconds()
        total_distance = last["distance_m"]
        total_calories = last.get("calories", 0)

        # Determine activity type
        avg_speed = self._avg_speed()
        configured_type = self._entry.options.get(CONF_STRAVA_ACTIVITY_TYPE, "auto")
        if configured_type == "auto":
            activity_type = detect_activity_type(self._machine_type_name, avg_speed)
        else:
            activity_type = configured_type

        tcx_content = generate_tcx(
            trackpoints=self._trackpoints,
            start_time=self._start_time,
            total_seconds=total_seconds,
            total_distance_m=total_distance,
            total_calories=total_calories,
            activity_type=activity_type,
        )

        tcx_path = self._workout_dir / (
            self._start_time.strftime("%Y%m%d_%H%M%S") + ".tcx"
        )
        num_points = len(self._trackpoints)

        # Cache summary data for _do_upload (which runs after _reset)
        self._last_avg_speed = avg_speed
        self._last_total_distance_m = total_distance
        self._last_total_seconds = total_seconds

        # Build human-readable summary
        dist_km = total_distance / 1000
        duration_min = total_seconds / 60
        self.last_workout_summary = f"{dist_km:.1f} km, {duration_min:.0f} min"
        self._last_tcx_path = tcx_path

        self._reset()
        _LOGGER.info(
            "Generated TCX: %d points, %.1f km, %.0f min",
            num_points, dist_km, duration_min,
        )
        return tcx_content, tcx_path

    def _avg_speed(self) -> float:
        speeds = [tp["speed_kmh"] for tp in self._trackpoints if tp["speed_kmh"] > 0]
        return sum(speeds) / len(speeds) if speeds else 0.0

    def _build_activity_name(self, activity_type: str, distance_m: float, duration_s: float) -> str:
        """Build the activity name from the template."""
        template = self._entry.options.get(
            CONF_STRAVA_NAME_TEMPLATE, DEFAULT_NAME_TEMPLATE
        )
        now = datetime.now()
        return template.format(
            activity=activity_type,
            device=self._device_name,
            date=now.strftime("%Y-%m-%d"),
            distance_km=f"{distance_m / 1000:.1f}",
            duration_min=f"{duration_s / 60:.0f}",
        )

    async def _do_upload(self, tcx_path: Path) -> None:
        """Upload a TCX file to Strava using cached summary data."""
        if not self._uploader:
            return

        self._set_state("uploading")
        opts = self._entry.options

        # Use cached summary from _finish_recording (trackpoints already cleared)
        configured_type = opts.get(CONF_STRAVA_ACTIVITY_TYPE, "auto")
        if configured_type == "auto":
            activity_type = detect_activity_type(
                self._machine_type_name, self._last_avg_speed
            )
        else:
            activity_type = configured_type

        name = self._build_activity_name(
            activity_type, self._last_total_distance_m, self._last_total_seconds
        )

        try:
            url = await self._uploader.upload(
                tcx_path=tcx_path,
                activity_type=activity_type,
                name=name,
                hide_from_home=opts.get(CONF_STRAVA_HIDE_FROM_HOME, False),
                private=opts.get(CONF_STRAVA_PRIVATE, False),
                gear_id=opts.get(CONF_STRAVA_GEAR_ID, ""),
            )
            if url:
                _LOGGER.info("Strava activity: %s", url)
                # Remove TCX after successful upload
                tcx_path.unlink(missing_ok=True)
                self._last_tcx_path = None
        except Exception:
            _LOGGER.exception("Upload failed, TCX kept for retry: %s", tcx_path.name)

    def _reset(self) -> None:
        self._trackpoints = []
        self._start_time = None
        self._consecutive_active = 0
        self._last_active_time = 0
        self._last_speed = 0.0
        self._last_distance = 0.0
        self._last_elapsed = -1

    async def close(self) -> None:
        """Release resources (called on entry unload)."""
        if self._uploader:
            await self._uploader.close()

    def on_disconnect(self) -> None:
        """Handle BLE disconnect — force-finish any active recording.

        Saves TCX to disk immediately (blocking OK — HA reloads after this).
        Upload happens on next boot via upload_pending().
        """
        if self.state != "recording":
            return

        _LOGGER.warning("BLE disconnected during recording, saving session")
        result = self._finish_recording()
        if result:
            tcx_content, tcx_path = result
            _write_tcx(tcx_content, tcx_path)
            _LOGGER.warning("Saved workout to %s for upload on next boot", tcx_path.name)
        self._set_state("idle")

    async def upload_last(self) -> None:
        """Manual upload of the last saved workout (button entity action)."""
        if not self._last_tcx_path or not self._last_tcx_path.exists():
            _LOGGER.warning("No workout available to upload")
            return
        await self._do_upload(self._last_tcx_path)
        self._set_state("idle")

    async def upload_pending(self) -> None:
        """Upload any pending TCX files from previous sessions."""
        if not self._uploader:
            return

        pending = sorted(self._workout_dir.glob("*.tcx"))
        if not pending:
            return

        _LOGGER.info("Found %d pending TCX file(s) to upload", len(pending))
        for path in pending:
            try:
                # Use defaults for pending files (no trackpoint data available)
                activity_type = detect_activity_type(self._machine_type_name, 0)
                name = self._build_activity_name(activity_type, 0, 0)
                opts = self._entry.options

                url = await self._uploader.upload(
                    tcx_path=path,
                    activity_type=activity_type,
                    name=name,
                    hide_from_home=opts.get(CONF_STRAVA_HIDE_FROM_HOME, False),
                    private=opts.get(CONF_STRAVA_PRIVATE, False),
                    gear_id=opts.get(CONF_STRAVA_GEAR_ID, ""),
                )
                if url:
                    _LOGGER.info("Uploaded pending: %s -> %s", path.name, url)
                # None = duplicate or processing error — remove to avoid infinite retries
                path.unlink(missing_ok=True)
            except Exception:
                _LOGGER.exception("Failed to upload pending: %s", path.name)
                break
