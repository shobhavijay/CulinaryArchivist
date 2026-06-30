import logging
from datetime import datetime
from culinary_archivist.state import ArchivistState
from culinary_archivist import config

log = logging.getLogger(__name__)

_CURRENT_YEAR = datetime.now().year


def pre_flight_router(state: ArchivistState) -> dict:
    """
    Determines mode: "express" or "full".
    Explicit mode flag always wins over auto-detect.
    Writes resolved mode back into state.
    """
    explicit = state.get("mode", "").strip().lower()
    if explicit in ("express", "full"):
        log.info("Pre-flight router: explicit mode=%s", explicit)
        return {"mode": explicit}

    quality = state.get("media_quality_score", 0.0)
    year = state.get("media_date_detected", 0)

    high_quality = quality >= config.AUTO_EXPRESS_QUALITY_THRESHOLD
    recent = year > 0 and (_CURRENT_YEAR - year) <= config.AUTO_EXPRESS_MAX_AGE_YEARS

    mode = "express" if (high_quality and recent) else "full"
    log.info(
        "Pre-flight router: auto-detect quality=%.2f year=%s → mode=%s",
        quality, year, mode,
    )
    return {"mode": mode}


def route_after_preflight(state: ArchivistState) -> str:
    """Edge function: returns the next node name based on resolved mode."""
    return state.get("mode", "full")
