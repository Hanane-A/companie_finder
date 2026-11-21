from datetime import datetime, timedelta

from app import db, stats
from tests.conftest import manual_prospect

MAINTENANT = datetime(2026, 11, 17, 18, 0)


def il_y_a(jours, heure=10):
    return (MAINTENANT - timedelta(days=jours)).replace(hour=heure, minute=0).isoformat()


def contact(pid, jours, kind="passage", apres="", notes=""):
    return {"id": None, "prospect_id": pid, "type": kind, "notes": notes, "statut_avant": "", "statut_apres": apres, "occurred_at": il_y_a(jours)}


PROSPECTS = [
    {"id": 1, "statut": "client", "secteur": "Coiffure"},
    {"id": 2, "statut": "refuse", "secteur": "Coiffure"},
    {"id": 3, "statut": "demo", "secteur": "Beauté"},
    {"id": 4, "statut": "visite", "secteur": "Beauté"},
    {"id": 5, "statut": "a_contacter", "secteur": "Coiffure"},
    {"id": 6, "statut": "interesse", "secteur": "Coiffure"},  # contacté il y a 60 jours
]
INTERACTIONS = [
    contact(1, 20, notes="Passage : intéressé", apres="interesse"),
    contact(1, 14, "demo", apres="demo"),
    contact(1, 10, "appel", apres="client"),
    contact(2, 18, notes="Passage : intéressé", apres="interesse"),
    contact(2, 12, "appel", apres="refuse"),
    contact(3, 9, notes="Passage : rendez-vous pris", apres="rdv"),
    contact(3, 3, "demo", apres="demo"),
    contact(4, 2, notes="Passage : décideur absent", apres="visite"),
    {**contact(4, 1, "note"), "notes": "Vu sur Maps"},
    contact(6, 60, "appel", apres="interesse"),
    {**contact(3, -2, "rdv"), "notes": "Démo prévue"},  # dans le futur : ignoré
]


def calcul(jours=30):
    return stats.calculer(PROSPECTS, INTERACTIONS, jours=jours, maintenant=MAINTENANT)


def test_entonnoir_sur_la_cohorte_de_la_periode():
    totaux = {e["label"]: e["total"] for e in calcul()["entonnoir"]}
    # Prospects 1 à 4 contactés dans les 30 jours ; le 6 l'a été il y a 60 jours.
    assert totaux == {"Contactés": 4, "Intéressés": 3, "Rendez-vous pris": 2, "Démo faite": 2, "Essai en cours": 1, "Clients": 1}


def test_taux_par_etape_compare_a_l_etape_precedente():
    etapes = calcul()["entonnoir"]
    assert etapes[0]["taux"] is None
    assert etapes[1]["taux"] == 75.0  # 3 intéressés sur 4 contactés
    assert etapes[2]["taux"] == 66.7
    assert calcul()["kpi"]["conversion"] == 25.0  # 1 client sur 4, calculé sur les totaux


def test_un_refus_garde_l_etape_atteinte():
    chemin = stats.parcours(PROSPECTS, INTERACTIONS, maintenant=MAINTENANT)[2]
    assert chemin.rang == 2


def test_contacts_et_futurs_exclus():
    kpi = calcul()["kpi"]
    # 8 contacts passés dans les 30 jours (3 + 2 + 2 + 1), hors note et hors rendez-vous futur.
    assert kpi["contacts"] == 8
    assert kpi["passages"] == 4
    assert kpi["prospects_contactes"] == 4


def test_nouveaux_rdv_et_clients_de_la_periode():
    kpi = calcul()["kpi"]
    assert kpi["nouveaux_rdv"] == 2  # prospects 1 (client direct) et 3
    assert kpi["nouveaux_clients"] == 1


def test_delai_avant_signature():
    kpi = calcul()["kpi"]
    assert kpi["delai_moyen"] == 10.0  # du 1er contact (J-20) à la signature (J-10)
    assert kpi["delai_median"] == 10.0
    demo = next(d for d in calcul()["delais_etapes"] if d["label"] == "Démo faite")
    assert demo["nombre"] == 2 and demo["moyenne"] == 6.0  # (6 + 6) / 2


def test_periode_courte():
    resultat = calcul(7)
    assert resultat["kpi"]["contacts"] == 2  # démo du prospect 3 (J-3) + passage du 4 (J-2)
    assert resultat["entonnoir"][0]["total"] == 1  # seul le 4 a eu son premier contact dans les 7 jours


def test_depuis_le_debut():
    totaux = {e["label"]: e["total"] for e in calcul(0)["entonnoir"]}
    assert totaux["Contactés"] == 5 and totaux["Intéressés"] == 4


def test_resultats_des_passages():
    assert dict(calcul()["resultats_passage"]) == {"Intéressé": 2, "RDV pris": 1, "Décideur absent": 1}


def test_contacts_par_semaine():
    semaines = calcul()["semaines"]
    assert len(semaines) == 6
    assert sum(s["total"] for s in semaines) == 8
    assert semaines[-1]["debut"] == datetime(2026, 11, 16).date()
    assert semaines[-1]["total"] == 0  # semaine en cours : lundi 16 et mardi 17, aucun contact
    assert semaines[-2]["passage"] == 1 and semaines[-2]["rdv"] == 1 and semaines[-2]["total"] == 2


def test_par_secteur():
    lignes = {l["secteur"]: l for l in calcul()["par_secteur"]}
    assert lignes["Coiffure"]["contactes"] == 2 and lignes["Coiffure"]["clients"] == 1
    assert lignes["Coiffure"]["taux"] == 50.0
    assert lignes["Beauté"]["taux"] == 0.0


def test_page_performance(client):
    pid = manual_prospect(client, marque="Salon stats")
    client.post(f"/prospects/{pid}/journal", data={"type": "passage", "statut": "interesse"})
    page = client.get("/performance?jours=7").text
    assert "Taux de conversion par étape" in page and "Contacts par semaine" in page
    assert client.get("/performance?jours=abc").status_code == 200
    assert client.get("/performance?jours=365").status_code == 200


def test_page_performance_vide(client):
    page = client.get("/performance").text
    assert "Aucun premier contact sur cette période" in page
    assert db.count_all() == 0
