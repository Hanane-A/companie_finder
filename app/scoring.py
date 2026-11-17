"""Score de priorité HANUP Desk.

Règles explicites et reproductibles : mêmes données, même score.
Chaque point est expliqué, pour que la commerciale sache pourquoi
un salon passe avant un autre. Les poids sont regroupés ici pour
pouvoir, plus tard, définir un barème par ciblage (restauration…).
"""
from __future__ import annotations

from datetime import date

from app.labels import EFFECTIFS, OUTIL_LABELS

SEUIL_CHAUD = 55
SEUIL_TIEDE = 30

CONCURRENTS = {
    "planity",
    "treatwell",
    "booksy",
    "fresha",
    "kiute",
    "clicrdv",
    "reservio",
    "salonkee",
    "mindbody",
}

POINTS_OUTIL = {
    "aucun": (30, "Aucun outil de réservation repéré"),
    "": (10, "Outil de réservation à vérifier"),
    "google": (10, "Seulement la réservation Google"),
    "autre": (10, "Outil peu connu, remplaçable"),
}

# Tranches d'effectif SIRENE : plusieurs postes = agenda plus difficile à tenir.
POINTS_EFFECTIF = {
    "00": (5, "Pas de salarié"),
    "01": (10, "1 ou 2 salariés"),
    "02": (15, "3 à 5 salariés : plusieurs agendas"),
    "03": (15, "6 à 9 salariés : plusieurs agendas"),
    "11": (10, "10 à 19 salariés"),
}
POINTS_EFFECTIF_INCONNU = (5, "Petite structure probable")
POINTS_GRANDE_STRUCTURE = (5, "Grande structure : décision souvent au siège")

POINTS_STATUT = {
    "contacte": (5, "Déjà en contact"),
    "visite": (5, "Passage déjà fait"),
    "interesse": (15, "S'est dit intéressé"),
    "rdv": (20, "Rendez-vous pris"),
    "demo": (25, "Démo faite"),
    "essai": (30, "Essai en cours"),
    "plus_tard": (5, "À rappeler plus tard"),
}

EFFECTIF_CODES = {label: code for code, label in EFFECTIFS.items()}


def _anciennete_mois(date_creation: str | None, today: date) -> int | None:
    if not date_creation:
        return None
    try:
        start = date.fromisoformat(str(date_creation)[:10])
    except ValueError:
        return None
    return (today.year - start.year) * 12 + today.month - start.month


def niveau_de(score: int) -> str:
    if score >= SEUIL_CHAUD:
        return "chaud"
    if score >= SEUIL_TIEDE:
        return "tiede"
    return "froid"


def compute(prospect: dict, *, today: date | None = None) -> dict:
    """Retourne {score, niveau, raisons}. Les raisons sont des (texte, points)."""
    today = today or date.today()
    statut = prospect.get("statut") or "a_contacter"
    if statut == "client":
        return {"score": 100, "niveau": "client", "raisons": [("Déjà client", 0)]}
    if statut == "refuse":
        return {"score": 0, "niveau": "refuse", "raisons": [("A refusé", 0)]}
    if not int(prospect.get("actif", 1) or 0):
        return {"score": 0, "niveau": "froid", "raisons": [("Établissement fermé", 0)]}

    raisons: list[tuple[str, int]] = []

    outil = prospect.get("outil_reservation") or ""
    if outil in CONCURRENTS:
        raisons.append((f"Déjà équipé ({OUTIL_LABELS.get(outil, outil)}) : argument à préparer", 0))
    elif outil in POINTS_OUTIL:
        raisons.append(POINTS_OUTIL[outil][::-1])

    code = EFFECTIF_CODES.get(prospect.get("effectif") or "")
    if code in POINTS_EFFECTIF:
        raisons.append(POINTS_EFFECTIF[code][::-1])
    elif code:
        raisons.append(POINTS_GRANDE_STRUCTURE[::-1])
    else:
        raisons.append(POINTS_EFFECTIF_INCONNU[::-1])

    mois = _anciennete_mois(prospect.get("date_creation"), today)
    if mois is not None and mois < 12:
        raisons.append(("Ouvert il y a moins d'un an : choisit ses outils", 20))
    elif mois is not None and mois < 36:
        raisons.append(("Ouvert depuis moins de 3 ans", 10))

    if prospect.get("site_presence") == "non":
        raisons.append(("Pas de site web : peu équipé en ligne", 5))

    if statut in POINTS_STATUT:
        raisons.append(POINTS_STATUT[statut][::-1])

    if prospect.get("telephone") or prospect.get("email") or prospect.get("instagram"):
        raisons.append(("Contact direct connu", 5))

    score = min(100, sum(points for _texte, points in raisons))
    return {"score": score, "niveau": niveau_de(score), "raisons": raisons}
