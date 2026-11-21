"""Statistiques de performance commerciale.

Fonctions pures : elles reçoivent les fiches et le journal, et rendent des
chiffres. Aucune lecture de la base ici, pour pouvoir tout tester.

Définitions (les mêmes partout dans l'outil) :
- Un « contact » est une ligne du journal de type passage, appel, SMS,
  message Instagram, email, rendez-vous ou démonstration, dont la date est
  passée. Les notes et les changements de statut ne comptent pas.
- Chaque prospect a une « étape atteinte » : la plus avancée qu'il ait connue,
  d'après son statut actuel et l'historique de ses statuts. Un salon passé
  par « Intéressé » puis « Refusé » compte comme ayant atteint « Intéressé ».
- L'entonnoir porte sur une cohorte : les prospects dont le premier contact
  tombe dans la période. Chaque taux = étape / étape précédente.
- Les taux globaux se calculent sur les totaux, jamais en moyennant des taux.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean, median

CONTACT_TYPES = ("passage", "appel", "sms", "dm", "email", "rdv", "demo")

RANG = {
    "a_contacter": 0,
    "contacte": 1,
    "visite": 1,
    "plus_tard": 1,
    "refuse": 1,
    "interesse": 2,
    "rdv": 3,
    "demo": 4,
    "essai": 5,
    "client": 6,
}

ETAPES = (
    (1, "Contactés"),
    (2, "Intéressés"),
    (3, "Rendez-vous pris"),
    (4, "Démo faite"),
    (5, "Essai en cours"),
    (6, "Clients"),
)

GROUPES_CONTACT = (
    ("passage", "Passages", ("passage",)),
    ("appel", "Appels", ("appel",)),
    ("message", "Messages", ("sms", "dm", "email")),
    ("rdv", "RDV et démos", ("rdv", "demo")),
)

RESULTATS_PASSAGE = (
    ("Passage : décideur absent", "Décideur absent"),
    ("Passage : intéressé", "Intéressé"),
    ("Passage : rendez-vous pris", "RDV pris"),
    ("Passage : pas maintenant", "Plus tard"),
    ("Passage : pas intéressé", "Pas intéressé"),
    ("Passage : salon fermé ou introuvable", "Fermé / introuvable"),
)

PERIODES = (
    (7, "7 jours"),
    (30, "30 jours"),
    (90, "90 jours"),
    (0, "Depuis le début"),
)


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass
class Parcours:
    """Ce qu'on sait du chemin d'un prospect dans l'entonnoir."""

    prospect_id: int
    secteur: str
    rang: int = 0
    # Date à laquelle chaque rang a été atteint pour la première fois (si connue).
    dates: dict[int, datetime] = field(default_factory=dict)

    @property
    def premier_contact(self) -> datetime | None:
        return self.dates.get(1)


def parcours(prospects: list[dict], interactions: list[dict], *, maintenant: datetime) -> dict[int, Parcours]:
    result: dict[int, Parcours] = {}
    for p in prospects:
        result[int(p["id"])] = Parcours(int(p["id"]), p.get("secteur") or "Non renseigné", RANG.get(p.get("statut") or "", 0))
    for i in sorted(interactions, key=lambda row: (row.get("occurred_at") or "", row.get("id") or 0)):
        item = result.get(int(i["prospect_id"]))
        quand = _dt(i.get("occurred_at"))
        if item is None or quand is None or quand > maintenant:
            continue
        atteint = 0
        if i.get("type") in CONTACT_TYPES:
            atteint = 1
        atteint = max(atteint, RANG.get(i.get("statut_apres") or "", 0))
        if atteint:
            # Atteindre un rang, c'est aussi avoir franchi les précédents.
            for rang in range(1, atteint + 1):
                item.dates.setdefault(rang, quand)
            item.rang = max(item.rang, atteint)
    return result


def debut_periode(jours: int, aujourd_hui: date) -> datetime | None:
    if not jours:
        return None
    return datetime.combine(aujourd_hui - timedelta(days=jours - 1), datetime.min.time())


def dans(quand: datetime | None, debut: datetime | None, fin: datetime) -> bool:
    return quand is not None and quand <= fin and (debut is None or quand >= debut)


def pourcentage(numerateur: int, denominateur: int) -> float | None:
    return round(100 * numerateur / denominateur, 1) if denominateur else None


def calculer(prospects: list[dict], interactions: list[dict], *, jours: int, maintenant: datetime) -> dict:
    debut = debut_periode(jours, maintenant.date())
    chemins = parcours(prospects, interactions, maintenant=maintenant)

    # Cohorte : premier contact dans la période. « Depuis le début » prend aussi
    # les prospects avancés sans date connue (statut posé à la création).
    if debut is None:
        cohorte = [c for c in chemins.values() if c.rang >= 1]
    else:
        cohorte = [c for c in chemins.values() if dans(c.premier_contact, debut, maintenant)]

    entonnoir = []
    precedent = None
    for rang, label in ETAPES:
        total = sum(1 for c in cohorte if c.rang >= rang)
        entonnoir.append(
            {
                "rang": rang,
                "label": label,
                "total": total,
                "taux": pourcentage(total, precedent) if precedent is not None else None,
            }
        )
        precedent = total
    contactes = entonnoir[0]["total"]
    clients_cohorte = entonnoir[-1]["total"]

    contacts = [
        i for i in interactions
        if i.get("type") in CONTACT_TYPES and dans(_dt(i.get("occurred_at")), debut, maintenant)
    ]
    par_type = Counter(i["type"] for i in contacts)

    nouveaux_rdv = sum(1 for c in chemins.values() if dans(c.dates.get(3), debut, maintenant))
    nouveaux_clients = [c for c in chemins.values() if dans(c.dates.get(6), debut, maintenant)]

    delais_signature = [
        (c.dates[6] - c.premier_contact).total_seconds() / 86400
        for c in nouveaux_clients
        if c.premier_contact is not None
    ]

    delais_etapes = []
    for rang, label in ETAPES[1:]:
        valeurs = [
            (c.dates[rang] - c.premier_contact).total_seconds() / 86400
            for c in chemins.values()
            if c.premier_contact is not None and dans(c.dates.get(rang), debut, maintenant)
        ]
        delais_etapes.append(
            {
                "label": label,
                "nombre": len(valeurs),
                "moyenne": round(mean(valeurs), 1) if valeurs else None,
                "mediane": round(median(valeurs), 1) if valeurs else None,
            }
        )

    passages = [i for i in contacts if i["type"] == "passage"]
    resultats = Counter()
    for i in passages:
        notes = i.get("notes") or ""
        label = next((nom for prefixe, nom in RESULTATS_PASSAGE if notes.startswith(prefixe)), "Autre passage")
        resultats[label] += 1
    resultats_passage = sorted(resultats.items(), key=lambda kv: (-kv[1], kv[0]))

    secteurs: dict[str, dict] = {}
    for c in cohorte:
        ligne = secteurs.setdefault(c.secteur, {"secteur": c.secteur, "contactes": 0, "interesses": 0, "clients": 0})
        ligne["contactes"] += 1
        ligne["interesses"] += c.rang >= 2
        ligne["clients"] += c.rang >= 6
    par_secteur = sorted(secteurs.values(), key=lambda row: (-row["contactes"], row["secteur"]))
    for ligne in par_secteur:
        ligne["taux"] = pourcentage(ligne["clients"], ligne["contactes"])

    return {
        "jours": jours,
        "debut": debut,
        "kpi": {
            "contacts": len(contacts),
            "prospects_contactes": len({i["prospect_id"] for i in contacts}),
            "passages": par_type.get("passage", 0),
            "nouveaux_rdv": nouveaux_rdv,
            "nouveaux_clients": len(nouveaux_clients),
            "conversion": pourcentage(clients_cohorte, contactes),
            "delai_moyen": round(mean(delais_signature), 1) if delais_signature else None,
            "delai_median": round(median(delais_signature), 1) if delais_signature else None,
            "signatures_mesurees": len(delais_signature),
        },
        "entonnoir": entonnoir,
        "semaines": semaines(interactions, jours=jours, maintenant=maintenant),
        "resultats_passage": resultats_passage,
        "delais_etapes": delais_etapes,
        "par_secteur": par_secteur,
    }


def semaines(interactions: list[dict], *, jours: int, maintenant: datetime) -> list[dict]:
    """Contacts par semaine (lundi → dimanche), la semaine en cours comprise."""
    nombre = {7: 4, 30: 6, 90: 13}.get(jours, 26)
    lundi = maintenant.date() - timedelta(days=maintenant.weekday())
    debuts = [lundi - timedelta(weeks=n) for n in range(nombre - 1, -1, -1)]
    lignes = [{"debut": d, "total": 0, **{cle: 0 for cle, _l, _t in GROUPES_CONTACT}} for d in debuts]
    index = {d: ligne for d, ligne in zip(debuts, lignes)}
    groupe_de = {t: cle for cle, _l, types in GROUPES_CONTACT for t in types}
    for i in interactions:
        quand = _dt(i.get("occurred_at"))
        if i.get("type") not in CONTACT_TYPES or quand is None or quand > maintenant:
            continue
        ligne = index.get(quand.date() - timedelta(days=quand.weekday()))
        if ligne is None:
            continue
        ligne["total"] += 1
        ligne[groupe_de[i["type"]]] += 1
    if jours == 0:
        # Sans période, on commence à la première semaine active.
        while len(lignes) > 4 and lignes[0]["total"] == 0:
            lignes.pop(0)
    return lignes
