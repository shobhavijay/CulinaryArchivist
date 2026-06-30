"""
Registry loader — reads all YAML files in the registry directory
and exposes them as a dict keyed by region_id.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

import yaml

from culinary_archivist import config

log = logging.getLogger(__name__)

_CACHE: Dict[str, dict] | None = None


def load_registry() -> Dict[str, dict]:
    """
    Load all *.yaml files from REGISTRY_DIR.
    Returns a dict: { region_id -> registry_dict }
    Results are cached after the first call.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    registry_dir = Path(config.REGISTRY_DIR)
    if not registry_dir.exists():
        log.warning("Registry directory not found: %s", registry_dir)
        return {}

    result: Dict[str, dict] = {}
    for yaml_file in sorted(registry_dir.glob("*.yaml")):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            region_id = data.get("region_id")
            if not region_id:
                log.warning("Skipping %s — no region_id found", yaml_file.name)
                continue
            result[region_id] = data
            log.info("Loaded registry: %s (%s)", region_id, yaml_file.name)
        except Exception as e:
            log.error("Failed to load registry %s: %s", yaml_file.name, e)

    _CACHE = result
    log.info("Registry loaded: %d regions — %s", len(result), list(result.keys()))
    return result


def get_region(region_id: str) -> dict | None:
    """Return a single registry entry by region_id, or None if not found."""
    return load_registry().get(region_id)
