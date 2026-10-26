from __future__ import annotations

import csv
import io
from datetime import datetime

from app.labels import OUTIL_LABELS, SITE_LABELS, SOURCE_LABELS, STATUT_LABELS
from app.text import csv_safe, fr_date

COLUMNS = (
    "Identifiant",
    "Nom / Enseigne",
    "Raison sociale",
    "Secteur",
    "Code NAF",
    "Indépendant",
    "Coworking",
    "Adresse",
    "Numéro",
    "Rue",
    "Code postal",
    "Ville",
    "Quartier",
    "SIRET",
    "SIREN",
    "Date création établissement",
    "Date création entreprise",
    "Actif",
    "Effectif établissement",
    "Dirigeant",
    "Source",
    "Statut",
    "Présence site",
    "Site web",
    "Outil de réservation",
    "Notes terrain",
    "Notes Google Maps",
    "Notes Instagram",
)


def filename() -> str:
    return f"companie-finder-{datetime.now():%Y%m%d}.csv"


def render_csv(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow(
            [
                csv_safe(row.get("id")),
                csv_safe(row.get("nom_affiche")),
                csv_safe(row.get("nom")),
                csv_safe(row.get("secteur")),
                csv_safe(row.get("code_naf")),
                "Oui" if row.get("independant") else "Non",
                "Oui" if row.get("coworking") else "Non",
                csv_safe(row.get("adresse")),
                csv_safe(row.get("numero_voie")),
                csv_safe(row.get("rue_label")),
                csv_safe(row.get("code_postal")),
                csv_safe(row.get("ville")),
                csv_safe(row.get("quartier")),
                csv_safe(row.get("siret")),
                csv_safe(row.get("siren")),
                csv_safe(fr_date(row.get("date_creation"))),
                csv_safe(fr_date(row.get("date_creation_unite"))),
                "Oui" if row.get("actif") else "Non",
                csv_safe(row.get("effectif")),
                csv_safe(row.get("dirigeant")),
                csv_safe(SOURCE_LABELS.get(row.get("source") or "", row.get("source") or "")),
                csv_safe(STATUT_LABELS.get(row.get("statut") or "", row.get("statut") or "")),
                csv_safe(SITE_LABELS.get(row.get("site_presence") or "", row.get("site_presence") or "")),
                csv_safe(row.get("site_web")),
                csv_safe(OUTIL_LABELS.get(row.get("outil_reservation") or "", row.get("outil_reservation") or "")),
                csv_safe(row.get("notes_terrain")),
                csv_safe(row.get("notes_maps")),
                csv_safe(row.get("notes_reseaux")),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")
