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


def _smooth_trackpoints(trackpoints: list[dict]) -> list[dict]:
    """Smooth and downsample trackpoints for clean Strava pace display.

    Three-stage pipeline:
    1. Trim leading zero-distance points (belt ramp-up before motion starts).
    2. Interpolate distance linearly between discrete integer-meter jumps.
    3. Downsample to one point per ~5s so remaining quantization noise averages out.
    """
    if len(trackpoints) < 2:
        return trackpoints

    # Stage 1: trim leading zero-distance points
    first_nonzero = 0
    for i, tp in enumerate(trackpoints):
        if tp["distance_m"] > 0:
            first_nonzero = i
            break
    if first_nonzero > 0:
        trackpoints = trackpoints[first_nonzero:]
    if len(trackpoints) < 2:
        return trackpoints

    # Stage 2: interpolate between discrete distance jumps
    changes = [0]
    for i in range(1, len(trackpoints)):
        if trackpoints[i]["distance_m"] > trackpoints[changes[-1]]["distance_m"]:
            changes.append(i)

    if len(changes) < 2:
        return trackpoints

    result = [dict(tp) for tp in trackpoints]

    for seg in range(len(changes) - 1):
        i_start = changes[seg]
        i_end = changes[seg + 1]
        d_start = trackpoints[i_start]["distance_m"]
        d_end = trackpoints[i_end]["distance_m"]
        t_start = trackpoints[i_start]["time"].timestamp()
        t_end = trackpoints[i_end]["time"].timestamp()
        dt = t_end - t_start
        if dt <= 0:
            continue

        for j in range(i_start + 1, i_end):
            frac = (trackpoints[j]["time"].timestamp() - t_start) / dt
            result[j]["distance_m"] = d_start + frac * (d_end - d_start)

    # Stage 3: downsample to reduce quantization ripple
    interval = 5.0
    downsampled = [result[0]]
    last_ts = result[0]["time"].timestamp()
    for tp in result[1:]:
        if tp["time"].timestamp() - last_ts >= interval:
            downsampled.append(tp)
            last_ts = tp["time"].timestamp()
    if downsampled[-1] is not result[-1]:
        downsampled.append(result[-1])
    result = downsampled

    # Recalculate speed from smoothed, downsampled distance
    for i in range(1, len(result)):
        dt = (result[i]["time"] - result[i - 1]["time"]).total_seconds()
        if dt > 0:
            dd = result[i]["distance_m"] - result[i - 1]["distance_m"]
            result[i]["speed_kmh"] = (dd / dt) * 3.6
    if result:
        result[0]["speed_kmh"] = result[1]["speed_kmh"] if len(result) > 1 else 0.0

    return result


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
    trackpoints = _smooth_trackpoints(trackpoints)
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
