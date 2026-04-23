# spacetrack-mcp

An MCP (Model Context Protocol) server for [Space-Track.org](https://www.space-track.org) that exposes satellite catalog data, Two-Line Elements (TLEs), orbital propagation, conjunction warnings, and reentry predictions as tools consumable by Claude Desktop and any other MCP-compatible AI client.

## What it does

`spacetrack-mcp` connects to the Space-Track.org REST API and wraps 13 data endpoints as MCP tools. When added to Claude Desktop, Claude can answer questions like:

- "Where is the ISS right now?"
- "Show me all active Starlink satellites launched by the US."
- "Are there any conjunction warnings for NORAD ID 25544?"
- "What objects are predicted to reenter in the next 30 days?"
- "How many debris objects does each country have in orbit?"

All responses are cached (Redis when available, in-memory otherwise) to respect Space-Track's rate limits and usage guidelines.

## Prerequisites

- Python 3.11 or later
- A free [Space-Track.org account](https://www.space-track.org/auth/createAccount)
- (Optional) A running Redis instance for persistent caching

## Installation

### From PyPI

```bash
pip install spacetrack-mcp
```

### With Redis support

```bash
pip install "spacetrack-mcp[redis]"
```

### Using uvx (no install required)

```bash
uvx spacetrack-mcp
```

## Configuration

The server reads credentials from environment variables. Copy `.env.example` to `.env` and fill in your Space-Track credentials:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `SPACETRACK_USERNAME` | Yes | Your Space-Track.org login email |
| `SPACETRACK_PASSWORD` | Yes | Your Space-Track.org password |
| `REDIS_URL` | No | Redis connection URL (e.g. `redis://localhost:6379`). Falls back to in-memory cache if not set. |

## Claude Desktop setup

Add the following block to your `claude_desktop_config.json` (typically found at `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "spacetrack": {
      "command": "spacetrack-mcp",
      "env": {
        "SPACETRACK_USERNAME": "your_username",
        "SPACETRACK_PASSWORD": "your_password"
      }
    }
  }
}
```

If you installed with `uvx`, use:

```json
{
  "mcpServers": {
    "spacetrack": {
      "command": "uvx",
      "args": ["spacetrack-mcp"],
      "env": {
        "SPACETRACK_USERNAME": "your_username",
        "SPACETRACK_PASSWORD": "your_password"
      }
    }
  }
}
```

After saving the config, restart Claude Desktop. You should see the spacetrack tools listed in the tool picker.

## Running directly

```bash
# Using the installed CLI entry point
spacetrack-mcp

# Using the Python module
python -m spacetrack_mcp
```

Both run the server over **stdio transport**, which is what Claude Desktop expects.

## MCP Tools Reference

### SATCAT (Satellite Catalog)

#### `search_satellites`
Search the satellite catalog by name, NORAD ID, country, or object type.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | Yes | Satellite name fragment or NORAD ID number |
| `object_type` | string | No | Filter: `PAYLOAD`, `ROCKET BODY`, `DEBRIS`, or `UNKNOWN` |
| `country` | string | No | Two-letter country code (e.g. `US`, `CN`, `RU`) |
| `limit` | integer | No | Maximum results (default 20, max 100) |

Returns: List of records with `NORAD_CAT_ID`, `OBJECT_NAME`, `COUNTRY`, `LAUNCH_DATE`, `DECAY_DATE`, `OBJECT_TYPE`, `RCS_SIZE`.

#### `get_satellite`
Return the full SATCAT record for a single satellite.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `norad_id` | integer | Yes | NORAD catalog number (e.g. `25544` for ISS) |

Returns: Complete SATCAT record dict, or empty dict if not found.

### TLE Data

#### `get_tle`
Return the latest Two-Line Element set for a satellite.

Queries the current `GP` class (replaces deprecated `tle_latest`). Applies a propagable filter (on-orbit only, epoch within the last 10 days).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `norad_id` | integer | Yes | NORAD catalog number |

Returns: Dict with `name`, `line1`, `line2`, `epoch`.

#### `get_tle_history`
Return recent historical TLEs for a satellite.

Queries `GP_History` (replaces deprecated `tle` class). Cached for 6 hours per Space-Track usage guidelines.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `norad_id` | integer | Yes | NORAD catalog number |
| `limit` | integer | No | Number of historical TLEs (default 10) |

Returns: List of `{name, line1, line2, epoch}` dicts, newest first.

### Orbital Mechanics

#### `propagate_orbit`
Compute the ground track of a satellite using SGP4 propagation.

Fetches the latest TLE, then propagates the orbit forward from the current time using the SGP4 model. Includes ECI-to-geodetic conversion using the Bowring iterative method.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `norad_id` | integer | Yes | NORAD catalog number |
| `steps` | integer | No | Number of propagation steps (default 90 ≈ one orbit) |
| `step_minutes` | number | No | Minutes between steps (default 1) |

Returns: List of `{time, lat, lon, alt_km, velocity_km_s}` dicts.

### Conjunction (Close Approach) Warnings

#### `get_conjunctions`
Return upcoming conjunction warnings for a satellite.

Queries `CDM_PUBLIC` (Conjunction Data Messages) for events where the satellite appears as either the primary or secondary object. Only future TCAs are included, ordered soonest first.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `norad_id` | integer | Yes | NORAD catalog number |
| `limit` | integer | No | Maximum events to return (default 20) |

Returns: List of conjunction records with `TCA`, `MISS_DISTANCE`, `COLLISION_PROBABILITY`, `SAT_1_NAME`, `SAT_2_NAME`, `RELATIVE_SPEED`.

### Reentry Predictions

#### `get_decay_predictions`
Return reentry predictions from the DECAY class.

Without a NORAD ID, returns the next predicted reentries across all tracked objects. With a NORAD ID, returns all decay records for that specific object.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `norad_id` | integer | No | NORAD catalog number. Omit for all upcoming reentries. |
| `limit` | integer | No | Maximum records to return (default 50) |

Returns: List of decay records with `NORAD_CAT_ID`, `OBJECT_NAME`, `DECAY_EPOCH`, `COUNTRY`, `RCS`, `MSG_EPOCH`.

#### `get_tip`
Return TIP (Tracking and Impact Prediction) messages.

More precise and frequently updated than `get_decay_predictions`, especially in the hours and days before reentry. Includes prediction time windows.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `norad_id` | integer | No | NORAD catalog number. Omit for most recent TIPs across all objects. |
| `limit` | integer | No | Maximum records to return (default 20) |

Returns: List of TIP records with `NORAD_CAT_ID`, `OBJECT_NAME`, `WINDOW_START`, `WINDOW_END`, `DECAY_EPOCH`, `MAXWINDOW`, `COUNTRY`, `INSERT_EPOCH`.

### Catalog Statistics

#### `get_boxscore`
Return catalog aggregate statistics broken down by country.

Shows counts of payloads, rocket bodies, and debris on-orbit and decayed, grouped by country/operator. No parameters required.

Returns: List of records per country with `ORBITAL_PAYLOAD_COUNT`, `ORBITAL_ROCKET_BODY_COUNT`, `ORBITAL_DEBRIS_COUNT`, `ORBITAL_TOTAL_COUNT`, `DECAYED_TOTAL_COUNT`, `COUNTRY_TOTAL`.

#### `get_launch_sites`
Return the complete Space-Track launch site reference table.

Maps `SITE_CODE` values (used in SATCAT records) to human-readable launch site names. No parameters required.

Returns: List of records with `SITE_CODE` and `LAUNCH_SITE`.

### Space Surveillance Network

#### `get_analyst_satellites`
Return uncatalogued analyst satellite records.

Contains recently detected objects tracked by the Space Surveillance Network that have not yet received an official NORAD catalog number. Useful for monitoring newly launched payloads or debris events.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | No | Maximum records, ordered by most recent epoch (default 50) |

Returns: List of analyst satellite records with `OBJECT_NAME`, `OBJECT_ID`, `EPOCH`, and TLE orbital element fields.

#### `get_sensors`
Return the Space Surveillance Network ground sensor catalog.

Lists all radar and optical tracking stations with geographic positions and sensor type. No parameters required.

Returns: List of sensor records with `SENSOR_ID`, `SENSOR_NAME`, `SENSOR_TYPE`, `LATITUDE`, `LONGITUDE`, `ALTITUDE`, `COUNTRY`.

#### `get_maneuvers`
Return reported satellite maneuver records.

Includes maneuver epoch, delta-V components, and the resulting orbital change. Useful for identifying active satellites and studying orbit maintenance patterns.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `norad_id` | integer | No | NORAD catalog number. Omit for most recently reported maneuvers across all satellites. |
| `limit` | integer | No | Maximum records to return (default 20) |

Returns: List of maneuver records with `NORAD_CAT_ID`, `OBJECT_NAME`, `MANEUVER_EPOCH`, `DELTA_V1`, `DELTA_V2`, `DELTA_V3`.

## Caching

Response caching is automatic. When `REDIS_URL` is set and Redis is reachable, responses are cached in Redis. Otherwise, an in-memory cache is used (data is lost on server restart, but works with no dependencies).

| Data type | TTL |
|---|---|
| SATCAT records | 24 hours |
| Current TLEs (GP) | 1 hour |
| Historical TLEs (GP_History) | 6 hours |
| Orbit propagation | 5 minutes |
| Conjunction warnings | 1 hour |
| Decay predictions | 6 hours |
| Boxscore | 24 hours |
| Launch sites | 7 days |
| TIP messages | 1 hour |
| Analyst satellites | 1 hour |
| Sensors | 24 hours |
| Maneuvers | 1 hour |

## Rate Limiting

The client enforces Space-Track's usage guidelines proactively:
- Sliding-window limiter: 25 requests/minute, 280 requests/hour (below the hard limits of 30/min, 300/hr)
- Automatic exponential backoff on HTTP 429 responses (4s, 8s, 16s)
- Session re-authentication on HTTP 401

## Development Setup

```bash
git clone https://github.com/your-org/spacetrack-mcp
cd spacetrack-mcp

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install in editable mode with optional Redis support
pip install -e ".[redis]"

# Configure credentials
cp .env.example .env
# Edit .env with your Space-Track credentials

# Run the server
spacetrack-mcp
# or
python -m spacetrack_mcp
```

### Project Structure

```
spacetrack-mcp/
├── src/
│   └── spacetrack_mcp/
│       ├── __init__.py     # Package metadata
│       ├── __main__.py     # python -m spacetrack_mcp entry point
│       ├── server.py       # FastMCP app + all 13 tool definitions
│       ├── client.py       # Space-Track API client with rate limiting
│       └── cache.py        # Redis/in-memory cache with TTL
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
└── LICENSE
```

### Adding New Tools

1. Add the API call method to `src/spacetrack_mcp/client.py`
2. Add a TTL constant to `src/spacetrack_mcp/cache.py`
3. Decorate a new function with `@mcp.tool` in `src/spacetrack_mcp/server.py`

FastMCP introspects the function signature and docstring to generate the MCP tool schema automatically.

## License

MIT — see [LICENSE](LICENSE)
