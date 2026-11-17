from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.text import fold_key

GEO_URL = "https://geo.api.gouv.fr/communes"
BAN_URL = "https://api-adresse.data.gouv.fr/search/"


class LocationError(Exception):
    pass


@dataclass
class Place:
    postal_codes: list[str]
    text_filter: str
    summary: str
    quartier: str


async def resolve_place(
    client: httpx.AsyncClient,
    code_postal: str,
    ville: str,
    quartier: str,
    *,
    filter_text: bool,
) -> Place:
    code_postal = "".join(code_postal.split())
    ville = ville.strip()
    quartier = quartier.strip()
    if code_postal and not (code_postal.isdigit() and len(code_postal) == 5):
        raise LocationError("Le code postal doit contenir 5 chiffres.")

    commune = None
    if ville:
        commune = await _resolve_commune_name(client, ville, code_postal)

    if not code_postal and not commune and quartier:
        commune = await _commune_or_none(client, quartier)
        if commune is not None:
            quartier = ""
            filter_text = False

    postal_codes: list[str] = []
    if code_postal and commune:
        known = commune.get("codesPostaux") or []
        if code_postal not in known:
            readable = ", ".join(known) or "aucun code publié"
            raise LocationError(
                f"Le code postal {code_postal} ne fait pas partie de {commune['nom']} ({readable})."
            )
        postal_codes = [code_postal]
    elif code_postal:
        postal_codes = [code_postal]
    elif commune:
        postal_codes = list(commune.get("codesPostaux") or [])

    ban_label = ""
    if not postal_codes and quartier:
        ban = await _ban_lookup(client, quartier)
        if ban is None:
            raise LocationError(
                f"Aucun lieu ne correspond à « {quartier} ». Indiquez un code postal ou une ville."
            )
        if ban.get("type") == "municipality" and ban.get("citycode"):
            commune = await _commune_by_code(client, ban["citycode"])
            postal_codes = list((commune or {}).get("codesPostaux") or [])
            if ban.get("postcode") and not postal_codes:
                postal_codes = [ban["postcode"]]
            quartier = ""
            filter_text = False
        else:
            if ban.get("postcode"):
                postal_codes = [ban["postcode"]]
            ban_label = ban.get("label") or ""

    if not postal_codes:
        raise LocationError("Indiquez un code postal, une ville ou un quartier.")

    text_filter = quartier if filter_text and quartier else ""
    summary = _summary(commune, postal_codes, code_postal, text_filter, ban_label)
    return Place(
        postal_codes=postal_codes,
        text_filter=text_filter,
        summary=summary,
        quartier=text_filter,
    )


def _summary(
    commune: dict | None,
    postal_codes: list[str],
    code_postal: str,
    text_filter: str,
    ban_label: str,
) -> str:
    if commune and code_postal:
        place = f"{commune['nom']} · {code_postal}"
    elif commune:
        extra = ""
        if len(postal_codes) > 1:
            extra = f" · {len(postal_codes)} codes postaux"
        place = f"{commune['nom']}{extra}"
    elif len(postal_codes) == 1:
        place = f"Code postal {postal_codes[0]}"
    else:
        place = f"{len(postal_codes)} codes postaux"
    if ban_label and not commune:
        place = f"{place} · repère « {ban_label} »"
    if text_filter:
        place = f"{place} · adresses contenant « {text_filter} »"
    return place


async def _resolve_commune_name(
    client: httpx.AsyncClient,
    name: str,
    code_postal: str,
) -> dict:
    matches = await _search_communes(client, name)
    exact = [item for item in matches if fold_key(item.get("nom")) == fold_key(name)]
    if code_postal:
        exact = [item for item in exact if code_postal in (item.get("codesPostaux") or [])] or exact
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise LocationError(_ambiguous(exact))
    if not matches:
        raise LocationError(f"Aucune commune ne correspond à « {name} ». Précisez le code postal.")
    best = matches[0]
    second = matches[1].get("_score", 0) if len(matches) > 1 else 0
    score = best.get("_score") or 0
    if score >= 0.85 and score - second > 0.08:
        return best
    raise LocationError(_ambiguous(matches[:5]))


async def _commune_or_none(client: httpx.AsyncClient, name: str) -> dict | None:
    matches = await _search_communes(client, name)
    exact = [item for item in matches if fold_key(item.get("nom")) == fold_key(name)]
    if len(exact) == 1:
        return exact[0]
    return None


async def _search_communes(client: httpx.AsyncClient, name: str) -> list[dict]:
    try:
        response = await client.get(
            GEO_URL,
            params={
                "nom": name,
                "fields": "nom,code,codesPostaux",
                "boost": "population",
                "limit": 8,
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise LocationError("Le service géographique est indisponible. Indiquez un code postal.") from exc
    if not isinstance(payload, list):
        return []
    return payload


async def _commune_by_code(client: httpx.AsyncClient, code: str) -> dict | None:
    try:
        response = await client.get(
            f"{GEO_URL}/{code}",
            params={"fields": "nom,code,codesPostaux"},
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise LocationError("Le service géographique est indisponible. Indiquez un code postal.") from exc
    if isinstance(payload, dict) and payload.get("code"):
        return payload
    return None


async def _ban_lookup(client: httpx.AsyncClient, query: str) -> dict | None:
    try:
        response = await client.get(BAN_URL, params={"q": query, "limit": 5})
        response.raise_for_status()
        features = response.json().get("features") or []
    except httpx.HTTPError as exc:
        raise LocationError("La Base Adresse Nationale est indisponible. Indiquez un code postal.") from exc
    if not features:
        return None
    return features[0].get("properties") or None


async def geocode(client: httpx.AsyncClient, query: str) -> tuple[float, float] | None:
    """Coordonnées d'une adresse via la Base Adresse Nationale, ou None."""
    query = " ".join((query or "").split())
    if len(query) < 3:
        return None
    try:
        response = await client.get(BAN_URL, params={"q": query, "limit": 1})
        response.raise_for_status()
        features = response.json().get("features") or []
    except (httpx.HTTPError, ValueError):
        return None
    if not features:
        return None
    feature = features[0]
    if float((feature.get("properties") or {}).get("score") or 0) < 0.4:
        return None
    lon, lat = (feature.get("geometry") or {}).get("coordinates") or (None, None)
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def _ambiguous(communes: list[dict]) -> str:
    labels = []
    for item in communes:
        codes = ", ".join(item.get("codesPostaux") or [])
        labels.append(f"{item.get('nom')} ({codes})" if codes else item.get("nom"))
    return "Plusieurs communes correspondent. Précisez le code postal : " + " · ".join(labels)
