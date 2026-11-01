"""Plano de ações individualizado a partir de uma prévia concluída do Bloco 06.

Regras:

* Nenhuma ação nasce aprovada: aparecer na prévia não é aprovação.
* Destino válido → copiar ou mover (conforme o plano); já no destino ou
  estrutura sensível excluída → manter no local; requer revisão → separar
  para revisão (pasta "Revisão" do destino, preservando a estrutura proposta;
  em planos de cópia o arquivo permanece no local, pois nada sai da origem);
  conflito, caminho inválido ou estrutura sensível mantida → bloqueada.
* Arquivos associados (RAW/XMP/JPEG) formam uma unidade: mesmo tipo de ação e
  mesma pasta de destino do arquivo principal; se um integrante estiver
  bloqueado ou a associação for ambígua, a unidade inteira é bloqueada.
* Resultados de duplicados (Bloco 03) e de semelhança visual (Bloco 05) viram
  apenas ALERTAS informativos, nunca ações.
* Destinos repetidos dentro do plano bloqueiam todas as ações envolvidas.
"""

from __future__ import annotations

import os
import uuid
from collections import defaultdict

from sysvar_organizer.engines.organization.models import ISSUE_LABELS
from sysvar_organizer.operations.associations import find_units
from sysvar_organizer.operations.models import (
    ACTION_COPY,
    ACTION_KEEP,
    ACTION_MOVE,
    ACTION_REVIEW,
    ENV_REAL,
    ENV_TEST,
    PHYSICAL_ACTIONS,
    STATE_BLOCKED,
    STATE_PLANNED,
)
from sysvar_organizer.persistence.operations_store import OperationsStore

REVIEW_FOLDER = "Revisão"
VISUAL_BANDS = {
    "exact": "cópia exata",
    "very_high": "muito alta",
    "probable": "provável",
    "uncertain": "incerta",
}


class PlanningError(ValueError):
    pass


def build_plan(
    store: OperationsStore,
    simulation_id: int,
    operation: str,
    environment: str = ENV_REAL,
    test_root: str | None = None,
    plan_key: str | None = None,
) -> int:
    if operation not in (ACTION_MOVE, ACTION_COPY):
        raise PlanningError("Tipo de operação do plano deve ser mover ou copiar.")
    if environment == ENV_TEST and not test_root:
        raise PlanningError("Planos de teste exigem a raiz da área de testes.")
    info = store.simulation_info(simulation_id)
    if info is None or info["status"] != "completed":
        raise PlanningError("A prévia de organização não existe ou não está concluída.")

    destination = info["destination"]
    rows = store.simulation_rows(simulation_id)
    duplicates = store.duplicate_peers(info["analysis_id"])
    visuals = store.visual_peers(info["analysis_id"])

    items: list[dict] = []
    for row in rows:
        source = os.path.join(row.folder, row.name)
        issues = [code for code in (row.issues or "").split(",") if code]
        alerts = [ISSUE_LABELS.get(code, code) for code in issues]
        peers = duplicates.get(row.file_id)
        if peers:
            alerts.append(
                f"possui {peers} duplicado(s) exato(s) (busca de duplicados) — sem ação automática"
            )
        visual = visuals.get(row.file_id)
        if visual:
            alerts.append(
                f"integra grupo de fotografias semelhantes ({VISUAL_BANDS.get(visual[0], visual[0])}, "
                f"{visual[1]} fotos) — sem ação automática"
            )

        action, target, state, reason = _map_status(row, operation, destination)
        items.append(
            {
                "action_key": uuid.uuid4().hex,
                "unit_key": "",
                "file_id": row.file_id,
                "source_path": source,
                "destination_path": target,
                "action_type": action,
                "justification": row.justification,
                "size_bytes": row.size_bytes,
                "expected_mtime": row.modified_at,
                "sensitive": int(row.structure_path is not None),
                "alerts": "; ".join(alerts),
                "dependencies": "",
                "block_reason": reason,
                "state": state,
                "approved": 0,
                "reversible": 0,
            }
        )
        items[-1]["unit_key"] = items[-1]["action_key"]

    _apply_units(rows, items)
    _block_repeated_destinations(items)
    _propagate_unit_blocks(items)
    key = plan_key or uuid.uuid4().hex
    return store.create_plan(
        key,
        simulation_id,
        info["analysis_id"],
        environment,
        test_root,
        operation,
        destination,
        items,
    )


def _map_status(row, operation: str, destination: str):
    if row.status == "ok":
        return operation, row.proposed_path, STATE_PLANNED, None
    if row.status == "already_in_place":
        return ACTION_KEEP, None, STATE_PLANNED, None
    if row.status == "excluded":
        return ACTION_KEEP, None, STATE_PLANNED, None
    if row.status == "review":
        if row.structure_path is not None:
            return (
                ACTION_KEEP,
                None,
                STATE_BLOCKED,
                "Pertence a uma estrutura sensível: não pode ser movido isoladamente.",
            )
        if operation == ACTION_COPY:
            # Separar para revisão desloca o arquivo; num plano de cópia nada pode
            # sair da origem, então o arquivo permanece no local para revisão humana.
            return ACTION_KEEP, None, STATE_PLANNED, None
        relative = os.path.relpath(row.proposed_path, destination) if row.proposed_path else row.name
        return ACTION_REVIEW, os.path.join(destination, REVIEW_FOLDER, relative), STATE_PLANNED, None
    if row.status == "conflict":
        return (
            operation,
            row.proposed_path,
            STATE_BLOCKED,
            "Conflito: outro arquivo receberia o mesmo caminho. Requer decisão humana.",
        )
    return (
        operation,
        row.proposed_path,
        STATE_BLOCKED,
        "Caminho proposto inválido no Windows (nome reservado/inválido ou longo demais).",
    )


def _apply_units(rows, items: list[dict]) -> None:
    units = find_units((row.folder, row.name) for row in rows)
    for unit in units:
        primary = items[unit.primary]
        members = [items[i] for i in unit.members]
        unit_key = f"unidade-{primary['action_key']}"
        names = ", ".join(unit.names)
        for member in members:
            member["unit_key"] = unit_key
            member["dependencies"] = f"Arquivos associados (mesma unidade): {names}"

        reason = None
        if unit.ambiguous:
            reason = f"Associação ambígua de arquivos: {unit.reason}. Requer revisão."
        elif any(m["state"] == STATE_BLOCKED for m in members):
            blocked = [os.path.basename(m["source_path"]) for m in members if m["state"] == STATE_BLOCKED]
            reason = f"Arquivo associado bloqueado ({', '.join(blocked)}): a unidade não pode ser separada."
        if reason:
            for member in members:
                member["state"] = STATE_BLOCKED
                member["block_reason"] = reason
            continue

        # A unidade segue o arquivo principal: mesma ação e mesma pasta de destino.
        target_folder = (
            os.path.dirname(primary["destination_path"]) if primary["destination_path"] else None
        )
        for member in members:
            member["action_type"] = primary["action_type"]
            member["destination_path"] = (
                os.path.join(target_folder, os.path.basename(member["source_path"]))
                if target_folder
                else None
            )
            if member is not primary:
                member["justification"] += (
                    " Acompanha o arquivo principal da unidade "
                    f"({os.path.basename(primary['source_path'])})."
                )


def _block_repeated_destinations(items: list[dict]) -> None:
    by_target: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        if item["action_type"] in PHYSICAL_ACTIONS and item["destination_path"]:
            by_target[os.path.normcase(os.path.normpath(item["destination_path"]))].append(item)
    for group in by_target.values():
        if len(group) > 1:
            for item in group:
                if item["state"] != STATE_BLOCKED:
                    item["state"] = STATE_BLOCKED
                    item["block_reason"] = "Destino repetido dentro do plano. Requer decisão humana."


def _propagate_unit_blocks(items: list[dict]) -> None:
    """Se qualquer integrante de uma unidade estiver bloqueado, bloqueia todos."""
    by_unit: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_unit[item["unit_key"]].append(item)
    for members in by_unit.values():
        blocked = [m for m in members if m["state"] == STATE_BLOCKED]
        if blocked and len(blocked) < len(members):
            reason = blocked[0]["block_reason"]
            for member in members:
                if member["state"] != STATE_BLOCKED:
                    member["state"] = STATE_BLOCKED
                    member["block_reason"] = f"Arquivo associado bloqueado: {reason}"
