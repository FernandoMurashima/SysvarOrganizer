"""Modelos de organização, validação de caminhos do Windows e alertas de destino.

Modelos (extensíveis: novos modelos só precisam declarar as partes da pasta):

* A: Categoria/
* B: Categoria/Ano/
* C: Categoria/Ano/Mês/

A data usada é a de modificação indexada (Bloco 02). Ela NÃO é a data em que
uma fotografia foi tirada; leitura de EXIF não faz parte deste bloco.
"""

from __future__ import annotations

import ntpath
import os
import re
from dataclasses import dataclass
from datetime import datetime

from sysvar_organizer.engines.file_analysis.locations import (
    is_same_or_inside,
    protected_location_warning,
)
from sysvar_organizer.engines.organization.categories import category_folder

DATE_SOURCE = "data de modificação indexada"
NO_DATE_FOLDER = "Sem data"
MIN_VALID_YEAR = 1980
MAX_PATH = 259  # MAX_PATH do Windows (260) menos o terminador

_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class OrganizationModel:
    key: str
    label: str
    description: str
    use_year: bool
    use_month: bool


MODELS: dict[str, OrganizationModel] = {
    "A": OrganizationModel(
        "A", "Modelo A — Por categoria", "Destino/Categoria/", use_year=False, use_month=False
    ),
    "B": OrganizationModel(
        "B", "Modelo B — Por categoria e ano", "Destino/Categoria/Ano/", use_year=True, use_month=False
    ),
    "C": OrganizationModel(
        "C",
        "Modelo C — Por categoria, ano e mês",
        "Destino/Categoria/Ano/Mês/",
        use_year=True,
        use_month=True,
    ),
}


@dataclass(frozen=True)
class DateInfo:
    year: int | None
    month: int | None
    issue: str | None  # 'missing_date' | 'invalid_date' | None


def date_info(modified_at: int | None, now: datetime | None = None) -> DateInfo:
    """Ano/mês locais a partir da data de modificação indexada (segundos Unix)."""
    if modified_at is None:
        return DateInfo(None, None, "missing_date")
    now = now or datetime.now()
    try:
        moment = datetime.fromtimestamp(modified_at)
    except (OverflowError, OSError, ValueError):
        return DateInfo(None, None, "invalid_date")
    if moment.year < MIN_VALID_YEAR or moment.year > now.year + 1:
        return DateInfo(None, None, "invalid_date")
    return DateInfo(moment.year, moment.month, None)


def folder_parts(model: OrganizationModel, category: str, date: DateInfo) -> list[str]:
    parts = [category_folder(category)]
    if model.use_year:
        if date.year is None:
            parts.append(NO_DATE_FOLDER)
        else:
            parts.append(f"{date.year:04d}")
            if model.use_month:
                parts.append(f"{date.month:02d}")
    return parts


def rule_text(model: OrganizationModel) -> str:
    if not model.use_year:
        return f"{model.label}: {model.description}"
    return f"{model.label}: {model.description} ({DATE_SOURCE}; sem data válida → '{NO_DATE_FOLDER}')"


def windows_name_issue(name: str) -> str | None:
    """Motivo pelo qual o nome é inválido/reservado no Windows, ou None."""
    if not name:
        return "Nome vazio."
    if _INVALID_CHARS.search(name):
        return 'Contém caracteres inválidos no Windows (< > : " / \\ | ? * ou de controle).'
    if name.endswith((" ", ".")):
        return "Termina com espaço ou ponto, o que o Windows não aceita."
    stem = name.split(".")[0].rstrip(" ").upper()
    if stem in _RESERVED:
        return f"Nome reservado do Windows ({stem})."
    return None


def destination_alerts(origin: str, destination: str) -> tuple[list[str], bool]:
    """Alertas sobre o destino simulado. Retorna (alertas, destino utilizável).

    Somente análise de texto do caminho e leitura de existência; nada é criado.
    """
    alerts: list[str] = []
    if not destination.strip():
        return ["Informe uma pasta de destino para a simulação."], False
    if not os.path.isabs(destination):
        return ["O destino deve ser um caminho completo (por exemplo, D:\\Organizado)."], False
    for part in _path_components(destination):
        issue = windows_name_issue(part)
        if issue:
            return [f"O destino contém um nome inválido ({part}): {issue}"], False
    if is_same_or_inside(destination, origin) and is_same_or_inside(origin, destination):
        alerts.append(
            "O destino é igual à origem: arquivos já organizados se misturariam aos demais e "
            "conflitos são prováveis."
        )
    elif is_same_or_inside(destination, origin):
        alerts.append(
            "O destino fica dentro da origem: as pastas propostas ficariam no meio dos arquivos "
            "analisados; arquivos que já estiverem no lugar proposto são indicados."
        )
    elif is_same_or_inside(origin, destination):
        alerts.append(
            "A origem fica dentro do destino: uma aplicação futura misturaria a estrutura atual "
            "com a proposta."
        )
    warning = protected_location_warning(destination)
    if warning:
        alerts.append(f"Destino em área protegida do sistema. {warning}")
    origin_drive = ntpath.splitdrive(origin)[0].upper()
    destination_drive = ntpath.splitdrive(destination)[0].upper()
    if origin_drive and destination_drive and origin_drive != destination_drive:
        alerts.append(
            "Origem e destino estão em unidades diferentes: uma aplicação futura exigiria "
            "cópia entre unidades e espaço livre no destino."
        )
    if os.path.lexists(destination) and not os.path.isdir(destination):
        return alerts + ["O destino informado existe e não é uma pasta."], False
    alerts.append(
        "O destino existe e será apenas considerado na simulação."
        if os.path.isdir(destination)
        else "O destino não existe e NÃO será criado: é apenas uma simulação."
    )
    return alerts, True


def _path_components(path: str) -> list[str]:
    drive, rest = os.path.splitdrive(path)
    return [p for p in re.split(r"[\\/]+", rest) if p]
