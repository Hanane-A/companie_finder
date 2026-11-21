from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from app import scoring
from app.labels import NAF_IDS, SANS_RUE, STATUTS
from app.text import fold, fold_key, rue_label, street_number

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "companie.db"

CONTACT_FIELDS = {
    "contact_nom",
    "contact_role",
    "telephone",
    "email",
    "instagram",
    "prochaine_action",
    "prochaine_action_at",
}

# Colonnes ajoutées après la première version : la migration les crée
# sur une base existante sans perdre les fiches déjà saisies.
NEW_COLUMNS = (
    ("contact_nom", "TEXT NOT NULL DEFAULT ''"),
    ("contact_role", "TEXT NOT NULL DEFAULT ''"),
    ("telephone", "TEXT NOT NULL DEFAULT ''"),
    ("email", "TEXT NOT NULL DEFAULT ''"),
    ("instagram", "TEXT NOT NULL DEFAULT ''"),
    ("prochaine_action", "TEXT NOT NULL DEFAULT ''"),
    ("prochaine_action_at", "TEXT NOT NULL DEFAULT ''"),
    ("dernier_contact_at", "TEXT NOT NULL DEFAULT ''"),
)

OFFICIAL_FIELDS = CONTACT_FIELDS | {
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
    backup_before_migration()
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

            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
                type TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                statut_avant TEXT NOT NULL DEFAULT '',
                statut_apres TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_interactions_prospect
                ON interactions(prospect_id, occurred_at);

            CREATE TABLE IF NOT EXISTS tournees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                jour TEXT NOT NULL,
                mode TEXT NOT NULL,
                depart_label TEXT NOT NULL DEFAULT '',
                arrets TEXT NOT NULL,
                created_at TEXT NOT NULL,
                saisie_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        migrate(conn)


def backup_before_migration() -> None:
    """Copie la base avant d'y ajouter des colonnes, une seule fois."""
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "prospects" not in tables:
            return
        columns = {row[1] for row in conn.execute("PRAGMA table_info(prospects)")}
    finally:
        conn.close()
    if all(name in columns for name, _definition in NEW_COLUMNS):
        return
    target = DB_PATH.with_name(f"{DB_PATH.stem}.avant-migration-{datetime.now():%Y%m%d-%H%M%S}.db")
    shutil.copy2(DB_PATH, target)


def migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(prospects)")}
    for name, definition in NEW_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE prospects ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prospects_relance ON prospects(prochaine_action_at)"
    )


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
            "contact_nom",
            "telephone",
            "email",
            "instagram",
        )
    )
    if item.get("telephone"):
        # Retrouver un numéro tapé sans espaces.
        item["blob"] += " " + str(item["telephone"]).replace(" ", "")
    echeance = item.get("prochaine_action_at") or ""
    item["relance_due"] = bool(echeance) and echeance <= today()
    if "statut" in item:
        resultat = scoring.compute(item)
        item["score"] = resultat["score"]
        item["niveau"] = resultat["niveau"]
        item["raisons"] = resultat["raisons"]
    return item


def today() -> str:
    return date.today().isoformat()


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
                    -- Une position déjà calculée n'est pas effacée par un import sans coordonnées.
                    latitude = CASE WHEN :latitude IS NULL OR :latitude = '' THEN latitude ELSE :latitude END,
                    longitude = CASE WHEN :longitude IS NULL OR :longitude = '' THEN longitude ELSE :longitude END,
                    date_creation = :date_creation,
                    date_creation_unite = :date_creation_unite,
                    -- Une fermeture constatée sur le terrain reste, même si SIRENE n'est pas à jour.
                    actif = CASE WHEN :actif = 0 THEN 0 ELSE actif END,
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
        **{key: values.get(key) or "" for key in CONTACT_FIELDS},
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
                notes_terrain, notes_maps, notes_reseaux,
                contact_nom, contact_role, telephone, email, instagram,
                prochaine_action, prochaine_action_at, created_at, updated_at
            ) VALUES (
                :siret, :siren, :nom, :enseigne, :secteur, :code_naf, :adresse,
                :numero_voie, :type_voie, :libelle_voie, :code_postal, :ville, :quartier,
                :latitude, :longitude, :date_creation, :date_creation_unite, :actif,
                :effectif, :dirigeant, :independant, :coworking, 0, :source,
                :statut, :site_presence, :site_web, :outil_reservation,
                :notes_terrain, :notes_maps, :notes_reseaux,
                :contact_nom, :contact_role, :telephone, :email, :instagram,
                :prochaine_action, :prochaine_action_at, :created_at, :updated_at
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
    timestamp = now()
    clean["updated_at"] = timestamp
    clean["id"] = prospect_id
    assignments = ", ".join(f"{key} = :{key}" for key in clean if key != "id")
    with session() as conn:
        row = conn.execute("SELECT statut FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
        if row is None:
            return
        conn.execute(
            f"UPDATE prospects SET {assignments} WHERE id = :id",
            clean,
        )
        new_statut = clean.get("statut")
        if new_statut and new_statut != row["statut"]:
            _log_statut(conn, prospect_id, row["statut"], new_statut, timestamp)


def set_statut(prospect_id: int, statut: str) -> bool:
    timestamp = now()
    with session() as conn:
        row = conn.execute("SELECT statut FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
        if row is None:
            return False
        if row["statut"] == statut:
            return True
        conn.execute(
            "UPDATE prospects SET statut = ?, updated_at = ? WHERE id = ?",
            (statut, timestamp, prospect_id),
        )
        _log_statut(conn, prospect_id, row["statut"], statut, timestamp)
        return True


def _log_statut(conn: sqlite3.Connection, prospect_id: int, avant: str, apres: str, timestamp: str) -> None:
    conn.execute(
        """
        INSERT INTO interactions (prospect_id, type, notes, statut_avant, statut_apres, occurred_at, created_at)
        VALUES (?, 'statut', '', ?, ?, ?, ?)
        """,
        (prospect_id, avant, apres, timestamp, timestamp),
    )


def add_interaction(
    prospect_id: int,
    *,
    kind: str,
    notes: str,
    occurred_at: str,
    statut: str = "",
    prochaine_action: str | None = None,
    prochaine_action_at: str | None = None,
) -> bool:
    """Enregistre un contact et met à jour la fiche dans la même transaction.

    Le journal est en ajout seul : une interaction n'est jamais modifiée.
    """
    timestamp = now()
    with session() as conn:
        row = conn.execute("SELECT statut FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
        if row is None:
            return False
        avant = row["statut"]
        apres = statut if statut and statut != avant else ""
        conn.execute(
            """
            INSERT INTO interactions (prospect_id, type, notes, statut_avant, statut_apres, occurred_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (prospect_id, kind, notes, avant if apres else "", apres, occurred_at, timestamp),
        )
        updates = {"updated_at": timestamp, "id": prospect_id}
        # Un rendez-vous noté à l'avance n'est pas encore un contact réalisé.
        if kind != "note" and occurred_at <= timestamp:
            updates["dernier_contact_at"] = occurred_at
        if apres:
            updates["statut"] = apres
        if prochaine_action_at is not None:
            updates["prochaine_action_at"] = prochaine_action_at
            updates["prochaine_action"] = prochaine_action or ""
        assignments = ", ".join(f"{key} = :{key}" for key in updates if key != "id")
        conn.execute(f"UPDATE prospects SET {assignments} WHERE id = :id", updates)
        return True


def list_interactions(prospect_id: int) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            """
            SELECT * FROM interactions
            WHERE prospect_id = ?
            ORDER BY occurred_at DESC, id DESC
            """,
            (prospect_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_prospect(prospect_id: int) -> bool:
    with session() as conn:
        conn.execute("DELETE FROM interactions WHERE prospect_id = ?", (prospect_id,))
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


# --- Page « Aujourd'hui » -------------------------------------------------

STATUTS_CHAUDS = ("interesse", "rdv", "demo", "essai")


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with session() as conn:
        return [enrich(row) for row in conn.execute(sql, params).fetchall()]


def relances(jusqu_au: str) -> list[dict]:
    """Prospects avec une relance prévue jusqu'à la date donnée, la plus ancienne d'abord."""
    return _rows(
        """
        SELECT * FROM prospects
        WHERE prochaine_action_at != '' AND prochaine_action_at <= ?
          AND statut NOT IN ('client', 'refuse')
        ORDER BY prochaine_action_at, id
        """,
        (jusqu_au,),
    )


def count_due() -> int:
    with session() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM prospects
                WHERE prochaine_action_at != '' AND prochaine_action_at <= ?
                  AND statut NOT IN ('client', 'refuse')
                """,
                (today(),),
            ).fetchone()[0]
        )


def chauds_sans_relance() -> list[dict]:
    """Prospects intéressés que rien ne fera rappeler : le risque d'oubli."""
    marks = ", ".join("?" for _ in STATUTS_CHAUDS)
    return _rows(
        f"""
        SELECT * FROM prospects
        WHERE statut IN ({marks}) AND prochaine_action_at = ''
          AND NOT EXISTS (
              SELECT 1 FROM interactions i
              WHERE i.prospect_id = prospects.id
                AND i.type IN ('rdv', 'demo') AND i.occurred_at > ?
          )
        ORDER BY updated_at
        """,
        (*STATUTS_CHAUDS, now()),
    )


def rendez_vous(depuis: str, jusqu_au: str) -> list[dict]:
    """Rendez-vous et démos notés dans le journal avec une date à venir."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT i.id AS interaction_id, i.type, i.notes, i.occurred_at, p.*
            FROM interactions i
            JOIN prospects p ON p.id = i.prospect_id
            WHERE i.type IN ('rdv', 'demo') AND i.occurred_at >= ? AND i.occurred_at < ?
            ORDER BY i.occurred_at
            """,
            (depuis, jusqu_au),
        ).fetchall()
    items = []
    for row in rows:
        item = enrich(row)
        item["rdv_type"] = row["type"]
        item["rdv_notes"] = row["notes"]
        item["rdv_at"] = row["occurred_at"]
        items.append(item)
    return items


def activite(jour: str) -> dict[str, int]:
    """Contacts réalisés ce jour-là, par type (hors notes et changements de statut)."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT type, COUNT(*) AS total FROM interactions
            WHERE substr(occurred_at, 1, 10) = ? AND occurred_at <= ?
              AND type NOT IN ('note', 'statut')
            GROUP BY type
            """,
            (jour, now()),
        ).fetchall()
    counts = {row["type"]: int(row["total"]) for row in rows}
    counts["total"] = sum(counts.values())
    return counts


def set_coords(prospect_id: int, lat: float, lon: float) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE prospects SET latitude = ?, longitude = ? WHERE id = ?",
            (f"{lat:.7f}", f"{lon:.7f}", prospect_id),
        )


def passages_du_jour(jour: str) -> set[int]:
    """Prospects déjà visités ce jour-là (passage noté dans le journal)."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT prospect_id FROM interactions
            WHERE type = 'passage' AND substr(occurred_at, 1, 10) = ?
            """,
            (jour,),
        ).fetchall()
    return {int(row["prospect_id"]) for row in rows}


# --- Statistiques -------------------------------------------------------


def donnees_stats() -> tuple[list[dict], list[dict]]:
    """Fiches et journal, réduits à ce que les statistiques utilisent."""
    with session() as conn:
        prospects = [dict(row) for row in conn.execute("SELECT id, statut, secteur, source FROM prospects")]
        interactions = [
            dict(row)
            for row in conn.execute(
                "SELECT id, prospect_id, type, notes, statut_avant, statut_apres, occurred_at FROM interactions"
            )
        ]
    return prospects, interactions


# --- Tournées imprimées ---------------------------------------------------


def create_tournee(jour: str, mode: str, depart_label: str, arrets: list[dict]) -> int:
    """Enregistre l'ordre imprimé : [{"id": 12, "heure": "09:45"}, …]."""
    with session() as conn:
        cursor = conn.execute(
            "INSERT INTO tournees (jour, mode, depart_label, arrets, created_at) VALUES (?, ?, ?, ?, ?)",
            (jour, mode, depart_label, json.dumps(arrets), now()),
        )
        return int(cursor.lastrowid)


def get_tournee(tournee_id: int) -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM tournees WHERE id = ?", (tournee_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["arrets"] = json.loads(item["arrets"])
    return item


def list_tournees(limit: int = 8) -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM tournees ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["arrets"] = json.loads(item["arrets"])
        items.append(item)
    return items


def tournees_a_saisir() -> list[dict]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM tournees WHERE saisie_at = '' AND jour <= ? ORDER BY jour, id",
            (today(),),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["arrets"] = json.loads(item["arrets"])
        items.append(item)
    return items


def mark_tournee_saisie(tournee_id: int) -> None:
    with session() as conn:
        conn.execute("UPDATE tournees SET saisie_at = ? WHERE id = ?", (now(), tournee_id))


def mark_ferme(prospect_id: int) -> None:
    """Constat de terrain : salon fermé ou introuvable."""
    with session() as conn:
        conn.execute(
            "UPDATE prospects SET actif = 0, prochaine_action = '', prochaine_action_at = '', updated_at = ? WHERE id = ?",
            (now(), prospect_id),
        )


def reporter_relance(prospect_id: int, nouvelle_date: str) -> bool:
    with session() as conn:
        cursor = conn.execute(
            """
            UPDATE prospects SET prochaine_action_at = ?, updated_at = ?
            WHERE id = ? AND prochaine_action_at != ''
            """,
            (nouvelle_date, now(), prospect_id),
        )
        return cursor.rowcount > 0
