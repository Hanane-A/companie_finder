from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db, ors, routing, scoring
from app.exporting import filename, render_csv
from app.geo import LocationError, geocode, resolve_place
from app.labels import (
    DEFAULT_SECTEURS,
    INTERACTION_IDS,
    INTERACTION_LABELS,
    INTERACTIONS,
    METIER_IDS,
    METIERS,
    OUTIL_LABELS,
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
from app.text import (
    clean_email,
    clean_instagram,
    clean_phone,
    external_url,
    fold,
    fr_date,
    fr_datetime,
    phone_international,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

ROOT = db.ROOT
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["fr_date"] = fr_date
templates.env.filters["external_url"] = external_url
templates.env.filters["fr_datetime"] = fr_datetime


def duree(minutes: float) -> str:
    total = int(round(minutes or 0))
    if total < 60:
        return f"{total} min"
    return f"{total // 60} h {total % 60:02d}"


def km(metres: float) -> str:
    metres = metres or 0
    if metres < 1000:
        return f"{int(round(metres / 10) * 10)} m"
    return f"{metres / 1000:.1f} km".replace(".", ",")


templates.env.filters["duree"] = duree
templates.env.filters["km"] = km
templates.env.globals.update(
    STATUTS=STATUTS,
    SOURCES=SOURCES,
    OUTILS=OUTILS,
    SITE_PRESENCE=SITE_PRESENCE,
    SECTEURS=SECTEURS,
    METIERS=METIERS,
    STATUT_LABELS=STATUT_LABELS,
    OUTIL_LABELS=OUTIL_LABELS,
    INTERACTIONS=INTERACTIONS,
    INTERACTION_LABELS=INTERACTION_LABELS,
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
        "nav_due": db.count_due(),
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
        "tri": "score" if params.get("tri") == "score" else "",
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


def contact_fields(form) -> tuple[dict, str]:
    telephone, error = clean_phone(field(form, "telephone"))
    if error:
        return {}, error
    email, error = clean_email(field(form, "email"))
    if error:
        return {}, error
    echeance = field(form, "prochaine_action_at")
    if echeance and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", echeance):
        return {}, "La date de prochaine action n'est pas valide."
    return {
        "contact_nom": field(form, "contact_nom"),
        "contact_role": field(form, "contact_role"),
        "telephone": telephone,
        "email": email,
        "instagram": clean_instagram(field(form, "instagram")),
        "prochaine_action": field(form, "prochaine_action") if echeance else "",
        "prochaine_action_at": echeance,
    }, ""


CONTACT_KEYS = (
    "contact_nom",
    "contact_role",
    "telephone",
    "email",
    "instagram",
    "prochaine_action",
    "prochaine_action_at",
)


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
        **{key: field(form, key) for key in CONTACT_KEYS},
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
        **{key: prospect.get(key) or "" for key in CONTACT_KEYS},
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
    contact, contact_error = contact_fields(form)
    if contact_error:
        return None, contact_error
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
        **contact,
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


def contact_links(prospect: dict) -> dict:
    phone = prospect.get("telephone") or ""
    intl = phone_international(phone)
    handle = prospect.get("instagram") or ""
    return {
        "tel": f"tel:{phone.replace(' ', '')}" if phone else "",
        "sms": f"sms:{phone.replace(' ', '')}" if phone else "",
        "whatsapp": f"https://wa.me/{intl}" if intl else "",
        "mail": f"mailto:{prospect['email']}" if prospect.get("email") else "",
        "instagram": f"https://www.instagram.com/{handle}/" if handle else "",
    }


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
    if filters["tri"] == "score":
        rows = sorted(rows, key=lambda row: (-row["score"], fold(row["nom_affiche"])))
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


PIPELINE_MAX_CARTES = 60


@app.get("/pipeline")
def pipeline(request: Request):
    filters = filters_from(request.query_params)
    rows = db.query_prospects({**filters, "statut": ""}, apply_q=True)
    counts = db.tally(rows)
    colonnes = []
    for key, label in STATUTS:
        cartes = sorted((row for row in rows if row["statut"] == key), key=priorite)
        colonnes.append(
            {
                "key": key,
                "label": label,
                "total": len(cartes),
                "cartes": cartes[:PIPELINE_MAX_CARTES],
            }
        )
    return render(
        request,
        "pipeline.html",
        active="pipeline",
        filters=filters,
        colonnes=colonnes,
        counts=counts,
        has_filters=any(filters.values()),
    )


@app.get("/aujourdhui")
def aujourdhui(request: Request):
    jour = date.today()
    today_iso = jour.isoformat()
    semaine = (jour + timedelta(days=7)).isoformat()
    toutes = db.relances(semaine)
    en_retard = [item for item in toutes if item["prochaine_action_at"] < today_iso]
    du_jour = [item for item in toutes if item["prochaine_action_at"] == today_iso]
    a_venir = [item for item in toutes if item["prochaine_action_at"] > today_iso]
    rdv = db.rendez_vous(datetime.now().strftime("%Y-%m-%dT00:00"), f"{semaine}T23:59:59")
    for item in en_retard:
        item["retard_jours"] = (jour - date.fromisoformat(item["prochaine_action_at"])).days
    for item in rdv + en_retard + du_jour + a_venir:
        item["links"] = contact_links(item)
        item["maps"] = maps_url(item)
    reporte = request.query_params.get("reporte")
    saisie = request.query_params.get("saisie")
    return render(
        request,
        "aujourdhui.html",
        active="aujourdhui",
        jour=jour,
        jour_label=jour_fr(jour),
        en_retard=en_retard,
        du_jour=du_jour,
        a_venir=a_venir,
        rdv=rdv,
        oublis=db.chauds_sans_relance(),
        activite=db.activite(today_iso),
        banner="Relance reportée." if reporte else (f"Tournée enregistrée : {plural(int(saisie), 'passage', 'passages')}." if saisie and saisie.isdigit() else ""),
        a_saisir=db.tournees_a_saisir(),
    )


JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


def jour_fr(value: date) -> str:
    return f"{JOURS[value.weekday()]} {value.day} {MOIS[value.month - 1]}"


@app.post("/prospects/{prospect_id}/reporter")
async def reporter(request: Request, prospect_id: int):
    form = await request.form()
    try:
        jours = int(field(form, "jours"))
    except ValueError:
        jours = 0
    if jours not in (1, 3, 7):
        return RedirectResponse("/aujourdhui", status_code=303)
    nouvelle = (date.today() + timedelta(days=jours)).isoformat()
    db.reporter_relance(prospect_id, nouvelle)
    return RedirectResponse("/aujourdhui?reporte=1", status_code=303)


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
        resultat = scoring.compute({**row, "statut": "a_contacter"})
        row["score"] = resultat["score"]
        row["niveau"] = resultat["niveau"]
        row["raisons"] = resultat["raisons"]
        items.append(row)
    if request.query_params.get("tri") == "score":
        items.sort(key=lambda row: -row["score"])
    fresh_count = sum(1 for item in items if not item["prospect_id"])
    tri_score = request.query_params.get("tri") == "score"
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
        tri_score=tri_score,
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
    params = request.query_params
    banner = ""
    if params.get("enregistre"):
        banner = "Fiche enregistrée."
    elif params.get("journal"):
        banner = "Contact ajouté au journal."
    return render_detail(
        request,
        prospect,
        values=values_from_prospect(prospect),
        banner=banner,
        journal_error=JOURNAL_ERRORS.get(params.get("erreur_journal") or "", ""),
    )


JOURNAL_ERRORS = {
    "type": "Choisissez le type de contact.",
    "date": "La date de prochaine action n'est pas valide.",
    "quand": "La date du contact n'est pas valide.",
}


def render_detail(request: Request, prospect: dict, *, values: dict, error: str = "", banner: str = "", journal_error: str = ""):
    return render(
        request,
        "detail.html",
        active="prospects",
        prospect=prospect,
        values=values,
        error=error,
        banner=banner,
        journal_error=journal_error,
        journal=db.list_interactions(prospect["id"]),
        links=contact_links(prospect),
        now_local=datetime.now().strftime("%Y-%m-%dT%H:%M"),
        today=date.today().isoformat(),
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
        contact, contact_error = contact_fields(form)
        if contact_error:
            return render_detail(request, prospect, values=posted_values(form), error=contact_error)
        payload = qualification_fields(form)
        payload.update(contact)
        payload["enseigne"] = field(form, "marque")
        payload["quartier"] = field(form, "quartier")
        db.update_prospect(prospect_id, payload, official=True)
        return RedirectResponse(f"/prospects/{prospect_id}?enregistre=1", status_code=303)

    values, error = parse_manual(form)
    if error or values is None:
        return render_detail(request, prospect, values=posted_values(form), error=error or "Le formulaire est incomplet.")
    if values["siret"]:
        other = db.get_by_siret(values["siret"])
        if other and other["id"] != prospect_id:
            return render_detail(request, prospect, values=posted_values(form), error="Ce SIRET est déjà dans le tableau.")
    db.update_prospect(prospect_id, values, official=False)
    return RedirectResponse(f"/prospects/{prospect_id}?enregistre=1", status_code=303)


@app.post("/prospects/{prospect_id}/statut")
async def prospect_statut(request: Request, prospect_id: int):
    form = await request.form()
    statut = field(form, "statut")
    if statut not in STATUT_IDS or not db.set_statut(prospect_id, statut):
        return JSONResponse({"ok": False}, status_code=400)
    return JSONResponse({"ok": True})


@app.post("/prospects/{prospect_id}/journal")
async def prospect_journal(request: Request, prospect_id: int):
    if db.get_prospect(prospect_id) is None:
        raise StarletteHTTPException(status_code=404)
    form = await request.form()
    back = f"/prospects/{prospect_id}"
    kind = field(form, "type")
    if kind not in INTERACTION_IDS:
        return RedirectResponse(f"{back}?erreur_journal=type#journal", status_code=303)
    quand = field(form, "occurred_at")
    try:
        occurred_at = datetime.fromisoformat(quand).isoformat(timespec="seconds") if quand else db.now()
    except ValueError:
        return RedirectResponse(f"{back}?erreur_journal=quand#journal", status_code=303)
    statut = field(form, "statut")
    if statut not in STATUT_IDS:
        statut = ""
    echeance = field(form, "prochaine_action_at")
    if echeance and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", echeance):
        return RedirectResponse(f"{back}?erreur_journal=date#journal", status_code=303)
    # Sans date, on garde la relance existante, sauf si on demande de l'effacer.
    if echeance:
        next_at, next_label = echeance, field(form, "prochaine_action")
    elif form.get("effacer_relance") == "1" or statut in ("client", "refuse"):
        next_at, next_label = "", ""
    else:
        next_at, next_label = None, None
    db.add_interaction(
        prospect_id,
        kind=kind,
        notes=field(form, "notes"),
        occurred_at=occurred_at,
        statut=statut,
        prochaine_action=next_label,
        prochaine_action_at=next_at,
    )
    return RedirectResponse(f"{back}?journal=1#journal", status_code=303)


@app.post("/prospects/{prospect_id}/supprimer")
def prospect_delete(prospect_id: int):
    db.delete_prospect(prospect_id)
    return RedirectResponse("/?supprime=1", status_code=303)


TOURNEE_KEYS = (
    "filtre", "cp", "ville", "q", "secteur", "niveau", "mode", "max_arrets", "visite",
    "heure_depart", "heure_fin", "depart_adresse", "depart_lat", "depart_lon",
    "masquer_refuses", "masquer_clients", "masquer_faits", "retour",
)
HEURE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def tournee_options(params) -> dict:
    submitted = "filtre" in params

    def flag(name: str, default: bool) -> bool:
        return default if not submitted else params.get(name) == "1"

    def number(name: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(params.get(name) or default)))
        except ValueError:
            return default

    def coord(name: str, low: float, high: float) -> float | None:
        try:
            value = float(params.get(name) or "")
        except ValueError:
            return None
        return value if low <= value <= high else None

    maintenant = datetime.now()
    proche = (maintenant + timedelta(minutes=5 - maintenant.minute % 5)).strftime("%H:%M")
    heure_depart = params.get("heure_depart") or ""
    heure_fin = params.get("heure_fin") or ""
    mode = params.get("mode") or "pied"
    niveau = params.get("niveau") or ""
    return {
        "mode": mode if mode in routing.MODE_IDS else "pied",
        "niveau": niveau if niveau in ("tiede", "chaud") else "",
        "max_arrets": number("max_arrets", 20, 1, routing.MAX_ARRETS),
        "visite": number("visite", 10, 0, 120),
        "heure_depart": heure_depart if HEURE.match(heure_depart) else proche,
        "heure_fin": heure_fin if HEURE.match(heure_fin) else "19:00",
        "depart_adresse": (params.get("depart_adresse") or "").strip()[:200],
        "depart_lat": coord("depart_lat", -90, 90),
        "depart_lon": coord("depart_lon", -180, 180),
        "masquer_refuses": flag("masquer_refuses", True),
        "masquer_clients": flag("masquer_clients", True),
        "masquer_faits": flag("masquer_faits", True),
        "retour": flag("retour", False),
    }


def tournee_query(filters: dict, options: dict, **updates) -> str:
    """Rebâtit l'adresse de la tournée à partir de valeurs déjà validées."""
    values = {
        "filtre": "1",
        "cp": filters["cp"],
        "ville": filters["ville"],
        "q": filters["q"],
        "secteur": filters["secteur"],
        "niveau": options["niveau"],
        "mode": options["mode"],
        "max_arrets": str(options["max_arrets"]),
        "visite": str(options["visite"]),
        "heure_depart": options["heure_depart"],
        "heure_fin": options["heure_fin"],
        "depart_adresse": options["depart_adresse"],
        "depart_lat": "" if options["depart_lat"] is None else f"{options['depart_lat']:.6f}",
        "depart_lon": "" if options["depart_lon"] is None else f"{options['depart_lon']:.6f}",
    }
    for name in ("masquer_refuses", "masquer_clients", "masquer_faits", "retour"):
        values[name] = "1" if options[name] else ""
    values.update(updates)
    return "/tournee?" + urlencode({key: value for key, value in values.items() if value})


def coords_of(item: dict) -> tuple[float, float] | None:
    try:
        lat = float(item.get("latitude") or "")
        lon = float(item.get("longitude") or "")
    except ValueError:
        return None
    if lat == 0 and lon == 0:
        return None
    return lat, lon


def priorite(item: dict) -> tuple:
    # Relance due d'abord, puis les plus prometteurs.
    return (0 if item.get("relance_due") else 1, -item.get("score", 0), fold(item["nom_affiche"]), item["id"])


@app.get("/tournee")
async def tournee(request: Request):
    filters = filters_from(request.query_params)
    options = tournee_options(request.query_params)
    excluded = []
    if options["masquer_refuses"]:
        excluded.append("refuse")
    if options["masquer_clients"]:
        excluded.append("client")
    candidates = db.query_prospects(filters, apply_q=True, exclude_statuts=tuple(excluded))
    faits = db.passages_du_jour(date.today().isoformat())
    nb_faits = sum(1 for item in candidates if item["id"] in faits)
    if options["masquer_faits"]:
        candidates = [item for item in candidates if item["id"] not in faits]
    if options["niveau"] == "chaud":
        candidates = [item for item in candidates if item["niveau"] == "chaud"]
    elif options["niveau"] == "tiede":
        candidates = [item for item in candidates if item["niveau"] in ("chaud", "tiede")]
    candidates.sort(key=priorite)
    selection = candidates[: options["max_arrets"]]
    hors_selection = len(candidates) - len(selection)

    error = ""
    start: tuple[float, float] | None = None
    if options["depart_lat"] is not None and options["depart_lon"] is not None:
        start = (options["depart_lat"], options["depart_lon"])
    timeout = httpx.Timeout(15.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, headers=HEADERS) as client:
        # Les fiches ajoutées à la main n'ont pas de coordonnées : on les géocode une fois.
        for item in selection:
            if coords_of(item) is None:
                query = " ".join(part for part in (item.get("adresse"), item.get("code_postal"), item.get("ville")) if part)
                found = await geocode(client, query)
                if found:
                    db.set_coords(item["id"], *found)
                    item["latitude"], item["longitude"] = str(found[0]), str(found[1])
        if start is None and options["depart_adresse"]:
            start = await geocode(client, options["depart_adresse"])
            if start is None:
                error = "Adresse de départ introuvable. Le départ se fait au premier arrêt."
        placed = [item for item in selection if coords_of(item)]
        unplaced = [item for item in selection if not coords_of(item)]
        depart_label = "Votre position" if options["depart_lat"] is not None else (options["depart_adresse"] if start else "")
        if start is None and placed:
            start = point_de_depart([coords_of(item) for item in placed])
            depart_label = "Premier arrêt"
        stops = [routing.Stop(item["id"], *coords_of(item)) for item in placed]
        matrix = None
        source = "estimation"
        if stops and options["mode"] in routing.ORS_PROFILES:
            points = [start] + [(stop.lat, stop.lon) for stop in stops]
            matrix = await ors.fetch_matrix(client, points, options["mode"])
            if matrix is not None:
                source = "openrouteservice"

    today = date.today()
    depart_dt = datetime.combine(today, datetime.strptime(options["heure_depart"], "%H:%M").time())
    fin_dt = datetime.combine(today, datetime.strptime(options["heure_fin"], "%H:%M").time())
    plan = routing.optimise(
        start or (0.0, 0.0),
        stops,
        mode=options["mode"],
        depart=depart_dt,
        visite_min=options["visite"],
        fin_souhaitee=fin_dt,
        retour=options["retour"],
        matrix=matrix,
        source=source,
    )
    by_id = {item["id"]: item for item in placed}
    etapes = []
    previous = start
    for visit in plan.visits:
        item = by_id[visit.stop_id]
        point = coords_of(item)
        item["visit"] = visit
        item["links"] = contact_links(item)
        item["itineraire"] = routing.google_maps_url([previous, point], options["mode"]) if previous else ""
        etapes.append(item)
        previous = point
    chemin = ([start] if start else []) + [coords_of(item) for item in etapes]
    if options["retour"] and start and etapes:
        chemin.append(start)
    current = tournee_query(filters, options)
    return render(
        request,
        "tournee.html",
        active="tournee",
        filters=filters,
        options=options,
        modes=routing.MODES,
        plan=plan,
        etapes=etapes,
        unplaced=unplaced,
        start=start,
        depart_label=depart_label,
        hors_selection=hors_selection,
        nb_faits=nb_faits,
        error=error,
        gmaps=routing.google_maps_chunks(chemin, options["mode"]),
        map_data={
            "start": list(start) if start else None,
            "stops": [
                {"n": item["visit"].order, "lat": coords_of(item)[0], "lon": coords_of(item)[1], "nom": item["nom_affiche"], "niveau": item["niveau"]}
                for item in etapes
            ],
            "retour": options["retour"],
        },
        retour_url=current,
        export_href=export_query(filters, excluded),
        tournees=db.list_tournees(),
    )


def point_de_depart(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Sans départ choisi : on part de l'arrêt le plus excentré, pour balayer la zone d'un bout à l'autre."""
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
    return max(points, key=lambda p: (routing.haversine_m(p, (lat, lon)), p))


RESULTATS_PASSAGE = {
    "absent": {"statut_si_nouveau": "visite", "jours": 2, "action": "Repasser : décideur absent", "note": "Passage : décideur absent"},
    "interesse": {"statut": "interesse", "jours": 1, "action": "Rappeler pour fixer une démo", "note": "Passage : intéressé"},
    "rdv": {"statut": "rdv", "jours": 1, "action": "Confirmer le rendez-vous", "note": "Passage : rendez-vous pris"},
    "plus_tard": {"statut": "plus_tard", "jours": 30, "action": "Recontacter", "note": "Passage : pas maintenant"},
    "refus": {"statut": "refuse", "jours": None, "action": "", "note": "Passage : pas intéressé"},
    "ferme": {"statut": "", "jours": None, "action": "", "note": "Passage : salon fermé ou introuvable"},
}


def appliquer_passage(prospect: dict, resultat_key: str, *, occurred_at: str, notes: str = "", base: date | None = None) -> None:
    """Enregistre un passage et met la fiche à jour. Utilisé en tournée et à la saisie du soir."""
    resultat = RESULTATS_PASSAGE[resultat_key]
    statut = resultat.get("statut") or ""
    if resultat_key == "absent" and prospect["statut"] in ("a_contacter", "contacte"):
        statut = resultat["statut_si_nouveau"]
    jours = resultat["jours"]
    base = base or date.today()
    db.add_interaction(
        prospect["id"],
        kind="passage",
        notes=resultat["note"] + (f". {notes}" if notes else ""),
        occurred_at=occurred_at,
        statut=statut,
        prochaine_action=resultat["action"],
        prochaine_action_at=(base + timedelta(days=jours)).isoformat() if jours else "",
    )
    if resultat_key == "ferme":
        db.mark_ferme(prospect["id"])


@app.post("/tournee/passage/{prospect_id}")
async def tournee_passage(request: Request, prospect_id: int):
    prospect = db.get_prospect(prospect_id)
    if prospect is None:
        raise StarletteHTTPException(status_code=404)
    form = await request.form()
    resultat = RESULTATS_PASSAGE.get(field(form, "resultat"))
    # On ne fait confiance qu'aux paramètres connus de la tournée : pas de redirection ailleurs.
    retour = parse_qs(urlsplit(field(form, "retour")).query) if field(form, "retour").startswith("/tournee") else {}
    params = {key: values[-1] for key, values in retour.items() if key in TOURNEE_KEYS and values}
    if resultat is None:
        return RedirectResponse("/tournee?" + urlencode(params), status_code=303)
    appliquer_passage(prospect, field(form, "resultat"), occurred_at=db.now(), notes=field(form, "notes"))
    point = coords_of(prospect)
    if point:
        # La suite de la tournée repart de ce salon, à l'heure qu'il est.
        params.update(
            {
                "depart_lat": f"{point[0]:.6f}",
                "depart_lon": f"{point[1]:.6f}",
                "depart_adresse": "",
                "heure_depart": datetime.now().strftime("%H:%M"),
            }
        )
    params["filtre"] = "1"
    return RedirectResponse("/tournee?" + urlencode(params) + "#etapes", status_code=303)


@app.post("/tournee/imprimer")
async def tournee_imprimer(request: Request):
    form = await request.form()
    arrets = []
    vus = set()
    for value in form.getlist("arret"):
        pid_text, _sep, heure = str(value).partition("|")
        if not pid_text.isdigit() or not HEURE.match(heure):
            continue
        pid = int(pid_text)
        if pid in vus or db.get_prospect(pid) is None:
            continue
        vus.add(pid)
        arrets.append({"id": pid, "heure": heure})
    if not arrets:
        return RedirectResponse("/tournee", status_code=303)
    mode = field(form, "mode")
    tournee_id = db.create_tournee(
        date.today().isoformat(),
        mode if mode in routing.MODE_IDS else "pied",
        field(form, "depart_label")[:120],
        arrets,
    )
    return RedirectResponse(f"/tournee/{tournee_id}/impression", status_code=303)


def tournee_ou_404(tournee_id: int) -> tuple[dict, list[dict]]:
    tournee = db.get_tournee(tournee_id)
    if tournee is None:
        raise StarletteHTTPException(status_code=404)
    stops = []
    for position, arret in enumerate(tournee["arrets"], start=1):
        prospect = db.get_prospect(arret["id"])
        if prospect is None:
            continue  # fiche supprimée depuis l'impression
        prospect["numero"] = position
        prospect["heure"] = arret["heure"]
        stops.append(prospect)
    return tournee, stops


@app.get("/tournee/{tournee_id}/impression")
def tournee_impression(request: Request, tournee_id: int):
    tournee, stops = tournee_ou_404(tournee_id)
    points = [coords_of(item) for item in stops]
    return templates.TemplateResponse(
        request,
        "impression.html",
        {
            "tournee": tournee,
            "stops": stops,
            "jour_label": jour_fr(date.fromisoformat(tournee["jour"])),
            "mode_label": dict(routing.MODES).get(tournee["mode"], ""),
            "outils_terrain": OUTILS_TERRAIN,
            "map_data": {
                "start": None,
                "stops": [
                    {"n": item["numero"], "lat": p[0], "lon": p[1], "nom": item["nom_affiche"], "niveau": item["niveau"]}
                    for item, p in zip(stops, points)
                    if p
                ],
                "retour": False,
            },
        },
    )


OUTILS_TERRAIN = ("aucun", "planity", "treatwell", "booksy", "fresha", "kiute", "google", "autre")
SAISIE_RESULTATS = (
    ("", "Pas fait"),
    ("absent", "Décideur absent"),
    ("interesse", "Intéressé"),
    ("rdv", "RDV pris"),
    ("plus_tard", "Plus tard"),
    ("refus", "Pas intéressé"),
    ("ferme", "Fermé / introuvable"),
)


def render_saisie(request: Request, tournee: dict, stops: list[dict], *, values: dict | None = None, errors: dict | None = None):
    return render(
        request,
        "saisie.html",
        active="tournee",
        tournee=tournee,
        stops=stops,
        values=values or {},
        errors=errors or {},
        resultats=SAISIE_RESULTATS,
        outils_terrain=OUTILS_TERRAIN,
        jour_label=jour_fr(date.fromisoformat(tournee["jour"])),
    )


@app.get("/tournee/{tournee_id}/saisie")
def tournee_saisie(request: Request, tournee_id: int):
    tournee, stops = tournee_ou_404(tournee_id)
    return render_saisie(request, tournee, stops)


@app.post("/tournee/{tournee_id}/saisie")
async def tournee_saisie_save(request: Request, tournee_id: int):
    tournee, stops = tournee_ou_404(tournee_id)
    form = await request.form()
    jour = date.fromisoformat(tournee["jour"])
    values: dict[int, dict] = {}
    errors: dict[int, str] = {}
    plan: list[tuple[dict, dict]] = []
    # 1. Tout valider d'abord : rien n'est enregistré si une ligne est fausse.
    for stop in stops:
        pid = stop["id"]
        row = {
            "resultat": field(form, f"resultat_{pid}"),
            "heure": field(form, f"heure_{pid}"),
            "notes": field(form, f"notes_{pid}"),
            "contact_nom": field(form, f"contact_nom_{pid}"),
            "telephone": field(form, f"telephone_{pid}"),
            "email": field(form, f"email_{pid}"),
            "instagram": field(form, f"instagram_{pid}"),
            "outil": field(form, f"outil_{pid}"),
            "rdv_at": field(form, f"rdv_at_{pid}"),
        }
        values[pid] = row
        if row["resultat"] and row["resultat"] not in RESULTATS_PASSAGE:
            row["resultat"] = ""
        telephone, error = clean_phone(row["telephone"])
        if not error:
            email, error = clean_email(row["email"])
        if not error and row["heure"] and not HEURE.match(row["heure"]):
            error = "L'heure de passage doit être au format 14:30."
        rdv_at = ""
        if not error and row["rdv_at"]:
            try:
                rdv_at = datetime.fromisoformat(row["rdv_at"]).isoformat(timespec="seconds")
            except ValueError:
                error = "La date du rendez-vous n'est pas valide."
        if error:
            errors[pid] = error
            continue
        updates = {}
        if row["contact_nom"]:
            updates["contact_nom"] = row["contact_nom"]
        if telephone:
            updates["telephone"] = telephone
        if email:
            updates["email"] = email
        if clean_instagram(row["instagram"]):
            updates["instagram"] = clean_instagram(row["instagram"])
        if row["outil"] in OUTILS_TERRAIN:
            updates["outil_reservation"] = row["outil"]
        plan.append((stop, {"updates": updates, "rdv_at": rdv_at, **row}))
    if errors:
        return render_saisie(request, tournee, stops, values=values, errors=errors)

    # 2. Puis enregistrer.
    enregistres = 0
    for stop, row in plan:
        if row["updates"]:
            db.update_prospect(stop["id"], row["updates"], official=bool(stop["officiel"]))
        if row["resultat"]:
            heure = row["heure"] or stop["heure"]
            occurred_at = f"{tournee['jour']}T{heure}:00"
            appliquer_passage(db.get_prospect(stop["id"]), row["resultat"], occurred_at=occurred_at, notes=row["notes"], base=jour)
            enregistres += 1
        elif row["notes"]:
            db.add_interaction(stop["id"], kind="note", notes=row["notes"], occurred_at=f"{tournee['jour']}T12:00:00")
        if row["rdv_at"]:
            db.add_interaction(stop["id"], kind="rdv", notes="Rendez-vous noté pendant la tournée", occurred_at=row["rdv_at"], statut="rdv")
    db.mark_tournee_saisie(tournee["id"])
    return RedirectResponse(f"/aujourdhui?saisie={enregistres}", status_code=303)


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
