from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from app.labels import NAF_IDS, SANS_RUE, STATUTS
from app.text import fold, fold_key, rue_label, street_number

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "companie.db"

OFFICIAL_FIELDS = {
    "enseigne",
    "quartier",
    "statut",
    "site_presence",
    "site_web",
    "outil_reservation",
    "notes_terrain",
    "notes_maps",
    "notes_reseaux",
}

MANUAL_FIELDS = OFFICIAL_FIELDS | {
    "nom",
    "secteur",
    "code_naf",
    "adresse",
    "numero_voie",
    "type_voie",
    "libelle_voie",
    "code_postal",
    "ville",
    "siret",
    "siren",
    "independant",
    "coworking",
    "source",
    "actif",
    "date_creation",
    "dirigeant",
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS prospects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                siret TEXT UNIQUE,
                siren TEXT,
                nom TEXT NOT NULL,
                enseigne TEXT,
                secteur TEXT,
                code_naf TEXT,
                adresse TEXT,
                numero_voie TEXT,
                type_voie TEXT,
                libelle_voie TEXT,
                code_postal TEXT,
                ville TEXT,
                quartier TEXT,
                latitude TEXT,
                longitude TEXT,
                date_creation TEXT,
                date_creation_unite TEXT,
                actif INTEGER NOT NULL DEFAULT 1,
                effectif TEXT,
                dirigeant TEXT,
                independant INTEGER NOT NULL DEFAULT 0,
                coworking INTEGER NOT NULL DEFAULT 0,
                officiel INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'api_gouv',
                statut TEXT NOT NULL DEFAULT 'a_contacter',
                site_presence TEXT NOT NULL DEFAULT 'inconnu',
                site_web TEXT,
                outil_reservation TEXT,
                notes_terrain TEXT,
                notes_maps TEXT,
                notes_reseaux TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS searches (
                token TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_prospects_siret ON prospects(siret);
            CREATE INDEX IF NOT EXISTS idx_prospects_statut ON prospects(statut);
            CREATE INDEX IF NOT EXISTS idx_prospects_cp ON prospects(code_postal);
            """
        )


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def enrich(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    enseigne = (item.get("enseigne") or "").strip()
    nom = (item.get("nom") or "").strip()
    item["nom_affiche"] = enseigne or nom or "Sans nom"
    item["rue_label"] = rue_label(item.get("type_voie"), item.get("libelle_voie"))
    item["blob"] = " ".join(
        str(item.get(key) or "")
        for key in (
            "nom",
            "enseigne",
            "adresse",
            "siret",
            "siren",
            "ville",
            "code_postal",
            "secteur",
            "code_naf",
            "libelle_voie",
            "type_voie",
            "quartier",
            "dirigeant",
        )
    )
    return item


def count_all() -> int:
    with session() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0])


def get_prospect(prospect_id: int) -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    return enrich(row) if row else None


def get_by_siret(siret: str) -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM prospects WHERE siret = ?", (siret,)).fetchone()
    return enrich(row) if row else None


def siret_ids() -> dict[str, int]:
    with session() as conn:
        rows = conn.execute(
            "SELECT id, siret FROM prospects WHERE siret IS NOT NULL AND siret != ''"
        ).fetchall()
    return {row["siret"]: row["id"] for row in rows}


def query_prospects(
    filters: dict,
    *,
    apply_q: bool = False,
    exclude_statuts: tuple[str, ...] = (),
) -> list[dict]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM prospects").fetchall()
    secteur = filters.get("secteur") or ""
    statut = filters.get("statut") or ""
    source = filters.get("source") or ""
    code_postal = "".join((filters.get("cp") or "").split())
    ville = fold_key(filters.get("ville") or "")
    tokens = fold(filters.get("q") or "").split() if apply_q else []
    excluded = set(exclude_statuts)
    selected: list[dict] = []
    for row in rows:
        item = enrich(row)
        if secteur in NAF_IDS and (item.get("code_naf") or "") != secteur:
            continue
        if secteur == "independant" and not item.get("independant"):
            continue
        if secteur == "coworking" and not item.get("coworking"):
            continue
        if statut and item.get("statut") != statut:
            continue
        if source and item.get("source") != source:
            continue
        if code_postal and (item.get("code_postal") or "") != code_postal:
            continue
        if ville and fold_key(item.get("ville") or "") != ville:
            continue
        if item.get("statut") in excluded:
            continue
        if tokens and not all(token in fold(item["blob"]) for token in tokens):
            continue
        selected.append(item)
    selected.sort(
        key=lambda item: (
            fold(item["nom_affiche"]),
            item.get("code_postal") or "",
            item["id"],
        )
    )
    return selected


def tally(items: list[dict]) -> dict[str, int]:
    counts = {key: 0 for key, _label in STATUTS}
    for item in items:
        key = item.get("statut")
        if key in counts:
            counts[key] += 1
    counts["total"] = len(items)
    return counts


def upsert_imported(items: list[dict]) -> tuple[int, int]:
    inserted = 0
    updated = 0
    timestamp = now()
    with session() as conn:
        for item in items:
            existing = conn.execute(
                "SELECT id FROM prospects WHERE siret = ?",
                (item["siret"],),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO prospects (
                        siret, siren, nom, enseigne, secteur, code_naf, adresse,
                        numero_voie, type_voie, libelle_voie, code_postal, ville, quartier,
                        latitude, longitude, date_creation, date_creation_unite, actif,
                        effectif, dirigeant, independant, coworking, officiel, source,
                        statut, site_presence, site_web, outil_reservation,
                        notes_terrain, notes_maps, notes_reseaux, created_at, updated_at
                    ) VALUES (
                        :siret, :siren, :nom, :enseigne, :secteur, :code_naf, :adresse,
                        :numero_voie, :type_voie, :libelle_voie, :code_postal, :ville, :quartier,
                        :latitude, :longitude, :date_creation, :date_creation_unite, :actif,
                        :effectif, :dirigeant, :independant, :coworking, 1, 'api_gouv',
                        'a_contacter', 'inconnu', '', '',
                        '', '', '', :created_at, :updated_at
                    )
                    """,
                    {**item, "created_at": timestamp, "updated_at": timestamp},
                )
                inserted += 1
                continue
            conn.execute(
                """
                UPDATE prospects SET
                    siren = :siren,
                    nom = :nom,
                    secteur = :secteur,
                    code_naf = :code_naf,
                    adresse = :adresse,
                    numero_voie = :numero_voie,
                    type_voie = :type_voie,
                    libelle_voie = :libelle_voie,
                    code_postal = :code_postal,
                    ville = :ville,
                    latitude = :latitude,
                    longitude = :longitude,
                    date_creation = :date_creation,
                    date_creation_unite = :date_creation_unite,
                    actif = :actif,
                    effectif = :effectif,
                    dirigeant = :dirigeant,
                    independant = CASE WHEN independant = 1 OR :independant = 1 THEN 1 ELSE 0 END,
                    coworking = CASE WHEN coworking = 1 OR :coworking = 1 THEN 1 ELSE 0 END,
                    officiel = 1,
                    enseigne = CASE
                        WHEN enseigne IS NULL OR enseigne = '' THEN :enseigne
                        ELSE enseigne
                    END,
                    quartier = CASE
                        WHEN quartier IS NULL OR quartier = '' THEN :quartier
                        ELSE quartier
                    END,
                    updated_at = :updated_at
                WHERE siret = :siret
                """,
                {**item, "updated_at": timestamp},
            )
            updated += 1
    return inserted, updated


def create_manual(values: dict) -> int:
    timestamp = now()
    payload = {
        "siret": values.get("siret"),
        "siren": values.get("siren") or "",
        "nom": values["nom"],
        "enseigne": values.get("enseigne") or "",
        "secteur": values.get("secteur") or "",
        "code_naf": values.get("code_naf") or "",
        "adresse": values.get("adresse") or "",
        "numero_voie": values.get("numero_voie") or "",
        "type_voie": values.get("type_voie") or "",
        "libelle_voie": values.get("libelle_voie") or "",
        "code_postal": values.get("code_postal") or "",
        "ville": values.get("ville") or "",
        "quartier": values.get("quartier") or "",
        "latitude": "",
        "longitude": "",
        "date_creation": values.get("date_creation") or "",
        "date_creation_unite": "",
        "actif": int(values.get("actif", 1)),
        "effectif": "",
        "dirigeant": values.get("dirigeant") or "",
        "independant": int(values.get("independant") or 0),
        "coworking": int(values.get("coworking") or 0),
        "source": values.get("source") or "maps",
        "statut": values.get("statut") or "a_contacter",
        "site_presence": values.get("site_presence") or "inconnu",
        "site_web": values.get("site_web") or "",
        "outil_reservation": values.get("outil_reservation") or "",
        "notes_terrain": values.get("notes_terrain") or "",
        "notes_maps": values.get("notes_maps") or "",
        "notes_reseaux": values.get("notes_reseaux") or "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with session() as conn:
        cursor = conn.execute(
            """
            INSERT INTO prospects (
                siret, siren, nom, enseigne, secteur, code_naf, adresse,
                numero_voie, type_voie, libelle_voie, code_postal, ville, quartier,
                latitude, longitude, date_creation, date_creation_unite, actif,
                effectif, dirigeant, independant, coworking, officiel, source,
                statut, site_presence, site_web, outil_reservation,
                notes_terrain, notes_maps, notes_reseaux, created_at, updated_at
            ) VALUES (
                :siret, :siren, :nom, :enseigne, :secteur, :code_naf, :adresse,
                :numero_voie, :type_voie, :libelle_voie, :code_postal, :ville, :quartier,
                :latitude, :longitude, :date_creation, :date_creation_unite, :actif,
                :effectif, :dirigeant, :independant, :coworking, 0, :source,
                :statut, :site_presence, :site_web, :outil_reservation,
                :notes_terrain, :notes_maps, :notes_reseaux, :created_at, :updated_at
            )
            """,
            payload,
        )
        return int(cursor.lastrowid)


def update_prospect(prospect_id: int, values: dict, *, official: bool) -> None:
    allowed = OFFICIAL_FIELDS if official else MANUAL_FIELDS
    clean = {key: values[key] for key in values if key in allowed}
    if not clean:
        return
    clean["updated_at"] = now()
    clean["id"] = prospect_id
    assignments = ", ".join(f"{key} = :{key}" for key in clean if key != "id")
    with session() as conn:
        conn.execute(
            f"UPDATE prospects SET {assignments} WHERE id = :id",
            clean,
        )


def set_statut(prospect_id: int, statut: str) -> bool:
    with session() as conn:
        cursor = conn.execute(
            "UPDATE prospects SET statut = ?, updated_at = ? WHERE id = ?",
            (statut, now(), prospect_id),
        )
        return cursor.rowcount > 0


def delete_prospect(prospect_id: int) -> bool:
    with session() as conn:
        cursor = conn.execute("DELETE FROM prospects WHERE id = ?", (prospect_id,))
        return cursor.rowcount > 0


def save_search(token: str, payload: dict) -> None:
    cutoff = (datetime.now() - timedelta(hours=12)).isoformat(timespec="seconds")
    with session() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO searches (token, payload, created_at) VALUES (?, ?, ?)",
            (token, json.dumps(payload, ensure_ascii=False), now()),
        )
        conn.execute("DELETE FROM searches WHERE created_at < ?", (cutoff,))


def load_search(token: str) -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT payload FROM searches WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None
    return json.loads(row["payload"])


def group_by_street(items: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for item in items:
        label = item.get("rue_label") or SANS_RUE
        groups.setdefault(label, []).append(item)
    ordered = []
    for label, rows in groups.items():
        rows.sort(key=lambda item: (street_number(item.get("numero_voie")), fold(item["nom_affiche"])))
        ordered.append({"rue": label, "stops": rows})
    ordered.sort(key=lambda group: (group["rue"] == SANS_RUE, fold(group["rue"])))
    return ordered
