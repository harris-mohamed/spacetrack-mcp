"""Space-Track.org API client with session management and retry logic."""

import logging
import os
import time
from collections import deque
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.space-track.org"
LOGIN_URL = f"{BASE_URL}/ajaxauth/login"

MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds

# Space-Track rate limits per usage guidelines.
# Hard limits: 30 req/min, 300 req/hour. We use conservative thresholds.
_MAX_PER_MINUTE = 25
_MAX_PER_HOUR = 280


class _RateLimiter:
    """Sliding-window rate limiter enforcing Space-Track usage guidelines.

    Tracks request timestamps in two windows (60s and 3600s) and sleeps
    proactively before a request would exceed either limit. This avoids
    receiving 429s in the first place, which is the recommended approach.
    """

    def __init__(self) -> None:
        self._minute: deque[float] = deque()
        self._hour: deque[float] = deque()

    def wait_if_needed(self) -> None:
        now = time.time()

        # Evict expired timestamps
        while self._minute and now - self._minute[0] > 60:
            self._minute.popleft()
        while self._hour and now - self._hour[0] > 3600:
            self._hour.popleft()

        # Sleep until minute window has room
        if len(self._minute) >= _MAX_PER_MINUTE:
            wait = 60.0 - (now - self._minute[0]) + 0.1
            logger.info("Rate limiter: sleeping %.1fs (minute window full)", wait)
            time.sleep(wait)
            now = time.time()
            while self._minute and now - self._minute[0] > 60:
                self._minute.popleft()

        # Sleep until hour window has room
        if len(self._hour) >= _MAX_PER_HOUR:
            wait = 3600.0 - (now - self._hour[0]) + 0.1
            logger.info("Rate limiter: sleeping %.1fs (hour window full)", wait)
            time.sleep(wait)
            now = time.time()
            while self._hour and now - self._hour[0] > 3600:
                self._hour.popleft()

        self._minute.append(now)
        self._hour.append(now)


class SpaceTrackClient:
    """Authenticated Space-Track.org HTTP client.

    Uses the current GP and GP_History classes. The older tle_latest and tle
    classes are deprecated and must not be used per Space-Track documentation.

    Maintains a single cookie-based session and re-authenticates automatically
    on HTTP 401. Applies client-side rate limiting before each request and
    retries on 429 with exponential backoff as a secondary safeguard.
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._session = requests.Session()
        self._authenticated = False
        self._rate_limiter = _RateLimiter()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _login(self) -> None:
        """Authenticate and store session cookies."""
        resp = self._session.post(
            LOGIN_URL,
            data={"identity": self._username, "password": self._password},
            timeout=30,
        )
        resp.raise_for_status()
        self._authenticated = True
        logger.info("Authenticated with Space-Track.org")

    def _get(self, path: str) -> Any:
        """GET *path* with proactive rate limiting and 429 backoff.

        Returns parsed JSON. Raises RuntimeError after MAX_RETRIES failures.
        """
        if not self._authenticated:
            self._login()

        self._rate_limiter.wait_if_needed()
        url = f"{BASE_URL}{path}"

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._session.get(url, timeout=30)

                if resp.status_code == 401:
                    logger.warning("Session expired — re-authenticating")
                    self._authenticated = False
                    self._login()
                    continue

                if resp.status_code == 429:
                    # Start at 4s, 8s, 16s — more aggressive than default because
                    # we already apply proactive limiting above.
                    wait = BACKOFF_BASE ** (attempt + 2)
                    logger.warning("Rate limited (429) — waiting %ds", wait)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise
                wait = BACKOFF_BASE ** attempt
                logger.warning("Request error (%s) — retrying in %ds", exc, wait)
                time.sleep(wait)

        raise RuntimeError(f"Failed to GET {path} after {MAX_RETRIES} retries")

    # ------------------------------------------------------------------
    # SATCAT — updates daily; cache for 24 hours
    # ------------------------------------------------------------------

    def search_satcat(
        self,
        query: str,
        object_type: Optional[str] = None,
        country: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Search SATCAT by name fragment or NORAD ID with optional filters."""
        if query.strip().isdigit():
            path = (
                f"/basicspacedata/query/class/satcat"
                f"/NORAD_CAT_ID/{query.strip()}"
                f"/orderby/NORAD_CAT_ID"
                f"/limit/{limit}/format/json"
            )
        else:
            encoded = query.upper().replace(" ", "%20")
            path = (
                f"/basicspacedata/query/class/satcat"
                f"/OBJECT_NAME/~~{encoded}"
                f"/orderby/NORAD_CAT_ID"
                f"/limit/{limit}/format/json"
            )

        results: list[dict] = self._get(path)

        # Apply optional client-side filters (avoids extra round trips)
        if object_type:
            results = [r for r in results if r.get("OBJECT_TYPE") == object_type.upper()]
        if country:
            results = [r for r in results if r.get("COUNTRY") == country.upper()]

        return results

    def get_satcat(self, norad_id: int) -> Optional[dict]:
        """Return full SATCAT record for a single object."""
        path = (
            f"/basicspacedata/query/class/satcat"
            f"/NORAD_CAT_ID/{norad_id}"
            f"/orderby/NORAD_CAT_ID/limit/1/format/json"
        )
        results = self._get(path)
        return results[0] if results else None

    # ------------------------------------------------------------------
    # GP (General Perturbations) — replaces deprecated tle_latest class
    # Updates approximately hourly; cache for 1 hour.
    # ------------------------------------------------------------------

    def get_gp_latest(self, norad_id: int) -> Optional[dict]:
        """Return the most recent GP elset for *norad_id*.

        Queries the GP class (replaces deprecated tle_latest). Applies the
        recommended propagable filter (DECAY_DATE/null-val and EPOCH/>now-10)
        to ensure the returned elset is suitable for SGP4 propagation. Falls
        back to unfiltered query if the object has recently decayed.
        """
        # Propagable filter: on-orbit only, epoch within last 10 days
        path = (
            f"/basicspacedata/query/class/gp"
            f"/NORAD_CAT_ID/{norad_id}"
            f"/DECAY_DATE/null-val"
            f"/EPOCH/%3Enow-10"
            f"/orderby/EPOCH%20desc"
            f"/limit/1/format/json"
        )
        results = self._get(path)

        if not results:
            # Object may have decayed recently; retry without the propagable filter
            logger.info("No propagable GP for %d — retrying without filter", norad_id)
            path = (
                f"/basicspacedata/query/class/gp"
                f"/NORAD_CAT_ID/{norad_id}"
                f"/orderby/EPOCH%20desc"
                f"/limit/1/format/json"
            )
            results = self._get(path)

        return results[0] if results else None

    # ------------------------------------------------------------------
    # GP_History — replaces deprecated tle class
    # Per guidelines: treat each bulk download as a one-time pull and cache
    # results locally. We use a 6-hour TTL to avoid repeated queries.
    # ------------------------------------------------------------------

    def get_gp_history(self, norad_id: int, limit: int = 10) -> list[dict]:
        """Return historical GP elsets for *norad_id*.

        Queries the GP_History class (replaces deprecated tle class).
        Space-Track guidelines treat bulk GP_History downloads as one-time
        pulls. Results are cached for 6 hours — do not call this method in
        a tight loop.
        """
        path = (
            f"/basicspacedata/query/class/gp_history"
            f"/NORAD_CAT_ID/{norad_id}"
            f"/orderby/EPOCH%20desc"
            f"/limit/{limit}/format/json"
        )
        return self._get(path)

    # ------------------------------------------------------------------
    # CDM_PUBLIC — Conjunction Data Messages (public subset)
    # Updates ~3× per day; cache for 1 hour.
    # A satellite can appear as either SAT_1 (primary) or SAT_2
    # (secondary), so we issue two queries and merge.
    # ------------------------------------------------------------------

    def get_conjunctions(self, norad_id: int, limit: int = 20) -> list[dict]:
        """Return upcoming conjunction warnings involving *norad_id*.

        Queries CDM_PUBLIC for close-approach events where the satellite
        is either the primary (SAT_1_ID) or secondary (SAT_2_ID) object.
        Only future TCAs are returned, ordered soonest first.
        """
        results: list[dict] = []
        for field in ("SAT_1_ID", "SAT_2_ID"):
            path = (
                f"/basicspacedata/query/class/cdm_public"
                f"/{field}/{norad_id}"
                f"/TCA/%3Enow"
                f"/orderby/TCA%20asc"
                f"/limit/{limit}/format/json"
            )
            try:
                batch = self._get(path)
                results.extend(batch)
            except Exception as exc:
                logger.warning("CDM_PUBLIC query for %s=%d failed: %s", field, norad_id, exc)

        # Deduplicate by CDM_ID and sort by TCA
        seen: set[str] = set()
        unique: list[dict] = []
        for r in sorted(results, key=lambda x: x.get("TCA", "")):
            cdm_id = str(r.get("CDM_ID", id(r)))
            if cdm_id not in seen:
                seen.add(cdm_id)
                unique.append(r)

        return unique[:limit]

    # ------------------------------------------------------------------
    # DECAY — reentry predictions and confirmed decays
    # Updates daily; cache for 6 hours.
    # ------------------------------------------------------------------

    def get_decay_predictions(
        self,
        norad_id: Optional[int] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return reentry predictions from the DECAY class.

        If *norad_id* is given, return all decay records for that object.
        Otherwise return upcoming predicted reentries ordered by decay epoch.
        """
        if norad_id is not None:
            path = (
                f"/basicspacedata/query/class/decay"
                f"/NORAD_CAT_ID/{norad_id}"
                f"/orderby/DECAY_EPOCH%20desc"
                f"/limit/{limit}/format/json"
            )
        else:
            path = (
                f"/basicspacedata/query/class/decay"
                f"/DECAY_EPOCH/%3Enow"
                f"/orderby/DECAY_EPOCH%20asc"
                f"/limit/{limit}/format/json"
            )
        return self._get(path)

    # ------------------------------------------------------------------
    # BOXSCORE — catalog aggregate statistics
    # Updates daily; cache for 24 hours.
    # ------------------------------------------------------------------

    def get_boxscore(self) -> list[dict]:
        """Return the full BOXSCORE table (per-country orbital object counts)."""
        path = "/basicspacedata/query/class/boxscore/format/json"
        return self._get(path)

    # ------------------------------------------------------------------
    # LAUNCH_SITE — launch facility reference table
    # Rarely changes; cache for 7 days.
    # ------------------------------------------------------------------

    def get_launch_sites(self) -> list[dict]:
        """Return the complete launch site reference table."""
        path = "/basicspacedata/query/class/launch_site/format/json"
        return self._get(path)

    # ------------------------------------------------------------------
    # TIP — Tracking and Impact Prediction messages
    # More detailed than DECAY: includes time windows and probability.
    # Updates frequently near reentry; cache for 1 hour.
    # ------------------------------------------------------------------

    def get_tip(self, norad_id: Optional[int] = None, limit: int = 20) -> list[dict]:
        """Return TIP (Tracking and Impact Prediction) messages.

        TIP messages provide detailed reentry predictions including time windows
        (WINDOW_START, WINDOW_END) and MAXWINDOW uncertainty. More precise and
        frequently updated than the DECAY class, especially as reentry approaches.

        If *norad_id* is given, returns TIP messages for that object.
        Otherwise returns the most recent TIP messages ordered by insertion time.
        """
        if norad_id is not None:
            path = (
                f"/basicspacedata/query/class/tip"
                f"/NORAD_CAT_ID/{norad_id}"
                f"/orderby/INSERT_EPOCH%20desc"
                f"/limit/{limit}/format/json"
            )
        else:
            path = (
                f"/basicspacedata/query/class/tip"
                f"/orderby/INSERT_EPOCH%20desc"
                f"/limit/{limit}/format/json"
            )
        return self._get(path)

    # ------------------------------------------------------------------
    # ANALYST_SATELLITE — uncatalogued objects under analysis
    # Tracks newly detected or unclassified objects not yet in SATCAT.
    # ------------------------------------------------------------------

    def get_analyst_satellites(self, limit: int = 50) -> list[dict]:
        """Return uncatalogued analyst satellite records.

        The analyst_satellite class contains objects detected by sensors that
        are being tracked but have not yet been formally catalogued in SATCAT.
        Useful for monitoring newly launched or recently detected objects.
        """
        path = (
            f"/basicspacedata/query/class/analyst_satellite"
            f"/orderby/EPOCH%20desc"
            f"/limit/{limit}/format/json"
        )
        return self._get(path)

    # ------------------------------------------------------------------
    # SENSOR — ground-based tracking station catalog
    # Optical and radar sites used to maintain the space catalog.
    # Rarely changes; cache for 24 hours.
    # ------------------------------------------------------------------

    def get_sensors(self) -> list[dict]:
        """Return the Space Surveillance Network sensor catalog.

        Lists ground-based radar and optical tracking stations including
        SENSOR_ID, SENSOR_NAME, SENSOR_TYPE, LATITUDE, LONGITUDE, ALTITUDE,
        and COUNTRY. Useful for understanding which stations track which objects.
        """
        path = "/basicspacedata/query/class/sensor/format/json"
        return self._get(path)

    # ------------------------------------------------------------------
    # MANEUVER — reported satellite maneuver data
    # Time-sensitive; cache for 1 hour.
    # ------------------------------------------------------------------

    def get_maneuvers(
        self,
        norad_id: Optional[int] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Return reported satellite maneuver records.

        The maneuver class contains maneuver notifications for tracked objects,
        including MANEUVER_EPOCH, DELTA_V components, and the resulting orbital
        change. If *norad_id* is provided, returns maneuvers for that satellite;
        otherwise returns the most recently reported maneuvers.
        """
        if norad_id is not None:
            path = (
                f"/basicspacedata/query/class/maneuver"
                f"/NORAD_CAT_ID/{norad_id}"
                f"/orderby/MANEUVER_EPOCH%20desc"
                f"/limit/{limit}/format/json"
            )
        else:
            path = (
                f"/basicspacedata/query/class/maneuver"
                f"/orderby/MANEUVER_EPOCH%20desc"
                f"/limit/{limit}/format/json"
            )
        return self._get(path)


# Module-level singleton — created lazily on first use.
_client: Optional[SpaceTrackClient] = None


def get_client() -> SpaceTrackClient:
    global _client
    if _client is None:
        username = os.getenv("SPACETRACK_USERNAME", "")
        password = os.getenv("SPACETRACK_PASSWORD", "")
        if not username or not password:
            raise EnvironmentError(
                "SPACETRACK_USERNAME and SPACETRACK_PASSWORD must be set"
            )
        _client = SpaceTrackClient(username, password)
    return _client
