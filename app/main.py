from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote_plus, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.exporting import filename, render_csv
from app.geo import LocationError, resolve_place
from app.labels import (
    DEFAULT_SECTEURS,
    METIER_IDS,
    METIERS,
    NAF_LABELS,
    OUTILS,
    SECTEUR_IDS,
    SECTEURS,
    SITE_IDS,
    SITE_PRESENCE,
    SOURCE_IDS,
    SOURCES,
    STATUT_IDS,
    STATUT_LABELS,
    STATUTS,
)
from app.sirene import HEADERS, SireneError, search_establishments
from app.text import external_url, fold, fr_date

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

ROOT = db.ROOT
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["fr_date"] = fr_date
templates.env.filters["external_url"] = external_url
templates.env.globals.update(
    STATUTS=STATUTS,
    SOURCES=SOURCES,
    OUTILS=OUTILS,
    SITE_PRESENCE=SITE_PRESENCE,
    SECTEURS=SECTEURS,
    METIERS=METIERS,
    STATUT_LABELS=STATUT_LABELS,
    page_href=lambda filters, **updates: page_href(filters, **updates),
)

TOKEN = re.compile(r"^[a-f0-9]{32}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="COMPANIE FINDER", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def page_href(filters: dict, **updates: str) -> str:
    params = {key: value for key, value in filters.items() if value}
    for key, value in updates.items():
        if value:
            params[key] = value
        else:
            params.pop(key, None)
    if not params:
        return "/"
    return "/?" + urlencode(params)


def render(request: Request, name: str, *, status_code: int = 200, **ctx):
    context = {
        "request": request,
        "nav_total": db.count_all(),
        "active": "",
        "banner": "",
        "banner_kind": "ok",
        **ctx,
    }
    return templates.TemplateResponse(name, context, status_code=status_code)


def field(form, name: str) -> str:
    value = form.get(name)
    if value is None:
        return ""
    return str(value).strip()


def filters_from(params) -> dict:
    secteur = params.get("secteur") or ""
    statut = params.get("statut") or ""
    source = params.get("source") or ""
    return {
        "secteur": secteur if secteur in SECTEUR_IDS else "",
        "statut": statut if statut in STATUT_IDS else "",
        "source": source if source in SOURCE_IDS else "",
        "cp": (params.get("cp") or "").strip(),
        "ville": (params.get("ville") or "").strip(),
        "q": (params.get("q") or "").strip(),
    }


def default_search_form() -> dict:
    return {
        "code_postal": "",
        "ville": "",
        "quartier": "",
        "secteurs": list(DEFAULT_SECTEURS),
        "actifs": True,
        "filtre_rue": True,
    }


def read_search_form(form) -> dict:
    secteurs = [value for value in form.getlist("secteur") if value in SECTEUR_IDS]
    return {
        "code_postal": field(form, "code_postal"),
        "ville": field(form, "ville"),
        "quartier": field(form, "quartier"),
        "secteurs": secteurs,
        "actifs": form.get("actifs") == "1",
        "filtre_rue": form.get("filtre_rue") == "1",
    }


def plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if int(count) == 1 else many}"


def dashboard_banner(params) -> tuple[str, str]:
    if "importes" in params:
        try:
            added = int(params.get("importes") or 0)
            refreshed = int(params.get("maj") or 0)
        except ValueError:
            return "", "ok"
        parts = []
        if added:
            parts.append(plural(added, "nouveau prospect ajouté", "nouveaux prospects ajoutés"))
        if refreshed:
            parts.append(plural(refreshed, "fiche déjà suivie mise à jour", "fiches déjà suivies mises à jour"))
        if not parts:
            return "Aucun établissement importé.", "warn"
        return ". ".join(part[:1].upper() + part[1:] for part in [parts[0]]) + (
            (". " + ". ".join(parts[1:]) + ".") if len(parts) > 1 else "."
        ), "ok"
    if params.get("supprime"):
        return "Prospect retiré du tableau.", "ok"
    return "", "ok"


def clean_siret(value: str) -> tuple[str | None, str]:
    siret = re.sub(r"\s", "", value or "")
    if not siret:
        return None, ""
    if not re.fullmatch(r"\d{14}", siret):
        return None, "Le SIRET doit contenir 14 chiffres."
    return siret, ""


def names_from(brand: str, legal: str) -> tuple[str, str]:
    brand = " ".join(brand.split())
    legal = " ".join(legal.split())
    if legal and brand and fold(legal) != fold(brand):
        return legal, brand
    return brand or legal, ""


def metier_fields(metier: str, independant: bool, coworking: bool) -> dict:
    if metier in NAF_LABELS:
        return {
            "code_naf": metier,
            "secteur": NAF_LABELS[metier],
            "independant": int(independant),
            "coworking": int(coworking),
        }
    if coworking:
        return {
            "code_naf": "",
            "secteur": "Coworking beauté",
            "independant": int(independant),
            "coworking": 1,
        }
    return {
        "code_naf": "",
        "secteur": "Autre activité",
        "independant": int(independant),
        "coworking": 0,
    }


def qualification_fields(form) -> dict:
    statut = field(form, "statut")
    if statut not in STATUT_IDS:
        statut = "a_contacter"
    presence = field(form, "site_presence")
    if presence not in SITE_IDS:
        presence = "inconnu"
    outil = field(form, "outil_reservation")
    if outil not in {value for value, _label in OUTILS}:
        outil = ""
    site = field(form, "site_web")
    if site and presence == "inconnu":
        presence = "oui"
    return {
        "statut": statut,
        "site_presence": presence,
        "site_web": site,
        "outil_reservation": outil,
        "notes_terrain": field(form, "notes_terrain"),
        "notes_maps": field(form, "notes_maps"),
        "notes_reseaux": field(form, "notes_reseaux"),
    }


def posted_values(form) -> dict:
    return {
        "marque": field(form, "marque"),
        "nom": field(form, "nom"),
        "adresse": field(form, "adresse"),
        "numero_voie": field(form, "numero_voie"),
        "libelle_voie": field(form, "libelle_voie"),
        "code_postal": field(form, "code_postal"),
        "ville": field(form, "ville"),
        "quartier": field(form, "quartier"),
        "metier": field(form, "metier") or "autre",
        "independant": form.get("independant") == "1",
        "coworking": form.get("coworking") == "1",
        "source": field(form, "source") or "maps",
        "siret": field(form, "siret"),
        "dirigeant": field(form, "dirigeant"),
        "date_creation": field(form, "date_creation"),
        "actif": form.get("actif") == "1",
        "statut": field(form, "statut") or "a_contacter",
        "site_presence": field(form, "site_presence") or "inconnu",
        "site_web": field(form, "site_web"),
        "outil_reservation": field(form, "outil_reservation"),
        "notes_terrain": field(form, "notes_terrain"),
        "notes_maps": field(form, "notes_maps"),
        "notes_reseaux": field(form, "notes_reseaux"),
    }


def values_from_prospect(prospect: dict) -> dict:
    legal = ""
    marque = prospect.get("enseigne") or ""
    if not prospect.get("officiel"):
        if prospect.get("enseigne") and fold(prospect["enseigne"]) != fold(prospect.get("nom") or ""):
            legal = prospect.get("nom") or ""
            marque = prospect.get("enseigne") or ""
        else:
            marque = prospect.get("nom_affiche") or ""
    return {
        "marque": marque,
        "nom": legal,
        "adresse": prospect.get("adresse") or "",
        "numero_voie": prospect.get("numero_voie") or "",
        "libelle_voie": prospect.get("libelle_voie") or "",
        "code_postal": prospect.get("code_postal") or "",
        "ville": prospect.get("ville") or "",
        "quartier": prospect.get("quartier") or "",
        "metier": prospect.get("code_naf") if prospect.get("code_naf") in NAF_LABELS else "autre",
        "independant": bool(prospect.get("independant")),
        "coworking": bool(prospect.get("coworking")),
        "source": prospect.get("source") or "maps",
        "siret": prospect.get("siret") or "",
        "dirigeant": prospect.get("dirigeant") or "",
        "date_creation": (prospect.get("date_creation") or "")[:10],
        "actif": bool(prospect.get("actif")),
        "statut": prospect.get("statut") or "a_contacter",
        "site_presence": prospect.get("site_presence") or "inconnu",
        "site_web": prospect.get("site_web") or "",
        "outil_reservation": prospect.get("outil_reservation") or "",
        "notes_terrain": prospect.get("notes_terrain") or "",
        "notes_maps": prospect.get("notes_maps") or "",
        "notes_reseaux": prospect.get("notes_reseaux") or "",
    }


def parse_manual(form) -> tuple[dict | None, str]:
    nom, enseigne = names_from(field(form, "marque"), field(form, "nom"))
    if not nom:
        return None, "Indiquez le nom ou l'enseigne."
    adresse = field(form, "adresse")
    ville = field(form, "ville")
    code_postal = "".join(field(form, "code_postal").split())
    if code_postal and not re.fullmatch(r"\d{5}", code_postal):
        return None, "Le code postal doit contenir 5 chiffres."
    if not (adresse or ville or code_postal):
        return None, "Indiquez au moins une ville, un code postal ou une adresse."
    siret, siret_error = clean_siret(field(form, "siret"))
    if siret_error:
        return None, siret_error
    metier = field(form, "metier")
    if metier not in METIER_IDS:
        metier = "autre"
    source = field(form, "source")
    if source not in SOURCE_IDS:
        source = "maps"
    date_creation = field(form, "date_creation")
    if date_creation and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_creation):
        date_creation = ""
    values = {
        "nom": nom,
        "enseigne": enseigne,
        "adresse": adresse,
        "numero_voie": field(form, "numero_voie"),
        "type_voie": "",
        "libelle_voie": field(form, "libelle_voie"),
        "code_postal": code_postal,
        "ville": ville,
        "quartier": field(form, "quartier"),
        "siret": siret,
        "siren": siret[:9] if siret else "",
        "source": source,
        "actif": 1 if form.get("actif") == "1" else 0,
        "date_creation": date_creation,
        "dirigeant": field(form, "dirigeant"),
        **metier_fields(metier, form.get("independant") == "1", form.get("coworking") == "1"),
        **qualification_fields(form),
    }
    return values, ""


def maps_url(prospect: dict) -> str:
    if prospect.get("latitude") and prospect.get("longitude"):
        return (
            "https://www.google.com/maps/search/?api=1&query="
            f"{prospect['latitude']},{prospect['longitude']}"
        )
    if prospect.get("adresse"):
        return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(prospect["adresse"])
    return ""


def instagram_url(prospect: dict) -> str:
    query = " ".join(
        part for part in (prospect.get("nom_affiche"), prospect.get("ville"), "instagram") if part
    )
    return "https://www.google.com/search?q=" + quote_plus(query)


def annuaire_url(prospect: dict) -> str:
    siret = prospect.get("siret") or ""
    if len(siret) == 14 and siret.isdigit():
        return f"https://annuaire-entreprises.data.gouv.fr/etablissement/{siret}"
    return ""


def export_query(filters: dict, exclude: list[str] | None = None) -> str:
    pairs = [(key, value) for key, value in filters.items() if value]
    for statut in exclude or []:
        pairs.append(("exclure", statut))
    if not pairs:
        return "/export.csv"
    return "/export.csv?" + urlencode(pairs)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    accept = request.headers.get("accept", "")
    if exc.status_code == 404 and "text/html" in accept:
        return render(request, "404.html", status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/")
def dashboard(request: Request):
    filters = filters_from(request.query_params)
    base = db.query_prospects({**filters, "statut": ""}, apply_q=False)
    counts = db.tally(base)
    rows = base if not filters["statut"] else [row for row in base if row["statut"] == filters["statut"]]
    banner, kind = dashboard_banner(request.query_params)
    return render(
        request,
        "dashboard.html",
        active="prospects",
        filters=filters,
        rows=rows,
        counts=counts,
        banner=banner,
        banner_kind=kind,
        export_href=export_query(filters),
        has_filters=any(filters.values()),
    )


@app.get("/recherche")
def recherche_form(request: Request):
    return render(
        request,
        "recherche.html",
        active="recherche",
        form=default_search_form(),
        error="",
    )


@app.post("/recherche")
async def recherche_run(request: Request):
    submitted = read_search_form(await request.form())
    if not submitted["secteurs"]:
        return render(
            request,
            "recherche.html",
            active="recherche",
            form=submitted,
            error="Choisissez au moins un secteur.",
        )
    try:
        timeout = httpx.Timeout(25.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, headers=HEADERS) as client:
            place = await resolve_place(
                client,
                submitted["code_postal"],
                submitted["ville"],
                submitted["quartier"],
                filter_text=submitted["filtre_rue"],
            )
            outcome = await search_establishments(
                client,
                place,
                submitted["secteurs"],
                actifs_only=submitted["actifs"],
            )
    except (LocationError, SireneError) as exc:
        return render(
            request,
            "recherche.html",
            active="recherche",
            form=submitted,
            error=str(exc),
        )

    token = uuid.uuid4().hex
    db.save_search(
        token,
        {
            "items": outcome.items,
            "reports": outcome.reports,
            "truncated": outcome.truncated,
            "summary": place.summary,
            "wide": len(place.postal_codes) > 8,
        },
    )
    return RedirectResponse(f"/recherche/{token}", status_code=303)


@app.get("/recherche/{token}")
def recherche_resultats(request: Request, token: str, erreur: str = ""):
    if not TOKEN.fullmatch(token):
        raise StarletteHTTPException(status_code=404)
    payload = db.load_search(token)
    if payload is None:
        return render(
            request,
            "recherche.html",
            active="recherche",
            form=default_search_form(),
            error="Cette recherche a expiré. Relancez-la.",
        )
    known = db.siret_ids()
    items = []
    for item in payload["items"]:
        row = dict(item)
        row["prospect_id"] = known.get(row["siret"])
        items.append(row)
    fresh_count = sum(1 for item in items if not item["prospect_id"])
    banner = "Cochez au moins un établissement à ajouter." if erreur == "selection" else ""
    return render(
        request,
        "resultats.html",
        active="recherche",
        token=token,
        items=items,
        reports=payload["reports"],
        summary=payload["summary"],
        truncated=payload["truncated"],
        wide=payload.get("wide", False),
        fresh_count=fresh_count,
        banner=banner,
        banner_kind="warn",
    )


@app.post("/recherche/{token}/importer")
async def importer(request: Request, token: str):
    if not TOKEN.fullmatch(token):
        raise StarletteHTTPException(status_code=404)
    payload = db.load_search(token)
    if payload is None:
        return RedirectResponse("/recherche", status_code=303)
    form = await request.form()
    picked = set(form.getlist("siret"))
    items = [item for item in payload["items"] if item["siret"] in picked]
    if not items:
        return RedirectResponse(f"/recherche/{token}?erreur=selection", status_code=303)
    added, refreshed = db.upsert_imported(items)
    return RedirectResponse(f"/?importes={added}&maj={refreshed}", status_code=303)


@app.get("/prospects/nouveau")
def nouveau_form(request: Request):
    values = posted_values_defaults()
    return render(request, "nouveau.html", active="prospects", values=values, error="")


def posted_values_defaults() -> dict:
    values = posted_values({})
    values["actif"] = True
    values["metier"] = "96.02A"
    values["source"] = "maps"
    values["statut"] = "a_contacter"
    values["site_presence"] = "inconnu"
    return values


@app.post("/prospects")
async def nouveau_save(request: Request):
    form = await request.form()
    values, error = parse_manual(form)
    if error or values is None:
        return render(
            request,
            "nouveau.html",
            active="prospects",
            values=posted_values(form),
            error=error or "Le formulaire est incomplet.",
        )
    if values["siret"] and db.get_by_siret(values["siret"]):
        return render(
            request,
            "nouveau.html",
            active="prospects",
            values=posted_values(form),
            error="Ce SIRET est déjà dans le tableau.",
        )
    prospect_id = db.create_manual(values)
    return RedirectResponse(f"/prospects/{prospect_id}?enregistre=1", status_code=303)


@app.get("/prospects/{prospect_id}")
def prospect_detail(request: Request, prospect_id: int):
    prospect = db.get_prospect(prospect_id)
    if prospect is None:
        raise StarletteHTTPException(status_code=404)
    banner = "Qualification enregistrée." if request.query_params.get("enregistre") else ""
    return render(
        request,
        "detail.html",
        active="prospects",
        prospect=prospect,
        values=values_from_prospect(prospect),
        error="",
        banner=banner,
        maps=maps_url(prospect),
        instagram=instagram_url(prospect),
        annuaire=annuaire_url(prospect),
    )


@app.post("/prospects/{prospect_id}")
async def prospect_save(request: Request, prospect_id: int):
    prospect = db.get_prospect(prospect_id)
    if prospect is None:
        raise StarletteHTTPException(status_code=404)
    form = await request.form()
    if prospect["officiel"]:
        payload = qualification_fields(form)
        payload["enseigne"] = field(form, "marque")
        payload["quartier"] = field(form, "quartier")
        db.update_prospect(prospect_id, payload, official=True)
        return RedirectResponse(f"/prospects/{prospect_id}?enregistre=1", status_code=303)

    values, error = parse_manual(form)
    if error or values is None:
        return render(
            request,
            "detail.html",
            active="prospects",
            prospect=prospect,
            values=posted_values(form),
            error=error or "Le formulaire est incomplet.",
            maps=maps_url(prospect),
            instagram=instagram_url(prospect),
            annuaire=annuaire_url(prospect),
        )
    if values["siret"]:
        other = db.get_by_siret(values["siret"])
        if other and other["id"] != prospect_id:
            return render(
                request,
                "detail.html",
                active="prospects",
                prospect=prospect,
                values=posted_values(form),
                error="Ce SIRET est déjà dans le tableau.",
                maps=maps_url(prospect),
                instagram=instagram_url(prospect),
                annuaire=annuaire_url(prospect),
            )
    db.update_prospect(prospect_id, values, official=False)
    return RedirectResponse(f"/prospects/{prospect_id}?enregistre=1", status_code=303)


@app.post("/prospects/{prospect_id}/statut")
async def prospect_statut(request: Request, prospect_id: int):
    form = await request.form()
    statut = field(form, "statut")
    if statut not in STATUT_IDS or not db.set_statut(prospect_id, statut):
        return JSONResponse({"ok": False}, status_code=400)
    return JSONResponse({"ok": True})


@app.post("/prospects/{prospect_id}/supprimer")
def prospect_delete(prospect_id: int):
    db.delete_prospect(prospect_id)
    return RedirectResponse("/?supprime=1", status_code=303)


@app.get("/tournee")
def tournee(request: Request):
    filters = filters_from(request.query_params)
    submitted = "filtre" in request.query_params
    masquer_refuses = True if not submitted else request.query_params.get("masquer_refuses") == "1"
    masquer_clients = False if not submitted else request.query_params.get("masquer_clients") == "1"
    excluded = []
    if masquer_refuses:
        excluded.append("refuse")
    if masquer_clients:
        excluded.append("client")
    items = db.query_prospects(filters, apply_q=True, exclude_statuts=tuple(excluded))
    groups = db.group_by_street(items)
    zone_bits = [bit for bit in (filters["cp"], filters["ville"], filters["q"]) if bit]
    return render(
        request,
        "tournee.html",
        active="tournee",
        filters=filters,
        groups=groups,
        stops=items,
        masquer_refuses=masquer_refuses,
        masquer_clients=masquer_clients,
        zone=" · ".join(zone_bits),
        export_href=export_query(filters, excluded),
    )


@app.get("/export.csv")
def export_csv(request: Request):
    filters = filters_from(request.query_params)
    excluded = [value for value in request.query_params.getlist("exclure") if value in STATUT_IDS]
    rows = db.query_prospects(filters, apply_q=True, exclude_statuts=tuple(excluded))
    return Response(
        content=render_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename()}"'},
    )
