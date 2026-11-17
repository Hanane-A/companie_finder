import asyncio
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app import db, main, ors, routing
from tests.conftest import manual_prospect

DEPART = datetime(2026, 11, 17, 9, 0)


def stops_on_a_line(*offsets):
    # Points sur une même latitude : l'ordre optimal est évident.
    return [routing.Stop(i + 1, 50.70, 3.15 + offset) for i, offset in enumerate(offsets)]


def test_ordre_optimal_sur_une_ligne():
    plan = routing.optimise((50.70, 3.15), stops_on_a_line(0.03, 0.01, 0.02), mode="pied", depart=DEPART, visite_min=10)
    assert [visit.stop_id for visit in plan.visits] == [2, 3, 1]


def test_resultat_reproductible():
    stops = [routing.Stop(i, 50.7 + (i * 7 % 5) / 300, 3.15 + (i * 3 % 7) / 300) for i in range(1, 15)]
    first = routing.optimise((50.7, 3.15), stops, mode="voiture", depart=DEPART, visite_min=5)
    second = routing.optimise((50.7, 3.15), list(stops), mode="voiture", depart=DEPART, visite_min=5)
    assert [v.stop_id for v in first.visits] == [v.stop_id for v in second.visits]


def test_2opt_jamais_pire_que_plus_proche_voisin():
    points = [(50.7, 3.15)] + [(50.7 + (i * 37 % 11) / 400, 3.15 + (i * 53 % 13) / 400) for i in range(1, 20)]
    matrix = routing.estimate_matrix(points, "pied")
    greedy = routing.nearest_neighbour(matrix, 19)
    improved = routing.two_opt(matrix, greedy, retour=False)
    assert sorted(improved) == sorted(greedy)
    assert routing.path_cost(matrix, improved, retour=False) <= routing.path_cost(matrix, greedy, retour=False)


def test_heures_d_arrivee_et_creneau():
    stops = stops_on_a_line(0.01, 0.02)
    plan = routing.optimise(
        (50.70, 3.15), stops, mode="pied", depart=DEPART, visite_min=10,
        fin_souhaitee=DEPART + timedelta(minutes=25),
    )
    first, second = plan.visits
    assert first.arrival == DEPART + timedelta(minutes=first.leg.minutes)
    assert second.arrival == first.departure + timedelta(minutes=second.leg.minutes)
    assert plan.fin == second.departure
    assert second.hors_creneau and not first.hors_creneau


def test_retour_au_depart_compte_le_dernier_trajet():
    stops = stops_on_a_line(0.02)
    aller = routing.optimise((50.70, 3.15), stops, mode="velo", depart=DEPART, visite_min=0)
    boucle = routing.optimise((50.70, 3.15), stops, mode="velo", depart=DEPART, visite_min=0, retour=True)
    assert boucle.trajet_min == pytest.approx(aller.trajet_min * 2)


def test_transports_marche_sur_les_petits_trajets():
    court = routing.estimate_leg((50.70, 3.150), (50.70, 3.155), "transports")
    long = routing.estimate_leg((50.70, 3.15), (50.75, 3.25), "transports")
    assert court.a_pied and not long.a_pied
    assert long.minutes > routing.TRANSPORTS_ATTENTE_MIN


def test_liens_google_maps():
    points = [(50.7, 3.15 + i / 100) for i in range(26)]
    assert len(routing.google_maps_chunks(points, "pied")) == 3
    assert len(routing.google_maps_chunks(points, "transports")) == 25
    url = routing.google_maps_url(points[:3], "voiture")
    assert "travelmode=driving" in url and "waypoints=" in url


def test_openrouteservice_utilise_si_disponible():
    def handler(request):
        assert request.headers["Authorization"] == "cle"
        return httpx.Response(200, json={"durations": [[0, 600], [600, 0]], "distances": [[0, 800], [800, 0]]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    matrix = asyncio.run(ors.fetch_matrix(client, [(50.7, 3.15), (50.71, 3.16)], "pied", key="cle"))
    assert matrix[0][1].minutes == 10 and matrix[0][1].metres == 800


def test_openrouteservice_en_panne_ou_sans_cle():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(429)))
    points = [(50.7, 3.15), (50.71, 3.16)]
    assert asyncio.run(ors.fetch_matrix(client, points, "pied", key="cle")) is None
    assert asyncio.run(ors.fetch_matrix(client, points, "pied", key="")) is None
    assert asyncio.run(ors.fetch_matrix(client, points, "transports", key="cle")) is None


@pytest.fixture()
def hors_ligne(monkeypatch):
    async def fake_geocode(_client, query):
        return (50.72, 3.16) if "Rue Connue" in query else None

    async def no_matrix(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "geocode", fake_geocode)
    monkeypatch.setattr(main.ors, "fetch_matrix", no_matrix)


def prospect_place(client, nom, lat, lon, **extra):
    pid = manual_prospect(client, marque=nom, **extra)
    db.set_coords(pid, lat, lon)
    return pid


def test_page_tournee_ordonnee(client, hors_ligne):
    loin = prospect_place(client, "Loin", 50.70, 3.18)
    pres = prospect_place(client, "Pres", 50.70, 3.16)
    page = client.get("/tournee?filtre=1&depart_lat=50.70&depart_lon=3.15&heure_depart=09:00&mode=pied").text
    assert page.index('href="/prospects/%d"' % pres) < page.index('href="/prospects/%d"' % loin)
    assert "Votre position" in page and "Fin estimée" in page


def test_geocodage_des_fiches_manuelles(client, hors_ligne):
    pid = manual_prospect(client, marque="Sans GPS", adresse="3 Rue Connue", ville="Roubaix")
    client.get("/tournee?filtre=1")
    prospect = db.get_prospect(pid)
    assert prospect["latitude"].startswith("50.72")


def test_adresse_introuvable_listee_a_part(client, hors_ligne):
    manual_prospect(client, marque="Fantome", adresse="nulle part", ville="Inconnue")
    page = client.get("/tournee?filtre=1").text
    assert "Sans position (1)" in page


def test_passage_interesse_met_a_jour_et_repart_du_salon(client, hors_ligne):
    pid = prospect_place(client, "Salon", 50.71, 3.17)
    retour = "/tournee?filtre=1&mode=velo&cp=59100&masquer_faits=1"
    response = client.post(f"/tournee/passage/{pid}", data={"resultat": "interesse", "retour": retour, "notes": "Gérante sympa"}, follow_redirects=False)
    assert response.status_code == 303
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["mode"] == ["velo"] and params["cp"] == ["59100"]
    assert params["depart_lat"] == ["50.710000"]
    prospect = db.get_prospect(pid)
    assert prospect["statut"] == "interesse"
    assert prospect["prochaine_action_at"] == (date.today() + timedelta(days=1)).isoformat()
    assert "Gérante sympa" in db.list_interactions(pid)[0]["notes"]


def test_passage_fait_disparait_de_la_tournee(client, hors_ligne):
    pid = prospect_place(client, "Deja vu", 50.71, 3.17)
    client.post(f"/tournee/passage/{pid}", data={"resultat": "absent", "retour": "/tournee?filtre=1&masquer_faits=1"})
    assert db.get_prospect(pid)["statut"] == "visite"
    page = client.get("/tournee?filtre=1&masquer_faits=1&masquer_refuses=1&masquer_clients=1").text
    assert 'href="/prospects/%d"' % pid not in page
    assert "1 passage déjà fait aujourd'hui" in page


def test_refus_sort_de_la_tournee(client, hors_ligne):
    pid = prospect_place(client, "Non merci", 50.71, 3.17)
    client.post(f"/tournee/passage/{pid}", data={"resultat": "refus", "retour": "/tournee?filtre=1"})
    assert db.get_prospect(pid)["statut"] == "refuse"
    assert db.get_prospect(pid)["prochaine_action_at"] == ""


def test_retour_vers_un_autre_site_ignore(client, hors_ligne):
    pid = prospect_place(client, "Salon", 50.71, 3.17)
    response = client.post(
        f"/tournee/passage/{pid}",
        data={"resultat": "absent", "retour": "https://exemple.com/tournee?mode=voiture"},
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert location.startswith("/tournee?") and "exemple.com" not in location


def test_reglages_invalides_ramenes_a_des_valeurs_sures(client, hors_ligne):
    prospect_place(client, "Salon", 50.71, 3.17)
    response = client.get("/tournee?filtre=1&mode=fusee&max_arrets=9999&heure_depart=99:99&depart_lat=500")
    assert response.status_code == 200
    options = main.tournee_options({"filtre": "1", "mode": "fusee", "max_arrets": "9999", "depart_lat": "500"})
    assert options["mode"] == "pied" and options["max_arrets"] == routing.MAX_ARRETS and options["depart_lat"] is None
