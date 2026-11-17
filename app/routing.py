"""Optimisation de tournée.

Le moteur est indépendant de l'interface et de la base : il reçoit des
points et une matrice de temps, et rend un ordre de passage avec les
heures d'arrivée. Mêmes entrées = même tournée (aucun hasard).

Ordre de passage : plus proche voisin, puis amélioration 2-opt
(on inverse des portions de trajet tant que cela fait gagner du temps).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

MODES = (
    ("pied", "À pied"),
    ("transports", "Transports en commun"),
    ("velo", "Vélo"),
    ("voiture", "Voiture"),
)
MODE_IDS = {value for value, _label in MODES}
GOOGLE_TRAVELMODE = {
    "pied": "walking",
    "transports": "transit",
    "velo": "bicycling",
    "voiture": "driving",
}
ORS_PROFILES = {
    "pied": "foot-walking",
    "velo": "cycling-regular",
    "voiture": "driving-car",
}

# Les rues ne sont pas droites : on rallonge la distance à vol d'oiseau.
DETOUR = 1.3
VITESSE_KMH = {"pied": 4.5, "velo": 14.0, "voiture": 22.0}
STATIONNEMENT_MIN = 3  # garer la voiture à chaque arrêt
TRANSPORTS_VITESSE_KMH = 18.0
TRANSPORTS_ATTENTE_MIN = 8  # marche jusqu'à l'arrêt et attente
TRANSPORTS_SEUIL_MARCHE_M = 900  # en dessous, on marche
MAX_ARRETS = 50


@dataclass
class Stop:
    id: int
    lat: float
    lon: float


@dataclass
class Leg:
    minutes: float
    metres: float
    a_pied: bool = False


@dataclass
class Visit:
    stop_id: int
    order: int
    arrival: datetime
    departure: datetime
    leg: Leg
    hors_creneau: bool = False


@dataclass
class Plan:
    visits: list[Visit] = field(default_factory=list)
    trajet_min: float = 0.0
    distance_m: float = 0.0
    sur_place_min: float = 0.0
    fin: datetime | None = None
    source: str = "estimation"


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def estimate_leg(a: tuple[float, float], b: tuple[float, float], mode: str) -> Leg:
    metres = haversine_m(a, b) * DETOUR
    if metres < 1:
        return Leg(0.0, 0.0, a_pied=mode in ("pied", "transports"))
    if mode == "transports":
        if metres <= TRANSPORTS_SEUIL_MARCHE_M:
            return Leg(metres / 1000 / VITESSE_KMH["pied"] * 60, metres, a_pied=True)
        return Leg(TRANSPORTS_ATTENTE_MIN + metres / 1000 / TRANSPORTS_VITESSE_KMH * 60, metres)
    minutes = metres / 1000 / VITESSE_KMH[mode] * 60
    if mode == "voiture":
        minutes += STATIONNEMENT_MIN
    return Leg(minutes, metres, a_pied=mode == "pied")


def estimate_matrix(points: list[tuple[float, float]], mode: str) -> list[list[Leg]]:
    return [[estimate_leg(a, b, mode) for b in points] for a in points]


def _cost(matrix: list[list[Leg]], i: int, j: int) -> float:
    return matrix[i][j].minutes


def nearest_neighbour(matrix: list[list[Leg]], n_stops: int) -> list[int]:
    """Indices 1..n_stops dans l'ordre ; 0 est le point de départ."""
    remaining = list(range(1, n_stops + 1))
    order: list[int] = []
    current = 0
    while remaining:
        # À égalité, le plus petit indice gagne : résultat stable.
        best = min(remaining, key=lambda j: (_cost(matrix, current, j), j))
        order.append(best)
        remaining.remove(best)
        current = best
    return order


def path_cost(matrix: list[list[Leg]], order: list[int], *, retour: bool) -> float:
    total = 0.0
    previous = 0
    for index in order:
        total += _cost(matrix, previous, index)
        previous = index
    if retour and order:
        total += _cost(matrix, previous, 0)
    return total


def two_opt(matrix: list[list[Leg]], order: list[int], *, retour: bool, max_passes: int = 50) -> list[int]:
    best = list(order)
    best_cost = path_cost(matrix, best, retour=retour)
    for _ in range(max_passes):
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 1, len(best)):
                candidate = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                cost = path_cost(matrix, candidate, retour=retour)
                if cost < best_cost - 1e-9:
                    best, best_cost = candidate, cost
                    improved = True
        if not improved:
            break
    return best


def optimise(
    start: tuple[float, float],
    stops: list[Stop],
    *,
    mode: str,
    depart: datetime,
    visite_min: int,
    fin_souhaitee: datetime | None = None,
    retour: bool = False,
    matrix: list[list[Leg]] | None = None,
    source: str = "estimation",
) -> Plan:
    if not stops:
        return Plan(fin=depart, source=source)
    points = [start] + [(stop.lat, stop.lon) for stop in stops]
    if matrix is None:
        matrix = estimate_matrix(points, mode)
        source = "estimation"
    order = two_opt(matrix, nearest_neighbour(matrix, len(stops)), retour=retour)

    plan = Plan(source=source)
    clock = depart
    previous = 0
    for position, index in enumerate(order, start=1):
        leg = matrix[previous][index]
        arrival = clock + timedelta(minutes=leg.minutes)
        departure = arrival + timedelta(minutes=visite_min)
        plan.visits.append(
            Visit(
                stop_id=stops[index - 1].id,
                order=position,
                arrival=arrival,
                departure=departure,
                leg=leg,
                hors_creneau=bool(fin_souhaitee and departure > fin_souhaitee),
            )
        )
        plan.trajet_min += leg.minutes
        plan.distance_m += leg.metres
        plan.sur_place_min += visite_min
        clock = departure
        previous = index
    if retour:
        leg = matrix[previous][0]
        plan.trajet_min += leg.minutes
        plan.distance_m += leg.metres
        clock += timedelta(minutes=leg.minutes)
    plan.fin = clock
    return plan


def google_maps_url(points: list[tuple[float, float]], mode: str) -> str:
    """Itinéraire Google Maps entre plusieurs points (origine, étapes, destination)."""
    if len(points) < 2:
        return ""
    fmt = lambda p: f"{p[0]:.6f},{p[1]:.6f}"
    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={fmt(points[0])}&destination={fmt(points[-1])}"
        f"&travelmode={GOOGLE_TRAVELMODE.get(mode, 'walking')}"
    )
    middle = points[1:-1]
    if middle:
        url += "&waypoints=" + "%7C".join(fmt(p) for p in middle)
    return url


def google_maps_chunks(points: list[tuple[float, float]], mode: str, size: int = 10) -> list[str]:
    """Découpe la tournée en tronçons (Google Maps limite le nombre d'étapes).

    En transports en commun, Google Maps n'accepte pas d'étapes : un lien par trajet.
    """
    if mode == "transports":
        size = 1
    urls = []
    start = 0
    while start < len(points) - 1:
        chunk = points[start : start + size + 1]
        urls.append(google_maps_url(chunk, mode))
        start += size
    return urls
