from __future__ import annotations

import re
import unicodedata

STREET_PARTICLES = {
    "de",
    "du",
    "des",
    "la",
    "le",
    "les",
    "et",
    "aux",
    "au",
    "en",
    "sur",
    "sous",
}


def fold(value: str | None) -> str:
    text = unicodedata.normalize("NFD", value or "")
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return text.casefold()


def fold_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(value))


def mostly_upper(value: str) -> bool:
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return False
    return sum(char.isupper() for char in letters) / len(letters) >= 0.75


def french_title(value: str | None, *, particles: bool = False) -> str:
    text = " ".join((value or "").split())
    if not text or not mostly_upper(text):
        return text
    return " ".join(
        _title_token(word, first=index == 0, particles=particles)
        for index, word in enumerate(text.split(" "))
    )


def _title_token(word: str, *, first: bool, particles: bool) -> str:
    if "-" in word:
        return "-".join(
            _title_token(part, first=first or index == 0, particles=particles)
            for index, part in enumerate(word.split("-"))
        )
    for separator in ("'", "’"):
        if separator in word:
            left, right = word.split(separator, 1)
            left_key = fold(left)
            if left_key in {"l", "d", "n", "qu"} and not first:
                left_text = left_key
            else:
                left_text = left.capitalize()
            if len(right) == 1:
                right_text = right.lower()
            else:
                right_text = _title_token(right, first=False, particles=particles)
            return left_text + separator + right_text
    if any(char.isdigit() for char in word) and not any(char.isalpha() for char in word):
        return word
    lowered = word.lower()
    if particles and not first and lowered in STREET_PARTICLES:
        return lowered
    return lowered.capitalize()


def prettify_enseigne(value: str | None) -> str:
    text = " ".join((value or "").split())
    if not text:
        return ""
    if (
        " " not in text
        and "-" not in text
        and "'" not in text
        and "’" not in text
        and text.isupper()
        and len(text) <= 10
    ):
        return text
    return french_title(text, particles=False)


def street_number(value: str | None) -> int:
    match = re.match(r"\d+", value or "")
    if not match:
        return 10**9
    return int(match.group())


_STREET_TYPES = (
    "boulevard",
    "avenue",
    "impasse",
    "chemin",
    "allee",
    "allée",
    "place",
    "cours",
    "route",
    "square",
    "passage",
    "sentier",
    "quai",
    "voie",
    "cite",
    "cité",
    "residence",
    "résidence",
    "lotissement",
    "rue",
    "av",
    "bd",
)


def parse_street(adresse: str | None) -> tuple[str, str, str]:
    """Retrouve numéro, type et nom de voie quand SIRENE n'envoie que l'adresse en ligne."""
    text = " ".join((adresse or "").split())
    if not text:
        return "", "", ""
    types = "|".join(_STREET_TYPES)
    match = re.search(
        rf"(?P<num>\d+)\s+(?:(?P<indice>[A-Za-z])\s+)?(?P<kind>{types})\s+(?P<name>.+?)\s+\d{{5}}\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return "", "", ""
    numero = match.group("num")
    if match.group("indice"):
        numero = f"{numero} {match.group('indice').upper()}"
    kind = french_title(match.group("kind"), particles=False)
    name = french_title(match.group("name"), particles=True)
    return numero, kind, name


def fr_date(value: str | None) -> str:
    if not value:
        return ""
    head = str(value)[:10]
    parts = head.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return str(value)
    year, month, day = parts
    return f"{day}/{month}/{year}"


def external_url(value: str | None) -> str:
    text = (value or "").strip()
    if not text or any(char.isspace() for char in text):
        return ""
    if text.startswith(("http://", "https://")):
        return text
    return "https://" + text


def split_parentheses(value: str | None) -> tuple[str, str]:
    text = " ".join((value or "").split())
    match = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", text)
    if match and match.group(1).strip():
        return match.group(1).strip(), match.group(2).strip()
    return text, ""


def rue_label(type_voie: str | None, libelle_voie: str | None) -> str:
    kind = (type_voie or "").strip()
    name = (libelle_voie or "").strip()
    if kind and name:
        if name.casefold().startswith(kind.casefold()):
            return name
        return f"{kind} {name}"
    return name or kind


def csv_safe(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text
