"""Temps de trajet réels via OpenRouteService (facultatif).

Activé seulement si la variable d'environnement ORS_API_KEY existe.
Sans clé, ou si le service échoue, la tournée utilise l'estimation
à vol d'oiseau : l'outil ne tombe jamais en panne à cause du réseau.
"""
from __future__ import annotations

import logging
import os

import httpx

from app.routing import ORS_PROFILES, Leg

logger = logging.getLogger("companie.ors")
MATRIX_URL = "https://api.openrouteservice.org/v2/matrix/{profile}"
MAX_LOCATIONS = 59  # 59 × 59 reste sous la limite d'éléments du plan gratuit


def api_key() -> str:
    return os.environ.get("ORS_API_KEY", "").strip()


async def fetch_matrix(
    client: httpx.AsyncClient,
    points: list[tuple[float, float]],
    mode: str,
    *,
    key: str | None = None,
) -> list[list[Leg]] | None:
    key = key if key is not None else api_key()
    profile = ORS_PROFILES.get(mode)
    if not key or not profile or len(points) < 2 or len(points) > MAX_LOCATIONS:
        return None
    try:
        response = await client.post(
            MATRIX_URL.format(profile=profile),
            headers={"Authorization": key, "Content-Type": "application/json"},
            json={
                "locations": [[lon, lat] for lat, lon in points],
                "metrics": ["duration", "distance"],
            },
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
        durations = payload["durations"]
        distances = payload["distances"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("OpenRouteService indisponible (%s), estimation utilisée", exc.__class__.__name__)
        return None
    matrix: list[list[Leg]] = []
    for i, row in enumerate(durations):
        legs = []
        for j, seconds in enumerate(row):
            if seconds is None:
                return None  # point inaccessible : on préfère l'estimation
            minutes = seconds / 60
            if mode == "voiture" and i != j:
                minutes += 3  # stationnement
            legs.append(Leg(minutes, float(distances[i][j] or 0), a_pied=mode == "pied"))
        matrix.append(legs)
    return matrix
