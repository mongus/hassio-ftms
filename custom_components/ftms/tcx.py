"""TCX file generation using stdlib XML.

Generates Garmin Training Center XML from recorded trackpoints, with the TPX
extension namespace for per-point speed data. No external dependencies.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from xml.etree.ElementTree import Element, ElementTree, SubElement, indent

TPX_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"

# Map Strava activity types to TCX Sport attribute values.
# TCX only supports Running, Biking, Other — everything else maps to Other.
SPORT_MAP: dict[str, str] = {
    "Run": "Running",
    "Ride": "Biking",
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _el(parent: Element, tag: str, text: str | None = None) -> Element:
    el = SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def generate_tcx(
    trackpoints: list[dict],
    start_time: datetime,
    total_seconds: float,
    total_distance_m: float,
    total_calories: int,
    activity_type: str = "Walk",
) -> str:
    """Build a TCX XML string from recorded trackpoints.

    Each trackpoint dict should have keys:
        time (datetime), distance_m (float), speed_kmh (float).
    """
    sport = SPORT_MAP.get(activity_type, "Other")

    root = Element("TrainingCenterDatabase")
    root.set("xmlns", "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("xmlns:tpx", TPX_NS)

    activities = _el(root, "Activities")
    activity = _el(activities, "Activity")
    activity.set("Sport", sport)

    _el(activity, "Id", _iso(start_time))

    lap = _el(activity, "Lap")
    lap.set("StartTime", _iso(start_time))
    _el(lap, "TotalTimeSeconds", f"{total_seconds:.1f}")
    _el(lap, "DistanceMeters", f"{total_distance_m:.1f}")
    _el(lap, "Calories", str(total_calories))
    _el(lap, "Intensity", "Active")
    _el(lap, "TriggerMethod", "Manual")

    track = _el(lap, "Track")

    for tp in trackpoints:
        tp_el = _el(track, "Trackpoint")
        _el(tp_el, "Time", _iso(tp["time"]))
        _el(tp_el, "DistanceMeters", f"{tp['distance_m']:.1f}")

        extensions = _el(tp_el, "Extensions")
        tpx = SubElement(extensions, f"{{{TPX_NS}}}TPX")
        SubElement(tpx, f"{{{TPX_NS}}}Speed").text = f"{tp['speed_kmh'] / 3.6:.4f}"

    indent(root, space="  ")
    tree = ElementTree(root)

    buf = io.BytesIO()
    tree.write(buf, xml_declaration=True, encoding="UTF-8")
    return buf.getvalue().decode("UTF-8")


def save_tcx(content: str, workout_dir: Path, start_time: datetime) -> Path:
    """Save TCX content to a timestamped file and return the path."""
    workout_dir.mkdir(parents=True, exist_ok=True)
    filename = start_time.strftime("%Y%m%d_%H%M%S") + ".tcx"
    path = workout_dir / filename
    path.write_text(content, encoding="utf-8")
    return path
