import asyncio
import time
from datetime import date

import httpx

from app import scoring, sirene
from app.geo import Place
from tests.conftest import manual_prospect

AUJOURDHUI = date(2026, 11, 17)


def score(**data):
    return scoring.compute({"statut": "a_contacter", "actif": 1, **data}, today=AUJOURDHUI)


def test_salon_sans_outil_recent_et_interesse_est_chaud():
    resultat = score(
        outil_reservation="aucun",
        effectif="3 à 5 salariés",
        date_creation="2026-03-01",
        statut="interesse",
    )
    assert resultat["niveau"] == "chaud"
    assert resultat["score"] == 30 + 15 + 20 + 15


def test_salon_inconnu_est_froid_et_explique():
    resultat = score()
    assert resultat["niveau"] == "froid"
    assert ("Outil de réservation à vérifier", 10) in resultat["raisons"]


def test_concurrent_ne_rapporte_rien_mais_reste_explique():
    resultat = score(outil_reservation="planity")
    assert any("Planity" in texte and points == 0 for texte, points in resultat["raisons"])


def test_score_plafonne_a_100():
    resultat = score(
        outil_reservation="aucun",
        effectif="3 à 5 salariés",
        date_creation="2026-10-01",
        site_presence="non",
        statut="essai",
        telephone="06 12 34 56 78",
    )
    assert resultat["score"] == 100


def test_clients_refus_et_fermes():
    assert score(statut="client")["niveau"] == "client"
    assert score(statut="refuse")["niveau"] == "refuse"
    assert score(actif=0)["score"] == 0


def test_score_reproductible():
    data = {"outil_reservation": "aucun", "effectif": "1 ou 2 salariés", "date_creation": "2024-05-01"}
    assert score(**data) == score(**data)


def test_tableau_trie_par_score(client):
    froid = manual_prospect(client, marque="Froid")
    chaud = manual_prospect(client, marque="Chaud", outil_reservation="aucun", statut="interesse")
    page = client.get("/?tri=score").text
    assert page.index('href="/prospects/%d"' % chaud) < page.index('href="/prospects/%d"' % froid)


def faux_sirene(total_pages: int):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        calls.append(page)
        results = [{"siren": f"{page:09d}", "matching_etablissements": []}] if page <= total_pages else []
        return httpx.Response(200, json={"results": results, "total_results": total_pages * 25, "total_pages": total_pages})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


def test_plus_de_200_resultats(monkeypatch):
    monkeypatch.setattr(sirene, "_limiter", sirene.RateLimiter(10_000))
    client, calls = faux_sirene(30)
    companies, reported, truncated = asyncio.run(sirene.fetch_companies(client, {}, sirene.NAF_MAX_PAGES))
    assert len(companies) == 30 and calls == list(range(1, 31))
    assert reported == 750 and truncated is False


def test_plafond_de_l_api_signale(monkeypatch):
    monkeypatch.setattr(sirene, "_limiter", sirene.RateLimiter(10_000))
    client, calls = faux_sirene(450)
    _companies, _reported, truncated = asyncio.run(sirene.fetch_companies(client, {}, sirene.NAF_MAX_PAGES))
    assert len(calls) == 400 and truncated is True


def test_debit_limite():
    limiter = sirene.RateLimiter(20)

    async def run():
        start = time.monotonic()
        await asyncio.gather(*(limiter.wait() for _ in range(11)))
        return time.monotonic() - start

    assert asyncio.run(run()) >= 0.45  # 10 intervalles de 50 ms


def test_reessaie_apres_429(monkeypatch):
    monkeypatch.setattr(sirene, "_limiter", sirene.RateLimiter(10_000))
    monkeypatch.setattr(asyncio, "sleep", _sans_attente)
    responses = iter([httpx.Response(429), httpx.Response(200, json={"results": [], "total_pages": 0})])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(responses)))
    assert asyncio.run(sirene.get_json(client, {"q": "x"}))["results"] == []


_sleep = asyncio.sleep


async def _sans_attente(_delay):
    await _sleep(0)
