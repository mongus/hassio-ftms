"""Constants for the FTMS integration."""

DOMAIN = "ftms"

# Strava configuration keys (stored in config entry options)
CONF_STRAVA_CLIENT_ID = "strava_client_id"
CONF_STRAVA_CLIENT_SECRET = "strava_client_secret"
CONF_STRAVA_REFRESH_TOKEN = "strava_refresh_token"
CONF_STRAVA_ACTIVITY_TYPE = "strava_activity_type"
CONF_STRAVA_NAME_TEMPLATE = "strava_name_template"
CONF_STRAVA_HIDE_FROM_HOME = "strava_hide_from_home"
CONF_STRAVA_PRIVATE = "strava_private"
CONF_STRAVA_GEAR_ID = "strava_gear_id"

# Strava API endpoints
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_UPLOAD_URL = "https://www.strava.com/api/v3/uploads"
STRAVA_ACTIVITY_URL = "https://www.strava.com/api/v3/activities"
STRAVA_CALLBACK_PORT = 8765

# Default name template with supported placeholders:
# {activity} = Walk/Run/Ride/etc, {device} = BLE device name,
# {date} = YYYY-MM-DD, {distance_km} = total km, {duration_min} = total min
DEFAULT_NAME_TEMPLATE = "{activity} on {device}"

# Activity type auto-detection from machine type.
# Maps machine_type.name.lower() → (slow_type, fast_type, speed_threshold_kmh)
# For treadmills: below threshold → Walk, above → Run
# For other machines: no speed distinction (threshold is None)
ACTIVITY_TYPE_MAP: dict[str, tuple[str, str, float | None]] = {
    "treadmill": ("Walk", "Run", 6.0),
    "cross_trainer": ("Elliptical", "Elliptical", None),
    "indoor_bike": ("Ride", "Ride", None),
    "rower": ("Rowing", "Rowing", None),
}

# Session tracking entity keys
SESSION_STATE = "session_state"
LAST_WORKOUT = "last_workout"
UPLOAD_WORKOUT = "upload_workout"

# Session detection thresholds
SESSION_START_COUNT = 3  # consecutive speed>0 readings to start recording
SESSION_IDLE_TIMEOUT = 30  # seconds of speed==0 before ending session
SESSION_MIN_POINTS = 10  # minimum trackpoints for a valid workout
