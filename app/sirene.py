from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from app.geo import Place
from app.labels import EFFECTIFS, NAF_LABELS
from app.text import (
    fold,
    french_title,
    parse_street,
    prettify_enseigne,
    split_parentheses,
    street_number,
)

logger = logging.getLogger("companie.sirene")

SEARCH_URL = "https://recherche-entreprises.api.gouv.fr/search"
HEADERS = {
    "User-Agent": "COMPANIE-FINDER/1.0 (prospection locale salons)",
    "Accept": "application/json",
}
PAGE_SIZE = 25
# L'API sert au plus 10 000 résultats par requête, soit 400 pages de 25.
NAF_MAX_PAGES = 400
KEYWORD_MAX_PAGES = 4
# L'API autorise 7 appels par seconde : on garde une marge.
REQUESTS_PER_SECOND = 6.0


class RateLimiter:
    """Espace les appels pour ne jamais dépasser un débit donné, toutes requêtes confondues."""

    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
            self._next = now + self.interval


_limiter: RateLimiter | None = None


def limiter() -> RateLimiter:
    # Créé à la demande pour appartenir à la boucle asyncio en cours.
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(REQUESTS_PER_SECOND)
    return _limiter

COWORKING_QUERIES = (
    {"q": "coworking", "naf": "96.02A"},
    {"q": "coworking", "naf": "96.02B"},
    {"q": "coworking", "naf": "96.04Z"},
    {"q": "coworking beaute"},
    {"q": "coworking coiffure"},
    {"q": "salon partage"},
    {"q": "espace partage beaute"},
)

DIRIGEANT_KEYS = (
    "exploitant",
    "entrepreneur",
    "gerant",
    "president",
    "directeur",
    "associe",
)


class SireneError(Exception):
    pass


@dataclass
class Query:
    label: str
    params: dict
    max_pages: int
    coworking: bool = False
    independant_lane: bool = False


@dataclass
class Outcome:
    items: list[dict] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)
    truncated: bool = False


async def search_establishments(
    client: httpx.AsyncClient,
    place: Place,
    secteurs: list[str],
    *,
    actifs_only: bool,
) -> Outcome:
    queries = build_queries(place, secteurs, actifs_only=actifs_only)
    if not queries:
        raise SireneError("Choisissez au moins un secteur.")

    semaphore = asyncio.Semaphore(4)
    tasks = [run_query(semaphore, client, query, place) for query in queries]
    results = await asyncio.gather(*tasks)

    merged: dict[str, dict] = {}
    reports: dict[str, dict] = {}
    truncated = False
    for query, companies, reported, query_truncated, establishments in results:
        truncated = truncated or query_truncated
        slot = reports.setdefault(
            query.label,
            {"label": query.label, "reported": 0, "queries": 0, "truncated": False},
        )
        slot["reported"] += reported
        slot["queries"] += 1
        slot["truncated"] = slot["truncated"] or query_truncated
        for item in establishments:
            current = merged.get(item["siret"])
            if current is None:
                merged[item["siret"]] = item
                continue
            current["independant"] = int(current["independant"] or item["independant"])
            current["coworking"] = int(current["coworking"] or item["coworking"])
            if not current.get("enseigne") and item.get("enseigne"):
                current["enseigne"] = item["enseigne"]
            for key in ("adresse", "latitude", "longitude", "dirigeant", "effectif", "date_creation"):
                if not current.get(key) and item.get(key):
                    current[key] = item[key]

    items = [
        item
        for item in merged.values()
        if matches_text(item, place.text_filter)
    ]
    items.sort(
        key=lambda item: (
            fold(item.get("ville") or ""),
            fold(item.get("libelle_voie") or ""),
            street_number(item.get("numero_voie")),
            fold(item.get("enseigne") or item.get("nom") or ""),
        )
    )
    return Outcome(items=items, reports=list(reports.values()), truncated=truncated)


def build_queries(place: Place, secteurs: list[str], *, actifs_only: bool) -> list[Query]:
    geo = {"code_postal": ",".join(place.postal_codes)}
    if actifs_only:
        geo["etat_administratif"] = "A"
    queries: list[Query] = []
    naf_ids = [secteur for secteur in secteurs if secteur in NAF_LABELS]
    for naf in naf_ids:
        queries.append(
            Query(
                label=NAF_LABELS[naf],
                params={**geo, "activite_principale": naf},
                max_pages=NAF_MAX_PAGES,
            )
        )
    if "independant" in secteurs:
        codes = naf_ids or tuple(NAF_LABELS)
        for naf in codes:
            queries.append(
                Query(
                    label=f"Indépendants · {NAF_LABELS[naf]}",
                    params={**geo, "activite_principale": naf, "est_entrepreneur_individuel": "true"},
                    max_pages=NAF_MAX_PAGES,
                    independant_lane=True,
                )
            )
    if "coworking" in secteurs:
        for extra in COWORKING_QUERIES:
            params = {**geo, "q": extra["q"]}
            if extra.get("naf"):
                params["activite_principale"] = extra["naf"]
            queries.append(
                Query(
                    label="Coworking beauté",
                    params=params,
                    max_pages=KEYWORD_MAX_PAGES,
                    coworking=True,
                )
            )
    return queries


async def run_query(
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
    query: Query,
    place: Place,
) -> tuple[Query, list[dict], int, bool, list[dict]]:
    async with semaphore:
        companies, reported, truncated = await fetch_companies(client, query.params, query.max_pages)
    establishments: list[dict] = []
    for company in companies:
        establishments.extend(extract_establishments(company, query, place))
    return query, companies, reported, truncated, establishments


async def fetch_companies(
    client: httpx.AsyncClient,
    params: dict,
    max_pages: int,
) -> tuple[list[dict], int, bool]:
    companies: list[dict] = []
    reported = 0
    total_pages = 1
    pages_read = 0
    for page in range(1, max_pages + 1):
        payload = await get_json(client, {**params, "page": page, "per_page": PAGE_SIZE})
        batch = payload.get("results") or []
        reported = int(payload.get("total_results") or 0)
        total_pages = int(payload.get("total_pages") or 0)
        pages_read = page
        companies.extend(batch)
        if not batch or page >= total_pages:
            break
    return companies, reported, total_pages > pages_read


async def get_json(client: httpx.AsyncClient, params: dict) -> dict:
    cleaned = {key: value for key, value in params.items() if value not in (None, "")}
    last_status = None
    for attempt in range(4):
        await limiter().wait()
        try:
            response = await client.get(SEARCH_URL, params=cleaned)
        except httpx.HTTPError as exc:
            logger.warning("SIRENE injoignable (%s)", exc.__class__.__name__)
            await asyncio.sleep(0.4 * (attempt + 1))
            continue
        if response.status_code in {429, 502, 503, 504}:
            last_status = response.status_code
            await asyncio.sleep(0.45 * (attempt + 1))
            continue
        if response.status_code >= 400:
            raise SireneError(
                f"L'API Recherche d'entreprises a répondu {response.status_code}. Réessayez dans un instant."
            )
        try:
            return response.json()
        except ValueError as exc:
            raise SireneError("Réponse illisible de l'API Recherche d'entreprises.") from exc
    if last_status == 429:
        raise SireneError("L'API Recherche d'entreprises limite le rythme. Patientez quelques secondes, puis relancez.")
    raise SireneError("L'API Recherche d'entreprises ne répond pas. Réessayez dans un instant.")


def extract_establishments(company: dict, query: Query, place: Place) -> list[dict]:
    allowed = set(place.postal_codes)
    etabs = list(company.get("matching_etablissements") or [])
    if not etabs:
        siege = company.get("siege") or {}
        code = str(siege.get("code_postal") or "")
        if siege and (not code or code in allowed):
            etabs = [siege]
    items = []
    for etab in etabs:
        code = str(etab.get("code_postal") or "")
        if code and code not in allowed:
            continue
        if query.params.get("etat_administratif") == "A" and (etab.get("etat_administratif") or "A") != "A":
            continue
        siret = str(etab.get("siret") or "").strip()
        if len(siret) != 14 or not siret.isdigit():
            continue
        items.append(normalize(company, etab, query, place))
    return items


def normalize(company: dict, etab: dict, query: Query, place: Place) -> dict:
    legal, brand_from_name = split_parentheses(company.get("nom_complet") or "")
    raison = company.get("nom_raison_sociale") or legal or company.get("nom_complet") or ""
    nom = french_title(raison, particles=False) or "Établissement non diffusible"
    enseignes = etab.get("liste_enseignes") or []
    if isinstance(enseignes, str):
        enseignes = [enseignes]
    raw_enseigne = ""
    if enseignes:
        raw_enseigne = enseignes[0] or ""
    raw_enseigne = raw_enseigne or etab.get("nom_commercial") or brand_from_name or ""
    enseigne = prettify_enseigne(raw_enseigne)
    if enseigne and fold(enseigne) == fold(nom):
        enseigne = ""
    code_naf = (etab.get("activite_principale") or company.get("activite_principale") or "").strip()
    if code_naf in NAF_LABELS:
        secteur = NAF_LABELS[code_naf]
    elif query.coworking:
        secteur = "Coworking beauté"
    else:
        secteur = "Autre activité"
    complements = company.get("complements") or {}
    independant = 1 if complements.get("est_entrepreneur_individuel") or query.independant_lane else 0
    numero = " ".join(
        part
        for part in (str(etab.get("numero_voie") or "").strip(), str(etab.get("indice_repetition") or "").strip())
        if part
    )
    type_voie = french_title(etab.get("type_voie") or "", particles=True)
    libelle = french_title(etab.get("libelle_voie") or "", particles=True)
    adresse = format_adresse(etab)
    if not libelle:
        parsed_numero, parsed_type, parsed_name = parse_street(adresse)
        numero = numero or parsed_numero
        type_voie = type_voie or parsed_type
        libelle = parsed_name
    return {
        "siret": siret_of(etab),
        "siren": str(company.get("siren") or siret_of(etab)[:9]),
        "nom": nom,
        "enseigne": enseigne,
        "secteur": secteur,
        "code_naf": code_naf,
        "adresse": adresse,
        "numero_voie": numero,
        "type_voie": type_voie,
        "libelle_voie": libelle,
        "code_postal": str(etab.get("code_postal") or ""),
        "ville": french_title(etab.get("libelle_commune") or "", particles=False),
        "quartier": place.quartier,
        "latitude": str(etab.get("latitude") or ""),
        "longitude": str(etab.get("longitude") or ""),
        "date_creation": day(etab.get("date_creation")),
        "date_creation_unite": day(company.get("date_creation")),
        "actif": 1 if (etab.get("etat_administratif") or "A") == "A" else 0,
        "effectif": effectif_label(etab.get("tranche_effectif_salarie")),
        "dirigeant": pick_dirigeant(company.get("dirigeants") or []),
        "independant": independant,
        "coworking": 1 if query.coworking else 0,
    }


def siret_of(etab: dict) -> str:
    return str(etab.get("siret") or "").strip()


def format_adresse(etab: dict) -> str:
    official = french_title(etab.get("adresse") or "", particles=True)
    if official:
        return official
    numero = " ".join(
        part
        for part in (str(etab.get("numero_voie") or "").strip(), str(etab.get("indice_repetition") or "").strip())
        if part
    )
    street = " ".join(
        part
        for part in (
            french_title(etab.get("type_voie") or "", particles=True),
            french_title(etab.get("libelle_voie") or "", particles=True),
        )
        if part
    )
    line = " ".join(part for part in (numero, street) if part)
    city = " ".join(
        part
        for part in (
            str(etab.get("code_postal") or "").strip(),
            french_title(etab.get("libelle_commune") or "", particles=False),
        )
        if part
    )
    return ", ".join(part for part in (line, city) if part)


def effectif_label(code: str | None) -> str:
    if not code or code == "NN":
        return ""
    return EFFECTIFS.get(str(code), "")


def day(value: object) -> str:
    if not value:
        return ""
    return str(value)[:10]


def pick_dirigeant(people: list[dict]) -> str:
    ranked: list[tuple[int, int, dict]] = []
    for index, person in enumerate(people):
        quality = fold(person.get("qualite") or "")
        if "commissaire" in quality:
            continue
        score = 10
        for weight, key in enumerate(DIRIGEANT_KEYS):
            if key in quality:
                score = 100 - weight
                break
        if person.get("type_dirigeant") == "personne morale":
            score -= 20
        ranked.append((score, index, person))
    if not ranked:
        return ""
    ranked.sort(key=lambda item: (-item[0], item[1]))
    person = ranked[0][2]
    if person.get("type_dirigeant") == "personne morale":
        name = french_title(person.get("denomination") or "", particles=False)
    else:
        name = french_title(
            " ".join(part for part in (person.get("prenoms"), person.get("nom")) if part),
            particles=False,
        )
    quality = (person.get("qualite") or "").strip()
    if name and quality:
        return f"{name} · {quality}"
    return name


def matches_text(item: dict, text_filter: str) -> bool:
    tokens = [token for token in fold(text_filter).split() if len(token) > 1]
    if not tokens:
        return True
    blob = fold(
        " ".join(
            str(item.get(key) or "")
            for key in ("adresse", "libelle_voie", "type_voie", "ville", "nom", "enseigne")
        )
    )
    return all(token in blob for token in tokens)
