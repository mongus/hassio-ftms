"""Strava OAuth token management and activity upload.

Handles async token refresh with rotation persistence, TCX file upload with
polling for processing status, and activity metadata updates. Uses httpx
(lazy-imported to avoid loading when Strava is unconfigured).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .const import STRAVA_ACTIVITY_URL, STRAVA_TOKEN_URL, STRAVA_UPLOAD_URL

_LOGGER = logging.getLogger(__name__)


class StravaUploader:
    """Handles Strava token refresh and activity uploads."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        on_token_refresh: Callable[[str], Awaitable[None]],
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._on_token_refresh = on_token_refresh
        self._access_token: str = ""
        self._token_expires_at: float = 0
        self._http: object | None = None  # lazy httpx.AsyncClient

    def _get_client(self):
        """Get or create a persistent httpx client (avoids repeated SSL init)."""
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=60)
        return self._http

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _ensure_token(self) -> None:
        """Refresh the access token if expired or about to expire."""
        if time.time() < self._token_expires_at - 60:
            return

        client = self._get_client()
        resp = await client.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        tokens = resp.json()

        self._access_token = tokens["access_token"]
        self._token_expires_at = tokens["expires_at"]

        # Strava rotates refresh tokens — persist the new one
        new_refresh = tokens.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            await self._on_token_refresh(new_refresh)
            _LOGGER.info("Rotated Strava refresh token")

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def upload(
        self,
        tcx_path: Path,
        activity_type: str,
        name: str,
        hide_from_home: bool = False,
        private: bool = False,
        gear_id: str = "",
    ) -> str | None:
        """Upload a TCX file to Strava and return the activity URL, or None."""
        await self._ensure_token()

        try:
            activity_id = await self._upload_file(tcx_path, activity_type)
        except Exception as exc:
            import httpx

            if isinstance(exc, httpx.HTTPStatusError):
                if exc.response.status_code == 401:
                    _LOGGER.warning("401 during upload, forcing token refresh")
                    self._token_expires_at = 0
                    await self._ensure_token()
                    activity_id = await self._upload_file(tcx_path, activity_type)
                elif exc.response.status_code == 409:
                    _LOGGER.warning("Duplicate activity, skipping: %s", tcx_path.name)
                    return None
                elif exc.response.status_code == 429:
                    _LOGGER.warning("Rate limited, will retry later")
                    raise
                else:
                    raise
            else:
                raise

        if not activity_id:
            return None

        await self._update_activity(
            activity_id,
            name=name,
            sport_type=activity_type,
            hide_from_home=hide_from_home,
            private=private,
            gear_id=gear_id,
        )
        url = f"https://www.strava.com/activities/{activity_id}"
        _LOGGER.info("Uploaded: %s", url)
        return url

    async def _upload_file(self, tcx_path: Path, activity_type: str) -> int | None:
        content = await asyncio.get_running_loop().run_in_executor(
            None, tcx_path.read_bytes
        )
        client = self._get_client()
        resp = await client.post(
            STRAVA_UPLOAD_URL,
            headers=self._auth_headers(),
            files={"file": (tcx_path.name, content, "application/xml")},
            data={
                "data_type": "tcx",
                "activity_type": activity_type.lower(),
            },
        )
        resp.raise_for_status()
        upload_id = resp.json()["id"]

        # Poll for processing completion
        for _ in range(30):
            await asyncio.sleep(2)
            resp = await client.get(
                f"{STRAVA_UPLOAD_URL}/{upload_id}",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            status = resp.json()

            if status.get("activity_id"):
                return status["activity_id"]
            if status.get("error"):
                err = status["error"]
                if "duplicate" in err.lower():
                    _LOGGER.warning("Duplicate activity: %s", err)
                    return None
                _LOGGER.error("Upload error: %s", err)
                return None

        _LOGGER.error("Upload processing timed out")
        return None

    async def _update_activity(
        self,
        activity_id: int,
        *,
        name: str,
        sport_type: str,
        hide_from_home: bool,
        private: bool,
        gear_id: str,
    ) -> None:
        payload: dict[str, object] = {
            "name": name,
            "sport_type": sport_type,
        }
        if hide_from_home:
            payload["hide_from_home"] = True
        if private:
            payload["visibility"] = "only_me"
        if gear_id:
            payload["gear_id"] = gear_id

        client = self._get_client()
        resp = await client.put(
            f"{STRAVA_ACTIVITY_URL}/{activity_id}",
            headers=self._auth_headers(),
            json=payload,
        )
        if resp.status_code != 200:
            _LOGGER.warning(
                "Failed to update activity %s: %s %s",
                activity_id,
                resp.status_code,
                resp.text,
            )


async def exchange_code_for_tokens(
    client_id: str,
    client_secret: str,
    code: str,
) -> dict | None:
    """Exchange an OAuth authorization code for tokens.

    Returns the full response dict (includes 'access_token', 'refresh_token',
    'expires_at', and 'athlete' with gear lists) or None on failure.
    """
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                STRAVA_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        _LOGGER.exception("Failed to exchange Strava authorization code")
        return None


async def fetch_athlete_gear(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> list[dict[str, str]]:
    """Fetch athlete gear using a refresh token (for reconfiguration flows).

    Returns list of {'id': 'g12345', 'name': 'Nike Pegasus'} dicts.
    """
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            # Get a fresh access token
            resp = await client.post(
                STRAVA_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30,
            )
            resp.raise_for_status()
            access_token = resp.json()["access_token"]

            # Fetch athlete profile with gear
            resp = await client.get(
                "https://www.strava.com/api/v3/athlete",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
            resp.raise_for_status()
            athlete = resp.json()

        gear: list[dict[str, str]] = []
        for item in athlete.get("shoes") or []:
            gear.append({"id": item["id"], "name": item.get("name", item["id"])})
        for item in athlete.get("bikes") or []:
            gear.append({"id": item["id"], "name": item.get("name", item["id"])})
        return gear
    except Exception:
        _LOGGER.exception("Failed to fetch Strava athlete gear")
        return []
