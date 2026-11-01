"""Tipos de dados da simulação de organização."""

from __future__ import annotations

from dataclasses import dataclass

# Situação de cada arquivo na prévia
ITEM_OK = "ok"  # destino válido
ITEM_ALREADY = "already_in_place"  # já está no caminho proposto
ITEM_REVIEW = "review"  # requer revisão
ITEM_CONFLICT = "conflict"  # requer decisão: outro arquivo teria o mesmo caminho
ITEM_INVALID = "invalid"  # nome/caminho inválido ou longo demais no Windows
ITEM_EXCLUDED = "excluded"  # estrutura sensível excluída da proposta

ITEM_LABELS = {
    ITEM_OK: "Destino válido",
    ITEM_ALREADY: "Já no destino",
    ITEM_REVIEW: "Requer revisão",
    ITEM_CONFLICT: "Requer decisão (conflito)",
    ITEM_INVALID: "Caminho inválido",
    ITEM_EXCLUDED: "Excluído (estrutura sensível)",
}

ISSUE_LABELS = {
    "ambiguous_extension": "extensão ambígua",
    "unknown_extension": "extensão desconhecida",
    "no_extension": "sem extensão",
    "missing_date": "data ausente",
    "invalid_date": "data inválida",
    "invalid_name": "nome inválido ou reservado no Windows",
    "long_path": "caminho proposto excede 260 caracteres",
    "sensitive_structure": "estrutura sensível",
    "already_in_place": "já localizado no destino proposto",
    "name_collision": "outro arquivo receberia o mesmo caminho",
}


@dataclass(frozen=True)
class OrganizationRequest:
    analysis_id: int
    root: str
    model: str  # 'A' | 'B' | 'C'
    destination: str
    exclude_sensitive: bool = True


@dataclass(frozen=True)
class OrganizationProgress:
    total: int
    processed: int
    current_path: str
    elapsed_seconds: float


@dataclass(frozen=True)
class OrganizationOutcome:
    simulation_id: int | None
    status: str
    total: int
    processed: int
    elapsed_seconds: float
    error_message: str | None = None
