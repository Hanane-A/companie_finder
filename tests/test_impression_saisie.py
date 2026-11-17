from datetime import date, timedelta

import pytest

from app import db, main
from tests.conftest import manual_prospect


@pytest.fixture()
def hors_ligne(monkeypatch):
    async def no_geocode(_client, _query):
        return None

    async def no_matrix(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "geocode", no_geocode)
    monkeypatch.setattr(main.ors, "fetch_matrix", no_matrix)


def imprimer(client, *arrets):
    data = {"mode": "pied", "depart_label": "Votre position", "arret": [f"{pid}|{heure}" for pid, heure in arrets]}
    response = client.post("/tournee/imprimer", data=data, follow_redirects=False)
    assert response.status_code == 303
    return int(response.headers["location"].split("/")[2])


def test_impression_enregistre_l_ordre(client):
    a = manual_prospect(client, marque="Premier", telephone="0612345678")
    b = manual_prospect(client, marque="Second")
    tid = imprimer(client, (b, "09:30"), (a, "09:50"))
    tournee = db.get_tournee(tid)
    assert [arret["id"] for arret in tournee["arrets"]] == [b, a]
    page = client.get(f"/tournee/{tid}/impression").text
    assert page.index("Second") < page.index("Premier")
    assert "06 12 34 56 78" in page and "Planity" in page


def test_impression_ignore_les_valeurs_invalides(client):
    a = manual_prospect(client)
    response = client.post(
        "/tournee/imprimer",
        data={"arret": ["abc|09:00", f"{a}|99:99", "99999|09:00", f"{a}|10:00", f"{a}|10:00"]},
        follow_redirects=False,
    )
    tid = int(response.headers["location"].split("/")[2])
    assert db.get_tournee(tid)["arrets"] == [{"id": a, "heure": "10:00"}]
    vide = client.post("/tournee/imprimer", data={"arret": ["x"]}, follow_redirects=False)
    assert vide.headers["location"] == "/tournee"


def test_saisie_du_soir(client):
    interesse = manual_prospect(client, marque="Interesse")
    absent = manual_prospect(client, marque="Absent")
    pas_fait = manual_prospect(client, marque="Pas fait", telephone="0611111111")
    tid = imprimer(client, (interesse, "09:30"), (absent, "09:50"), (pas_fait, "10:10"))
    assert "Tournée n° %d" % tid in client.get("/aujourdhui").text

    response = client.post(
        f"/tournee/{tid}/saisie",
        data={
            f"resultat_{interesse}": "interesse",
            f"heure_{interesse}": "09:40",
            f"telephone_{interesse}": "06 22 33 44 55",
            f"outil_{interesse}": "aucun",
            f"notes_{interesse}": "4 postes, agenda papier",
            f"resultat_{absent}": "absent",
            f"resultat_{pas_fait}": "",
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/aujourdhui?saisie=2"

    p = db.get_prospect(interesse)
    assert p["statut"] == "interesse" and p["telephone"] == "06 22 33 44 55"
    assert p["outil_reservation"] == "aucun" and p["niveau"] == "chaud"
    passage = db.list_interactions(interesse)[0]
    assert passage["occurred_at"].endswith("T09:40:00") and "agenda papier" in passage["notes"]

    assert db.get_prospect(absent)["statut"] == "visite"
    # Un champ vide n'efface jamais une info connue, et un arrêt « pas fait » reste intact.
    assert db.get_prospect(pas_fait)["telephone"] == "06 11 11 11 11"
    assert db.list_interactions(pas_fait) == []
    assert db.get_tournee(tid)["saisie_at"]
    assert "Tournée n° %d" % tid not in client.get("/aujourdhui").text


def test_saisie_refusee_si_une_ligne_est_fausse(client):
    a = manual_prospect(client, marque="A")
    b = manual_prospect(client, marque="B")
    tid = imprimer(client, (a, "09:30"), (b, "09:50"))
    response = client.post(
        f"/tournee/{tid}/saisie",
        data={f"resultat_{a}": "interesse", f"resultat_{b}": "absent", f"telephone_{b}": "123"},
    )
    assert response.status_code == 200
    assert "10 chiffres" in response.text
    # Rien n'est enregistré, et la saisie est conservée à l'écran.
    assert db.list_interactions(a) == [] and db.list_interactions(b) == []
    assert 'value="123"' in response.text


def test_rdv_note_sur_la_feuille(client):
    a = manual_prospect(client, marque="RDV")
    tid = imprimer(client, (a, "09:30"))
    demain = (date.today() + timedelta(days=1)).isoformat()
    client.post(f"/tournee/{tid}/saisie", data={f"resultat_{a}": "rdv", f"rdv_at_{a}": f"{demain}T15:00"})
    assert db.get_prospect(a)["statut"] == "rdv"
    assert "Rendez-vous à venir" in client.get("/aujourdhui").text


def test_salon_ferme(client):
    a = manual_prospect(client, marque="Ferme", prochaine_action_at="2026-12-01", prochaine_action="Repasser")
    tid = imprimer(client, (a, "09:30"))
    client.post(f"/tournee/{tid}/saisie", data={f"resultat_{a}": "ferme"})
    p = db.get_prospect(a)
    assert p["actif"] == 0 and p["prochaine_action_at"] == "" and p["score"] == 0


def test_saisie_ignore_les_prospects_hors_tournee(client):
    a = manual_prospect(client, marque="Dans la tournee")
    intrus = manual_prospect(client, marque="Intrus")
    tid = imprimer(client, (a, "09:30"))
    client.post(f"/tournee/{tid}/saisie", data={f"resultat_{intrus}": "refus"})
    assert db.get_prospect(intrus)["statut"] == "a_contacter"


def test_tournee_inconnue(client):
    assert client.get("/tournee/999/impression").status_code == 404
    assert client.get("/tournee/999/saisie").status_code == 404


def test_bouton_imprimer_sur_la_tournee(client, hors_ligne):
    pid = manual_prospect(client, marque="Salon")
    db.set_coords(pid, 50.71, 3.17)
    page = client.get("/tournee?filtre=1").text
    assert 'name="arret" value="%d|' % pid in page
    assert "Imprimer la tournée et les fiches" in page
