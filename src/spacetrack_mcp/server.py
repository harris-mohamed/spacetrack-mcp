"""FastMCP server exposing Space-Track.org data as MCP tools."""

import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastmcp import FastMCP
from sgp4.api import Satrec, jday

from spacetrack_mcp.cache import (
    ANALYST_TTL,
    BOXSCORE_TTL,
    CONJUNCTION_TTL,
    DECAY_TTL,
    LAUNCH_SITE_TTL,
    MANEUVER_TTL,
    PROPAGATION_TTL,
    SATCAT_TTL,
    SENSOR_TTL,
    TIP_TTL,
    TLE_CURRENT_TTL,
    TLE_HISTORY_TTL,
    get_cache,
)
from spacetrack_mcp.client import get_client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="spacetrack-mcp",
    instructions=(
        "Tools for querying Space-Track.org satellite catalog and TLE data. "
        "Use search_satellites to find satellites by name or NORAD ID, then "
        "get_tle and propagate_orbit for orbital data."
    ),
)


# ---------------------------------------------------------------------------
# SATCAT tools
# ---------------------------------------------------------------------------


@mcp.tool
def search_satellites(
    query: str,
    object_type: Optional[str] = None,
    country: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Search the satellite catalog by name, NORAD ID, country or object type.

    Args:
        query: Satellite name fragment or NORAD ID number.
        object_type: Filter by type — PAYLOAD, ROCKET BODY, DEBRIS, or UNKNOWN.
        country: Two-letter country code (e.g. US, CN, RU).
        limit: Maximum results to return (default 20, max 100).

    Returns:
        List of SATCAT records with NORAD_CAT_ID, OBJECT_NAME, COUNTRY,
        LAUNCH_DATE, DECAY_DATE, OBJECT_TYPE, and RCS_SIZE.
    """
    cache_key = f"satcat:search:{query}:{object_type}:{country}:{limit}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    results = client.search_satcat(query, object_type=object_type, country=country, limit=limit)

    # Keep only the fields the spec requires
    trimmed = [
        {
            "NORAD_CAT_ID": r.get("NORAD_CAT_ID"),
            "OBJECT_NAME": r.get("OBJECT_NAME"),
            "COUNTRY": r.get("COUNTRY"),
            "LAUNCH_DATE": r.get("LAUNCH_DATE"),
            "DECAY_DATE": r.get("DECAY_DATE"),
            "OBJECT_TYPE": r.get("OBJECT_TYPE"),
            "RCS_SIZE": r.get("RCS_SIZE"),
        }
        for r in results
    ]

    cache.set(cache_key, trimmed, SATCAT_TTL)
    return trimmed


@mcp.tool
def get_satellite(norad_id: int) -> dict:
    """Return the full SATCAT record for a single satellite.

    Args:
        norad_id: NORAD catalog number (e.g. 25544 for ISS).

    Returns:
        Complete SATCAT record dict, or empty dict if not found.
    """
    cache_key = f"satcat:{norad_id}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    record = client.get_satcat(norad_id)
    result = record or {}

    cache.set(cache_key, result, SATCAT_TTL)
    return result


# ---------------------------------------------------------------------------
# TLE tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_tle(norad_id: int) -> dict:
    """Return the latest Two-Line Element set for a satellite.

    Queries the GP class (the current standard, replacing deprecated tle_latest).
    Applies a propagable filter to ensure the elset is suitable for SGP4 propagation
    (on-orbit only, epoch within the last 10 days).

    Args:
        norad_id: NORAD catalog number.

    Returns:
        Dict with name, line1, line2, epoch keys, or empty dict if not found.
    """
    cache_key = f"tle:latest:{norad_id}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    record = client.get_gp_latest(norad_id)

    if not record:
        return {}

    result = {
        "name": record.get("OBJECT_NAME", ""),
        "line1": record.get("TLE_LINE1", ""),
        "line2": record.get("TLE_LINE2", ""),
        "epoch": record.get("EPOCH", ""),
    }

    cache.set(cache_key, result, TLE_CURRENT_TTL)
    return result


@mcp.tool
def get_tle_history(norad_id: int, limit: int = 10) -> list[dict]:
    """Return recent historical TLEs for a satellite.

    Queries the GP_History class (the current standard, replacing deprecated tle).
    Per Space-Track guidelines, historical data should be downloaded infrequently
    and stored locally. Results are cached for 6 hours.

    Args:
        norad_id: NORAD catalog number.
        limit: Number of historical TLEs to return (default 10).

    Returns:
        List of dicts with name, line1, line2, epoch — newest first.
    """
    cache_key = f"tle:history:{norad_id}:{limit}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    records = client.get_gp_history(norad_id, limit=limit)

    result = [
        {
            "name": r.get("OBJECT_NAME", ""),
            "line1": r.get("TLE_LINE1", ""),
            "line2": r.get("TLE_LINE2", ""),
            "epoch": r.get("EPOCH", ""),
        }
        for r in records
    ]

    cache.set(cache_key, result, TLE_HISTORY_TTL)
    return result


# ---------------------------------------------------------------------------
# Propagation tool
# ---------------------------------------------------------------------------


@mcp.tool
def propagate_orbit(
    norad_id: int,
    steps: int = 90,
    step_minutes: float = 1.0,
) -> list[dict]:
    """Compute the ground track of a satellite using SGP4 propagation.

    Fetches the latest TLE and propagates the orbit forward from now.

    Args:
        norad_id: NORAD catalog number.
        steps: Number of propagation steps (default 90 = one full orbit).
        step_minutes: Time between steps in minutes (default 1).

    Returns:
        List of {time, lat, lon, alt_km, velocity_km_s} dicts.
    """
    cache_key = f"propagate:{norad_id}:{steps}:{step_minutes}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Fetch TLE (use cached if available)
    tle = get_tle(norad_id)
    if not tle or not tle.get("line1") or not tle.get("line2"):
        raise ValueError(f"No TLE available for NORAD ID {norad_id}")

    satellite = Satrec.twoline2rv(tle["line1"], tle["line2"])

    now = datetime.now(timezone.utc)
    track = []

    for i in range(steps):
        dt_offset = now.timestamp() + i * step_minutes * 60
        prop_dt = datetime.fromtimestamp(dt_offset, tz=timezone.utc)
        jd_val, fr = jday(
            prop_dt.year, prop_dt.month, prop_dt.day,
            prop_dt.hour, prop_dt.minute,
            prop_dt.second + prop_dt.microsecond / 1e6,
        )

        error, r, v = satellite.sgp4(jd_val, fr)
        if error != 0:
            continue  # skip steps with propagation errors

        # Convert ECI (km) to geodetic (lat/lon/alt)
        x, y, z = r
        vx, vy, vz = v

        RE = 6378.137  # Earth equatorial radius (km)
        flat = 1.0 / 298.257223563
        e2 = 2 * flat - flat**2

        lon = math.degrees(math.atan2(y, x))
        p = math.sqrt(x**2 + y**2)
        lat = math.degrees(math.atan2(z, p * (1 - e2)))

        # Bowring iterative method for geodetic latitude (5 iterations)
        for _ in range(5):
            sin_lat = math.sin(math.radians(lat))
            N = RE / math.sqrt(1 - e2 * sin_lat**2)
            lat = math.degrees(math.atan2(z + e2 * N * sin_lat, p))

        sin_lat = math.sin(math.radians(lat))
        cos_lat = math.cos(math.radians(lat))
        N = RE / math.sqrt(1 - e2 * sin_lat**2)

        if abs(cos_lat) > 1e-10:
            alt_km = p / cos_lat - N
        else:
            alt_km = abs(z) / abs(sin_lat) - N * (1 - e2)

        velocity_km_s = math.sqrt(vx**2 + vy**2 + vz**2)

        track.append(
            {
                "time": prop_dt.isoformat(),
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "alt_km": round(alt_km, 2),
                "velocity_km_s": round(velocity_km_s, 4),
            }
        )

    cache.set(cache_key, track, PROPAGATION_TTL)
    return track


# ---------------------------------------------------------------------------
# Conjunction tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_conjunctions(norad_id: int, limit: int = 20) -> list[dict]:
    """Return upcoming conjunction (close approach) warnings for a satellite.

    Queries the CDM_PUBLIC class for events where the satellite appears as
    either the primary or secondary object. Only future TCAs are included,
    ordered soonest first.

    Args:
        norad_id: NORAD catalog number of the satellite to check.
        limit: Maximum number of conjunction events to return (default 20).

    Returns:
        List of conjunction records with TCA, MISS_DISTANCE, COLLISION_PROBABILITY,
        SAT_1_NAME, SAT_2_NAME, RELATIVE_SPEED, and related fields.
    """
    cache_key = f"cdm:{norad_id}:{limit}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    results = client.get_conjunctions(norad_id, limit=limit)

    cache.set(cache_key, results, CONJUNCTION_TTL)
    return results


# ---------------------------------------------------------------------------
# Decay / reentry tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_decay_predictions(
    norad_id: Optional[int] = None,
    limit: int = 50,
) -> list[dict]:
    """Return reentry predictions from Space-Track's DECAY class.

    Without a NORAD ID, returns the next predicted reentries across all
    tracked objects (useful for "what's coming down soon?" queries).
    With a NORAD ID, returns all decay records for that specific object.

    Args:
        norad_id: Optional NORAD catalog number. Omit to query all upcoming reentries.
        limit: Maximum records to return (default 50).

    Returns:
        List of decay records with NORAD_CAT_ID, OBJECT_NAME, DECAY_EPOCH,
        COUNTRY, RCS, and MSG_EPOCH fields.
    """
    cache_key = f"decay:{norad_id}:{limit}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    results = client.get_decay_predictions(norad_id=norad_id, limit=limit)

    cache.set(cache_key, results, DECAY_TTL)
    return results


# ---------------------------------------------------------------------------
# Catalog overview tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_boxscore() -> list[dict]:
    """Return catalog aggregate statistics broken down by country.

    The BOXSCORE table shows the count of payloads, rocket bodies, and debris
    currently on-orbit and historically decayed, grouped by country/operator.
    Useful for answering questions like "how many objects does each country
    have in orbit?"

    Returns:
        List of records per country with ORBITAL_PAYLOAD_COUNT,
        ORBITAL_ROCKET_BODY_COUNT, ORBITAL_DEBRIS_COUNT, ORBITAL_TOTAL_COUNT,
        DECAYED_TOTAL_COUNT, and COUNTRY_TOTAL fields.
    """
    cache_key = "boxscore"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    results = client.get_boxscore()

    cache.set(cache_key, results, BOXSCORE_TTL)
    return results


@mcp.tool
def get_launch_sites() -> list[dict]:
    """Return the complete Space-Track launch site reference table.

    Provides the mapping between site codes (used in SATCAT SITE field)
    and human-readable launch site names and countries.

    Returns:
        List of records with SITE_CODE and LAUNCH_SITE fields.
    """
    cache_key = "launch_sites"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    results = client.get_launch_sites()

    cache.set(cache_key, results, LAUNCH_SITE_TTL)
    return results


# ---------------------------------------------------------------------------
# TIP — Tracking and Impact Prediction tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_tip(norad_id: Optional[int] = None, limit: int = 20) -> list[dict]:
    """Return TIP (Tracking and Impact Prediction) messages from Space-Track.

    TIP messages provide detailed reentry prediction windows with WINDOW_START,
    WINDOW_END, DECAY_EPOCH, MAXWINDOW (uncertainty in minutes), and INSERTION
    time. More frequently updated and precise than the DECAY class — especially
    useful in the hours/days before reentry.

    Args:
        norad_id: Optional NORAD catalog number. Omit to get the most recent TIPs
                  across all objects (useful for "what's reentering soon?" queries).
        limit: Maximum records to return (default 20).

    Returns:
        List of TIP records with NORAD_CAT_ID, OBJECT_NAME, WINDOW_START,
        WINDOW_END, DECAY_EPOCH, MAXWINDOW, COUNTRY, and INSERT_EPOCH fields.
    """
    cache_key = f"tip:{norad_id}:{limit}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    results = client.get_tip(norad_id=norad_id, limit=limit)

    cache.set(cache_key, results, TIP_TTL)
    return results


# ---------------------------------------------------------------------------
# Analyst satellite tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_analyst_satellites(limit: int = 50) -> list[dict]:
    """Return uncatalogued analyst satellite records from Space-Track.

    The analyst_satellite class contains recently detected objects that are being
    tracked by the Space Surveillance Network but have not yet received an official
    NORAD catalog number. Useful for monitoring newly launched payloads or debris
    events before they are formally catalogued.

    Args:
        limit: Maximum records to return, ordered by most recent epoch (default 50).

    Returns:
        List of analyst satellite records with OBJECT_NAME, OBJECT_ID, EPOCH,
        and TLE orbital elements fields.
    """
    cache_key = f"analyst_sat:{limit}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    results = client.get_analyst_satellites(limit=limit)

    cache.set(cache_key, results, ANALYST_TTL)
    return results


# ---------------------------------------------------------------------------
# Sensor catalog tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_sensors() -> list[dict]:
    """Return the Space Surveillance Network ground sensor catalog.

    Lists all radar and optical tracking stations used to maintain the space
    object catalog, including their geographic positions and sensor type. Useful
    for understanding coverage, answering questions about which countries operate
    tracking stations, and correlating observation gaps.

    Returns:
        List of sensor records with SENSOR_ID, SENSOR_NAME, SENSOR_TYPE,
        LATITUDE, LONGITUDE, ALTITUDE, and COUNTRY fields.
    """
    cache_key = "sensors"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    results = client.get_sensors()

    cache.set(cache_key, results, SENSOR_TTL)
    return results


# ---------------------------------------------------------------------------
# Maneuver tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_maneuvers(norad_id: Optional[int] = None, limit: int = 20) -> list[dict]:
    """Return reported satellite maneuver records from Space-Track.

    The maneuver class contains maneuver notifications for tracked objects.
    Records include the maneuver epoch, delta-V components, and the resulting
    orbital change. Useful for identifying active satellites (those that
    maneuver) and understanding orbit maintenance patterns.

    Args:
        norad_id: Optional NORAD catalog number. Omit to get the most recently
                  reported maneuvers across all tracked satellites.
        limit: Maximum records to return (default 20).

    Returns:
        List of maneuver records with NORAD_CAT_ID, OBJECT_NAME,
        MANEUVER_EPOCH, DELTA_V1, DELTA_V2, DELTA_V3, and related fields.
    """
    cache_key = f"maneuver:{norad_id}:{limit}"
    cache = get_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    results = client.get_maneuvers(norad_id=norad_id, limit=limit)

    cache.set(cache_key, results, MANEUVER_TTL)
    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio transport (for Claude Desktop)."""
    mcp.run()


if __name__ == "__main__":
    main()
