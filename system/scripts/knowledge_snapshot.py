"""Read the atomic records/coverage generation; never hide corrupt snapshots."""
from __future__ import annotations

import json
from pathlib import Path


def load_snapshot(directory: Path) -> dict | None:
    try:
        value = json.loads((directory / 'snapshot.json').read_text())
    except FileNotFoundError:
        return None  # Compatibility with stores that predate atomic publication.
    if (not isinstance(value, dict) or value.get('version') != 1
            or not isinstance(value.get('coverage'), dict)
            or not isinstance(value.get('records'), list)
            or any(not isinstance(row, dict) for row in value['records'])
            or value['coverage'].get('records_total') != len(value['records'])):
        raise ValueError('Invalid knowledge snapshot; refusing legacy fallback')
    return value
