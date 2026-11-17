from datetime import date, datetime, timedelta

from app import db
from tests.conftest import manual_prospect


def jour(delta: int) -> str:
    return (date.today() + timedelta(days=delta)).isoformat()


def test_classement_retard_jour_semaine(client):
    retard = manual_prospect(client, marque="Retard", prochaine_action_at=jour(-2), prochaine_action="Rappeler")
    du_jour = manual_prospect(client, marque="Du jour", prochaine_action_at=jour(0), prochaine_action="Passer")
    semaine = manual_prospect(client, marque="Semaine", prochaine_action_at=jour(4), prochaine_action="Démo")
    manual_prospect(client, marque="Trop loin", prochaine_action_at=jour(30), prochaine_action="Plus tard")

    page = client.get("/aujourdhui").text
    assert "En retard de 2 jours" in page
    assert page.index("Retard") < page.index("Du jour") < page.index("Semaine")
    assert "Trop loin" not in page
    assert {retard, du_jour, semaine}  # identifiants créés


def test_clients_et_refus_ne_sont_jamais_relances(client):
    pid = manual_prospect(client, marque="Fini", prochaine_action_at=jour(-1), prochaine_action="Rappeler")
    db.set_statut(pid, "refuse")
    assert db.relances(jour(7)) == []
    assert db.count_due() == 0


def test_badge_de_navigation(client):
    manual_prospect(client, prochaine_action_at=jour(-1), prochaine_action="Rappeler")
    manual_prospect(client, prochaine_action_at=jour(3), prochaine_action="Plus tard")
    assert db.count_due() == 1
    assert 'title="Relances dues">1<' in client.get("/").text


def test_reporter_une_relance(client):
    pid = manual_prospect(client, prochaine_action_at=jour(-1), prochaine_action="Rappeler")
    response = client.post(f"/prospects/{pid}/reporter", data={"jours": "3"}, follow_redirects=False)
    assert response.status_code == 303
    assert db.get_prospect(pid)["prochaine_action_at"] == jour(3)


def test_reporter_refuse_une_duree_inconnue(client):
    pid = manual_prospect(client, prochaine_action_at=jour(-1), prochaine_action="Rappeler")
    client.post(f"/prospects/{pid}/reporter", data={"jours": "365"})
    assert db.get_prospect(pid)["prochaine_action_at"] == jour(-1)


def test_rendez_vous_futur_affiche_mais_pas_compte_comme_contact(client):
    pid = manual_prospect(client, marque="Salon RDV")
    demain = datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).replace(hour=10)
    client.post(
        f"/prospects/{pid}/journal",
        data={"type": "demo", "occurred_at": demain.strftime("%Y-%m-%dT%H:%M"), "notes": "Démo HANUP Desk"},
    )
    page = client.get("/aujourdhui").text
    assert "Rendez-vous à venir" in page and "Démo HANUP Desk" in page
    assert db.get_prospect(pid)["dernier_contact_at"] == ""
    assert db.activite(jour(0))["total"] == 0


def test_interesses_sans_relance(client):
    pid = manual_prospect(client, marque="Chaud")
    db.set_statut(pid, "interesse")
    assert [item["id"] for item in db.chauds_sans_relance()] == [pid]
    client.post(f"/prospects/{pid}/journal", data={"type": "appel", "prochaine_action_at": jour(2)})
    assert db.chauds_sans_relance() == []


def test_activite_du_jour(client):
    pid = manual_prospect(client)
    client.post(f"/prospects/{pid}/journal", data={"type": "passage"})
    client.post(f"/prospects/{pid}/journal", data={"type": "appel"})
    client.post(f"/prospects/{pid}/journal", data={"type": "note"})
    counts = db.activite(jour(0))
    assert counts["total"] == 2
    assert counts["passage"] == 1


def test_page_vide(client):
    assert "Rien à relancer aujourd'hui" in client.get("/aujourdhui").text


def test_rendez_vous_prevu_nest_pas_un_oubli(client):
    pid = manual_prospect(client, marque="RDV calé")
    demain = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    client.post(f"/prospects/{pid}/journal", data={"type": "rdv", "occurred_at": demain, "statut": "rdv"})
    assert db.chauds_sans_relance() == []


def test_pipeline_en_colonnes(client):
    a = manual_prospect(client, marque="Carte A")
    db.set_statut(a, "interesse")
    page = client.get("/pipeline").text
    assert 'data-statut="interesse"' in page and 'data-id="%d"' % a in page
    colonne = page[page.index('data-statut="interesse"'):page.index('data-statut="rdv"')]
    assert 'data-id="%d"' % a in colonne
