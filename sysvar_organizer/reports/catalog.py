"""Montagem dos relatórios de cada tipo de atividade a partir do SQLite.

Usa somente os dados gravados pelos Blocos 02 a 07. Nenhuma análise é
repetida e nenhum arquivo analisado é acessado.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import sqlite3

from sysvar_organizer.engines.organization.categories import category_label
from sysvar_organizer.engines.organization.models import ISSUE_LABELS as ORGANIZATION_ISSUES
from sysvar_organizer.engines.organization.models import ITEM_LABELS
from sysvar_organizer.engines.organization.rules import MODELS
from sysvar_organizer.engines.organization.sensitive import KIND_LABELS as STRUCTURE_LABELS
from sysvar_organizer.operations.models import (
    ACTION_LABELS,
    ENV_REAL,
    REAL_OPERATIONS_BLOCKED_MESSAGE,
    STATE_LABELS,
    display_state,
)
from sysvar_organizer.persistence.history_repository import (
    ACTIVITY_LABELS,
    KIND_COMPARISON,
    KIND_DUPLICATES,
    KIND_ORGANIZATION,
    KIND_STORAGE,
    KIND_VISUAL,
    ActivityFilter,
    activity_query,
    activity_record,
)
from sysvar_organizer.reports.model import Column, Dataset, Report, SummaryItem
from sysvar_organizer.reports.values import (
    BOOL,
    BYTES,
    EPOCH,
    INT,
    SECONDS,
    TIMESTAMP,
    bytes_text,
    count_text,
    timestamp_text,
)

KIND_PLAN = "plan"
KIND_ACTIVITIES = "activities"
REPORT_LABELS = {
    **ACTIVITY_LABELS,
    KIND_PLAN: "Plano de operações",
    KIND_ACTIVITIES: "Histórico de atividades",
}

STATUS_LABELS = {
    "running": "Em andamento",
    "completed": "Concluída",
    "cancelled": "Cancelada",
    "failed": "Falhou",
}
LOCATION_KIND_LABELS = {"folder": "Pasta", "drive": "Unidade"}
LOCATION_STATUS_LABELS = {
    "pending": "Pendente",
    "completed": "Concluído",
    "cancelled": "Cancelado",
    "inaccessible": "Inacessível",
    "failed": "Falhou",
}
ISSUE_LABELS = {
    "access_denied": "Acesso negado",
    "not_found": "Não encontrado",
    "missing": "Não encontrado",
    "read_error": "Erro de leitura",
    "not_a_folder": "Não é uma pasta",
    "symlink_not_followed": "Link simbólico não seguido",
    "junction_not_followed": "Junção não seguida",
    "link_not_followed": "Link não seguido",
    "system_folder_skipped": "Pasta de sistema ignorada",
    "special_entry_skipped": "Item especial ignorado",
    "modified_since_scan": "Modificado depois da análise",
    "changed_during_read": "Alterado durante a leitura",
    "not_regular_file": "Não é um arquivo comum",
    "cloud_only": "Somente na nuvem (não baixado)",
    "unsupported_format": "Formato não suportado",
    "corrupt_image": "Imagem danificada",
    "too_large": "Imagem grande demais",
    "too_small": "Imagem pequena demais",
}
COMPARISON_LABELS = {
    "identical": "Idêntico",
    "identical_renamed": "Idêntico, nome/local diferente",
    "different": "Diferente",
    "only_a": "Exclusivo de A",
    "only_b": "Exclusivo de B",
    "indeterminate": "Indeterminado",
}
FOLDER_LABELS = {"both": "Nos dois lados", "only_a": "Exclusiva de A", "only_b": "Exclusiva de B"}
BAND_LABELS = {
    "exact": "Cópia exata (SHA-256)",
    "very_high": "Similaridade muito alta",
    "probable": "Similaridade provável",
    "uncertain": "Similaridade incerta — revisar",
}
LEVEL_LABELS = {"info": "Informação", "warning": "Atenção", "error": "Erro"}
EXECUTION_LABELS = {"execute": "Execução", "reconcile": "Reconciliação"}
RECOVERY_LABELS = {"reverted": "Revertida", "blocked": "Bloqueada", "failed": "Falhou"}

SNAPSHOT_NOTICE = (
    "O relatório reflete o momento em que a atividade foi realizada. Arquivos indexados "
    "podem ter sido alterados, movidos ou removidos depois; nada foi verificado novamente "
    "no disco para gerar este relatório."
)


class ReportNotFound(LookupError):
    pass


def join_path(folder: str | None, name: str | None) -> str | None:
    if folder is None or name is None:
        return None
    if "\\" in folder or (len(folder) >= 2 and folder[1] == ":"):
        return ntpath.join(folder, name)
    return posixpath.join(folder, name) if folder.startswith("/") else os.path.join(folder, name)


def _label(labels: dict, value):
    return None if value is None else labels.get(value, value)


def _common(kind: str, row, origin: str, *, started="started_at") -> list[SummaryItem]:
    status = row["status"]
    text = STATUS_LABELS.get(status, status)
    if status == "completed" and "has_warnings" in row.keys() and row["has_warnings"]:
        text = "Concluída com avisos"
    return [
        SummaryItem("tipo", "Tipo de atividade", ACTIVITY_LABELS.get(kind, REPORT_LABELS.get(kind))),
        SummaryItem("id", "Identificador", row["id"], INT),
        SummaryItem("origem", "Origem", origin),
        SummaryItem("situacao", "Situação", text),
        SummaryItem("inicio", "Início", row[started], TIMESTAMP),
        SummaryItem("termino", "Término", row["finished_at"], TIMESTAMP),
        SummaryItem("duracao", "Duração", row["elapsed_seconds"], SECONDS),
    ]


def _error(row) -> list[SummaryItem]:
    message = row["error_message"]
    return [SummaryItem("erro", "Mensagem de erro", message)] if message else []


def _fetch(conn: sqlite3.Connection, sql: str, params: tuple):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise ReportNotFound("Registro não encontrado no histórico.")
    return row


# Armazenamento --------------------------------------------------------------

def storage_report(conn: sqlite3.Connection, analysis_id: int) -> Report:
    row = _fetch(conn, "SELECT * FROM analysis_history WHERE id = ?", (analysis_id,))
    issues = conn.execute(
        "SELECT COUNT(*) FROM analysis_issues WHERE analysis_id = ?", (analysis_id,)
    ).fetchone()[0]
    summary = _common(KIND_STORAGE, row, row["target_path"]) + [
        SummaryItem("arquivos", "Arquivos indexados", row["files_count"], INT),
        SummaryItem("pastas", "Pastas", row["folders_count"], INT),
        SummaryItem("volume", "Volume total indexado", row["total_bytes"], BYTES),
        SummaryItem("inacessiveis", "Itens inacessíveis", row["inaccessible_count"], INT),
        SummaryItem("ignorados", "Itens ignorados", row["skipped_count"], INT),
        SummaryItem("ocorrencias", "Ocorrências registradas", issues, INT),
    ] + _error(row)
    locations = Dataset(
        "locais",
        "Locais examinados",
        (
            Column("local", "Local"),
            Column("tipo", "Tipo"),
            Column("situacao", "Situação"),
            Column("arquivos", "Arquivos", INT),
            Column("pastas", "Pastas", INT),
            Column("bytes", "Volume (bytes)", BYTES),
        ),
        "SELECT path, kind, status, files_count, folders_count, total_bytes "
        "FROM analysis_locations WHERE analysis_id = ? ORDER BY id",
        (analysis_id,),
        lambda r: (
            r[0],
            _label(LOCATION_KIND_LABELS, r[1]),
            _label(LOCATION_STATUS_LABELS, r[2]),
            r[3],
            r[4],
            r[5],
        ),
    )
    occurrences = _issues_dataset("analysis_issues", "analysis_id", analysis_id)
    return Report(
        KIND_STORAGE,
        REPORT_LABELS[KIND_STORAGE],
        analysis_id,
        f"Análise de armazenamento nº {analysis_id}",
        f"Todos os registros da análise nº {analysis_id}",
        tuple(summary),
        (SNAPSHOT_NOTICE,),
        (locations, occurrences),
    )


def _issues_dataset(table: str, key: str, source_id: int) -> Dataset:
    return Dataset(
        "ocorrencias",
        "Ocorrências",
        (
            Column("data", "Data", TIMESTAMP),
            Column("tipo", "Tipo"),
            Column("caminho", "Caminho"),
            Column("mensagem", "Mensagem"),
        ),
        f"SELECT occurred_at, kind, path, message FROM {table} WHERE {key} = ? ORDER BY id",
        (source_id,),
        lambda r: (r[0], _label(ISSUE_LABELS, r[1]), r[2], r[3]),
    )


# Duplicados -----------------------------------------------------------------

def duplicates_report(conn: sqlite3.Connection, search_id: int) -> Report:
    row = _fetch(
        conn,
        "SELECT s.*, a.target_path FROM duplicate_searches s "
        "LEFT JOIN analysis_history a ON a.id = s.analysis_id WHERE s.id = ?",
        (search_id,),
    )
    origin = row["target_path"] or f"análise nº {row['analysis_id']}"
    summary = _common(KIND_DUPLICATES, row, origin) + [
        SummaryItem("analise", "Análise de origem", row["analysis_id"], INT),
        SummaryItem("candidatos", "Arquivos candidatos", row["candidates_count"], INT),
        SummaryItem("lidos", "Arquivos lidos para hash", row["read_count"], INT),
        SummaryItem("reaproveitados", "Hashes reaproveitados", row["reused_count"], INT),
        SummaryItem("nao_verificados", "Arquivos não verificados", row["skipped_count"], INT),
        SummaryItem("grupos", "Grupos de duplicados", row["groups_count"], INT),
        SummaryItem("integrantes", "Arquivos integrantes", row["group_files_count"], INT),
        SummaryItem("volume_logico", "Volume lógico dos grupos", row["logical_bytes"], BYTES),
        SummaryItem("excedentes", "Cópias excedentes", row["excess_copies"], INT),
        SummaryItem(
            "recuperavel",
            "Espaço logicamente recuperável (estimativa)",
            row["recoverable_bytes"],
            BYTES,
        ),
        SummaryItem("hard_links", "Hard links (mesmo arquivo físico)", row["hardlink_aliases"], INT),
    ] + _error(row)
    notices = (
        "Espaço recuperável é uma ESTIMATIVA lógica: nenhum arquivo foi excluído e nenhum "
        "espaço foi liberado.",
        "Hard links apontam para o mesmo arquivo físico: aparecem nos grupos, mas não contam "
        "como cópias excedentes nem como espaço recuperável.",
        SNAPSHOT_NOTICE,
    )
    groups = Dataset(
        "grupos",
        "Grupos",
        (
            Column("grupo", "Grupo", INT),
            Column("sha256", "SHA-256"),
            Column("tamanho", "Tamanho de cada arquivo (bytes)", BYTES),
            Column("arquivos", "Arquivos", INT),
            Column("fisicos", "Arquivos físicos distintos", INT),
            Column("excedentes", "Cópias excedentes", INT),
            Column("recuperavel", "Espaço logicamente recuperável (bytes)", BYTES),
        ),
        "SELECT id, sha256, size_bytes, files_count, physical_count, excess_copies, "
        "recoverable_bytes FROM duplicate_groups WHERE search_id = ? "
        "ORDER BY recoverable_bytes DESC, files_count DESC, id",
        (search_id,),
    )
    files = Dataset(
        "arquivos",
        "Arquivos dos grupos",
        (
            Column("grupo", "Grupo", INT),
            Column("nome", "Nome"),
            Column("caminho", "Caminho completo"),
            Column("tamanho", "Tamanho (bytes)", BYTES),
            Column("modificado", "Modificado em (na análise)", EPOCH),
            Column("sha256", "SHA-256"),
            Column("fisico", "Arquivo físico"),
            Column("reaproveitado", "Hash reaproveitado", BOOL),
        ),
        """
        SELECT df.group_id, f.name, d.path, df.size_bytes, f.modified_at, df.sha256,
               df.is_hardlink_alias, df.hash_reused
        FROM duplicate_files df
        JOIN duplicate_groups g ON g.id = df.group_id
        JOIN indexed_files f ON f.id = df.file_id
        JOIN indexed_folders d ON d.id = f.folder_id
        WHERE df.search_id = ? AND df.group_id IS NOT NULL
        ORDER BY g.recoverable_bytes DESC, g.files_count DESC, g.id, d.path, f.name
        """,
        (search_id,),
        lambda r: (
            r[0],
            r[1],
            join_path(r[2], r[1]),
            r[3],
            r[4],
            r[5],
            "Hard link (mesmo arquivo físico de outro integrante)" if r[6] else "Distinto",
            r[7],
        ),
        "SELECT COUNT(*) FROM duplicate_files WHERE search_id = ? AND group_id IS NOT NULL",
    )
    occurrences = _issues_dataset("duplicate_issues", "search_id", search_id)
    return Report(
        KIND_DUPLICATES,
        REPORT_LABELS[KIND_DUPLICATES],
        search_id,
        f"Busca de duplicados nº {search_id}",
        f"Todos os registros da busca nº {search_id}",
        tuple(summary),
        notices,
        (groups, files, occurrences),
    )


# Comparação de pastas -------------------------------------------------------

def _comparison_note(kind, name_differs, folder_differs, same_physical, a_size, b_size, detail):
    physical = "  ·  mesmo arquivo físico (hard link ou sobreposição)" if same_physical else ""
    if kind == "identical":
        return "Mesmo caminho relativo" + physical
    if kind == "identical_renamed":
        if name_differs and folder_differs:
            text = "Nome e subpasta diferentes"
        elif name_differs:
            text = "Nome diferente"
        elif folder_differs:
            text = "Subpasta diferente"
        else:
            text = "Caminho diferente"
        return text + physical
    if kind == "different":
        if a_size is not None and b_size is not None and a_size != b_size:
            return "Mesmo caminho, tamanhos diferentes"
        return "Mesmo caminho, conteúdo diferente (SHA-256)"
    if kind == "only_a":
        return "Sem correspondência em B"
    if kind == "only_b":
        return "Sem correspondência em A"
    return detail or "Não foi possível comparar com segurança"


def comparison_report(conn: sqlite3.Connection, comparison_id: int) -> Report:
    row = _fetch(conn, "SELECT * FROM folder_comparisons WHERE id = ?", (comparison_id,))
    summary = _common(KIND_COMPARISON, row, f"{row['root_a']}  ×  {row['root_b']}") + [
        SummaryItem("origem_a", "Origem A", row["root_a"]),
        SummaryItem("origem_b", "Origem B", row["root_b"]),
        SummaryItem("analise_a", "Análise de A", row["analysis_a_id"], INT),
        SummaryItem("analise_b", "Análise de B", row["analysis_b_id"], INT),
        SummaryItem("arquivos_a", "Arquivos em A", row["files_a"], INT),
        SummaryItem("arquivos_b", "Arquivos em B", row["files_b"], INT),
        SummaryItem("identicos", "Idênticos (correspondências)", row["identical_pairs"], INT),
        SummaryItem(
            "identicos_renomeados",
            "Idênticos com nomes ou locais diferentes (correspondências)",
            row["renamed_pairs"],
            INT,
        ),
        SummaryItem("diferentes", "Diferentes", row["different_count"], INT),
        SummaryItem("exclusivos_a", "Exclusivos de A", row["only_a_count"], INT),
        SummaryItem("exclusivos_b", "Exclusivos de B", row["only_b_count"], INT),
        SummaryItem("indeterminados", "Indeterminados", row["indeterminate_count"], INT),
        SummaryItem(
            "fisicos",
            "Arquivos físicos distintos nas correspondências",
            row["matched_physical"],
            INT,
        ),
        SummaryItem(
            "mesmo_fisico", "Pares do mesmo arquivo físico", row["same_physical_pairs"], INT
        ),
        SummaryItem("conteudos", "Conteúdos distintos nos dois lados", row["shared_contents"], INT),
        SummaryItem("volume_comum", "Volume desses conteúdos", row["shared_bytes"], BYTES),
        SummaryItem("pastas_ambas", "Pastas nos dois lados", row["folders_both"], INT),
        SummaryItem("pastas_a", "Pastas exclusivas de A", row["folders_only_a"], INT),
        SummaryItem("pastas_b", "Pastas exclusivas de B", row["folders_only_b"], INT),
        SummaryItem(
            "equivalencia_conteudo", "Equivalência de conteúdo", row["content_equivalent"], BOOL
        ),
        SummaryItem(
            "equivalencia_estrutura",
            "Equivalência de estrutura",
            row["structure_equivalent"],
            BOOL,
        ),
    ] + _error(row)
    notices = [
        "Os totais de idênticos contam correspondências (pares); um mesmo arquivo pode "
        "participar de mais de uma correspondência. Pares que apontam para o mesmo arquivo "
        "físico (hard link ou sobreposição) estão indicados.",
        "Indeterminados não puderam ser comparados com segurança e não são tratados como "
        "iguais nem como diferentes.",
    ]
    omitted = (row["renamed_pairs"] or 0) - (row["renamed_listed"] or 0)
    if omitted > 0:
        notices.append(
            f"{count_text(omitted)} correspondência(s) de conteúdos com muitas cópias não "
            "foram listadas individualmente na comparação original, mas estão contadas nos "
            "indicadores."
        )
    notices.append(SNAPSHOT_NOTICE)
    side = "{p}.rel_path, d{p}.path, i{p}.name, {p}.size_bytes, {p}.sha256"
    results = Dataset(
        "resultados",
        "Resultados",
        (
            Column("classificacao", "Classificação"),
            Column("relativo_a", "Caminho relativo em A"),
            Column("completo_a", "Caminho completo em A"),
            Column("relativo_b", "Caminho relativo em B"),
            Column("completo_b", "Caminho completo em B"),
            Column("tamanho", "Tamanho (bytes)", BYTES),
            Column("tamanho_a", "Tamanho em A (bytes)", BYTES),
            Column("tamanho_b", "Tamanho em B (bytes)", BYTES),
            Column("sha256_a", "SHA-256 em A"),
            Column("sha256_b", "SHA-256 em B"),
            Column("mesmo_fisico", "Mesmo arquivo físico", BOOL),
            Column("observacao", "Observação"),
        ),
        f"""
        SELECT r.kind, r.size_bytes, r.same_physical, r.name_differs, r.folder_differs,
               r.detail, {side.format(p='a')}, {side.format(p='b')}
        FROM comparison_results r
        LEFT JOIN comparison_files a ON a.id = r.a_entry_id
        LEFT JOIN indexed_files ia ON ia.id = a.file_id
        LEFT JOIN indexed_folders da ON da.id = ia.folder_id
        LEFT JOIN comparison_files b ON b.id = r.b_entry_id
        LEFT JOIN indexed_files ib ON ib.id = b.file_id
        LEFT JOIN indexed_folders db ON db.id = ib.folder_id
        WHERE r.comparison_id = ?
        ORDER BY r.id
        """,
        (comparison_id,),
        lambda r: (
            _label(COMPARISON_LABELS, r[0]),
            r[6],
            join_path(r[7], r[8]),
            r[11],
            join_path(r[12], r[13]),
            r[1],
            r[9],
            r[14],
            r[10],
            r[15],
            r[2],
            _comparison_note(r[0], r[3], r[4], r[2], r[9], r[14], r[5]),
        ),
        "SELECT COUNT(*) FROM comparison_results WHERE comparison_id = ?",
    )
    folders = Dataset(
        "pastas",
        "Comparação estrutural (pastas)",
        (Column("situacao", "Situação"), Column("pasta", "Pasta relativa")),
        "SELECT status, rel_path FROM comparison_folders WHERE comparison_id = ? "
        "ORDER BY rel_path",
        (comparison_id,),
        lambda r: (_label(FOLDER_LABELS, r[0]), r[1]),
    )
    return Report(
        KIND_COMPARISON,
        REPORT_LABELS[KIND_COMPARISON],
        comparison_id,
        f"Comparação de pastas nº {comparison_id}",
        f"Todos os registros da comparação nº {comparison_id}",
        tuple(summary),
        tuple(notices),
        (results, folders),
    )


# Fotografias semelhantes ----------------------------------------------------

_BAND_ORDER = (
    "CASE {c} WHEN 'exact' THEN 0 WHEN 'very_high' THEN 1 WHEN 'probable' THEN 2 ELSE 3 END"
)


def visual_report(conn: sqlite3.Connection, search_id: int) -> Report:
    row = _fetch(conn, "SELECT * FROM visual_searches WHERE id = ?", (search_id,))
    exact_members = conn.execute(
        "SELECT COUNT(*) FROM visual_group_members WHERE search_id = ? AND is_exact_copy = 1",
        (search_id,),
    ).fetchone()[0]
    completed = row["status"] == "completed"
    summary = _common(KIND_VISUAL, row, row["root"]) + [
        SummaryItem("analise", "Análise de origem", row["analysis_id"], INT),
        SummaryItem("candidatas", "Imagens candidatas", row["candidates_count"], INT),
        SummaryItem("calculadas", "Assinaturas calculadas", row["computed_count"], INT),
        SummaryItem("reaproveitadas", "Assinaturas reaproveitadas", row["reused_count"], INT),
        SummaryItem("ignoradas", "Imagens ignoradas", row["skipped_count"], INT),
        SummaryItem("grupos", "Total de grupos", row["groups_count"], INT),
        SummaryItem("envolvidas", "Fotografias envolvidas", row["grouped_images"], INT),
        SummaryItem("grupos_exatos", "Grupos de cópias exatas", row["exact_groups"], INT),
        SummaryItem(
            "copias_exatas",
            "Fotografias confirmadas como cópia exata",
            exact_members if completed else None,
            INT,
        ),
        SummaryItem("muito_alta", "Grupos de similaridade muito alta", row["very_high_groups"], INT),
        SummaryItem("provavel", "Grupos de similaridade provável", row["probable_groups"], INT),
        SummaryItem("incerta", "Grupos de similaridade incerta", row["uncertain_groups"], INT),
        SummaryItem("criterios", "Critérios", row["criteria"]),
    ] + _error(row)
    notices = (
        "A similaridade perceptual (pHash/dHash) NÃO comprova igualdade binária: somente "
        "cópias exatas foram confirmadas por SHA-256.",
        "As assinaturas não foram recalculadas para este relatório. Não há reconhecimento "
        "facial.",
        SNAPSHOT_NOTICE,
    )
    groups = Dataset(
        "grupos",
        "Grupos",
        (
            Column("grupo", "Grupo", INT),
            Column("faixa", "Faixa"),
            Column("fotografias", "Fotografias", INT),
            Column("phash", "Maior distância pHash", INT),
            Column("dhash", "Maior distância dHash", INT),
            Column("exatas", "Cópias exatas no grupo", INT),
        ),
        "SELECT id, band, members_count, max_phash_distance, max_dhash_distance, exact_members "
        f"FROM visual_groups WHERE search_id = ? ORDER BY {_BAND_ORDER.format(c='band')}, id",
        (search_id,),
        lambda r: (r[0], _label(BAND_LABELS, r[1]), r[2], r[3], r[4], r[5]),
    )
    members = Dataset(
        "integrantes",
        "Integrantes dos grupos",
        (
            Column("grupo", "Grupo", INT),
            Column("faixa", "Faixa"),
            Column("posicao", "Posição", INT),
            Column("nome", "Nome"),
            Column("caminho", "Caminho completo"),
            Column("tamanho", "Tamanho (bytes)", BYTES),
            Column("largura", "Largura", INT),
            Column("altura", "Altura", INT),
            Column("formato", "Formato"),
            Column("phash", "Distância pHash", INT),
            Column("dhash", "Distância dHash", INT),
            Column("sha256", "SHA-256"),
            Column("exata", "Cópia exata", BOOL),
            Column("mesmo_fisico", "Mesmo arquivo físico", BOOL),
        ),
        f"""
        SELECT m.group_id, g.band, m.position, f.name, d.path, f.size_bytes, vi.width,
               vi.height, vi.format, m.phash_distance, m.dhash_distance, m.sha256,
               m.is_exact_copy, m.same_physical
        FROM visual_group_members m
        JOIN visual_groups g ON g.id = m.group_id
        JOIN visual_images vi ON vi.id = m.image_id
        JOIN indexed_files f ON f.id = vi.file_id
        JOIN indexed_folders d ON d.id = f.folder_id
        WHERE m.search_id = ?
        ORDER BY {_BAND_ORDER.format(c='g.band')}, m.group_id, m.position
        """,
        (search_id,),
        lambda r: (
            r[0],
            _label(BAND_LABELS, r[1]),
            r[2],
            r[3],
            join_path(r[4], r[3]),
            *r[5:],
        ),
        "SELECT COUNT(*) FROM visual_group_members WHERE search_id = ?",
    )
    skipped = Dataset(
        "ignoradas",
        "Imagens ignoradas",
        (
            Column("nome", "Nome"),
            Column("caminho", "Caminho completo"),
            Column("motivo", "Motivo"),
            Column("mensagem", "Mensagem"),
        ),
        """
        SELECT f.name, d.path, vi.issue_kind, vi.issue_message
        FROM visual_images vi
        JOIN indexed_files f ON f.id = vi.file_id
        JOIN indexed_folders d ON d.id = f.folder_id
        WHERE vi.search_id = ? AND vi.status = 'skipped'
        ORDER BY d.path, f.name
        """,
        (search_id,),
        lambda r: (r[0], join_path(r[1], r[0]), _label(ISSUE_LABELS, r[2]), r[3]),
        "SELECT COUNT(*) FROM visual_images WHERE search_id = ? AND status = 'skipped'",
    )
    return Report(
        KIND_VISUAL,
        REPORT_LABELS[KIND_VISUAL],
        search_id,
        f"Fotografias semelhantes nº {search_id}",
        f"Todos os registros da busca nº {search_id}",
        tuple(summary),
        notices,
        (groups, members, skipped),
    )


# Organização ----------------------------------------------------------------

def _alerts_text(alerts: str | None) -> str | None:
    if not alerts:
        return None
    try:
        values = json.loads(alerts)
    except ValueError:
        return alerts
    if isinstance(values, list):
        return "; ".join(str(v) for v in values) or None
    return str(values)


def _issues_text(codes: str | None) -> str:
    return "; ".join(ORGANIZATION_ISSUES.get(c, c) for c in (codes or "").split(",") if c)


def organization_report(conn: sqlite3.Connection, simulation_id: int) -> Report:
    row = _fetch(conn, "SELECT * FROM organization_simulations WHERE id = ?", (simulation_id,))
    model = MODELS.get(row["model"])
    summary = _common(KIND_ORGANIZATION, row, row["root"]) + [
        SummaryItem("natureza", "Natureza", "SIMULAÇÃO (prévia) — nenhuma operação executada"),
        SummaryItem("modelo", "Modelo de organização", model.label if model else row["model"]),
        SummaryItem("destino", "Destino simulado", row["destination"]),
        SummaryItem("analise", "Análise de origem", row["analysis_id"], INT),
        SummaryItem(
            "excluir_sensiveis", "Estruturas sensíveis excluídas", row["exclude_sensitive"], BOOL
        ),
        SummaryItem("arquivos", "Arquivos classificados", row["files_count"], INT),
        SummaryItem("volume", "Volume lógico envolvido", row["total_bytes"], BYTES),
        SummaryItem("volume_proposta", "Volume da proposta", row["proposal_bytes"], BYTES),
        SummaryItem("validos", "Destino válido", row["ok_count"], INT),
        SummaryItem("no_destino", "Já no destino", row["already_count"], INT),
        SummaryItem("revisao", "Requerem revisão", row["review_count"], INT),
        SummaryItem("conflitos", "Conflitos (requerem decisão)", row["conflict_count"], INT),
        SummaryItem("invalidos", "Caminhos inválidos", row["invalid_count"], INT),
        SummaryItem("excluidos", "Excluídos (estrutura sensível)", row["excluded_count"], INT),
        SummaryItem("pastas", "Pastas propostas", row["folders_count"], INT),
        SummaryItem("estruturas", "Estruturas sensíveis identificadas", row["structures_count"], INT),
        SummaryItem("regra", "Regra de data", row["rule"]),
        SummaryItem("alertas", "Alertas do destino", _alerts_text(row["alerts"])),
    ] + _error(row)
    notices = (
        "SIMULAÇÃO: nenhuma pasta foi criada e nenhum arquivo foi copiado, movido, "
        "renomeado ou excluído. Destinos são apenas propostas.",
        SNAPSHOT_NOTICE,
    )
    items = Dataset(
        "itens",
        "Arquivos classificados",
        (
            Column("nome", "Nome"),
            Column("origem", "Caminho de origem"),
            Column("tamanho", "Tamanho (bytes)", BYTES),
            Column("categoria", "Categoria"),
            Column("situacao", "Situação"),
            Column("destino", "Destino proposto"),
            Column("alertas", "Alertas"),
            Column("justificativa", "Justificativa"),
            Column("estrutura", "Estrutura sensível"),
        ),
        """
        SELECT f.name, d.path, f.size_bytes, o.category, o.status, o.proposed_path, o.issues,
               o.justification, o.structure_path
        FROM organization_items o
        JOIN indexed_files f ON f.id = o.file_id
        JOIN indexed_folders d ON d.id = f.folder_id
        WHERE o.simulation_id = ?
        ORDER BY o.id
        """,
        (simulation_id,),
        lambda r: (
            r[0],
            join_path(r[1], r[0]),
            r[2],
            category_label(r[3]),
            _label(ITEM_LABELS, r[4]),
            r[5],
            _issues_text(r[6]),
            r[7],
            r[8],
        ),
        "SELECT COUNT(*) FROM organization_items WHERE simulation_id = ?",
    )
    categories = Dataset(
        "categorias",
        "Categorias",
        (
            Column("categoria", "Categoria"),
            Column("arquivos", "Arquivos", INT),
            Column("bytes", "Volume (bytes)", BYTES),
        ),
        """
        SELECT o.category, COUNT(*), COALESCE(SUM(f.size_bytes), 0)
        FROM organization_items o JOIN indexed_files f ON f.id = o.file_id
        WHERE o.simulation_id = ?
        GROUP BY o.category ORDER BY COUNT(*) DESC, o.category
        """,
        (simulation_id,),
        lambda r: (category_label(r[0]), r[1], r[2]),
    )
    destinations = Dataset(
        "destinos",
        "Destinos propostos",
        (
            Column("pasta", "Pasta proposta"),
            Column("arquivos", "Arquivos", INT),
            Column("bytes", "Volume (bytes)", BYTES),
        ),
        """
        SELECT o.proposed_folder, COUNT(*), COALESCE(SUM(f.size_bytes), 0)
        FROM organization_items o JOIN indexed_files f ON f.id = o.file_id
        WHERE o.simulation_id = ? AND o.proposed_folder IS NOT NULL
        GROUP BY o.proposed_folder ORDER BY o.proposed_folder
        """,
        (simulation_id,),
    )
    structures = Dataset(
        "estruturas",
        "Estruturas sensíveis",
        (
            Column("caminho", "Caminho"),
            Column("tipo", "Tipo"),
            Column("motivo", "Motivo"),
            Column("arquivos", "Arquivos", INT),
            Column("tratamento", "Tratamento na simulação"),
        ),
        "SELECT path, kind, reason, files_count FROM organization_structures "
        "WHERE simulation_id = ? ORDER BY path",
        (simulation_id,),
        lambda r, excluded=bool(row["exclude_sensitive"]): (
            r[0],
            _label(STRUCTURE_LABELS, r[1]),
            r[2],
            r[3],
            "Excluída da proposta" if excluded else "Mantida para revisão",
        ),
    )
    return Report(
        KIND_ORGANIZATION,
        REPORT_LABELS[KIND_ORGANIZATION],
        simulation_id,
        f"Simulação de organização nº {simulation_id}",
        f"Todos os registros da simulação nº {simulation_id}",
        tuple(summary),
        notices,
        (items, categories, destinations, structures),
    )


# Planos e operações ---------------------------------------------------------

def environment_label(environment: str) -> str:
    return "REAL — execução bloqueada" if environment == ENV_REAL else "TESTE (área isolada)"


def plan_report(conn: sqlite3.Connection, plan_id: int) -> Report:
    row = _fetch(conn, "SELECT * FROM operation_plans WHERE id = ?", (plan_id,))
    environment = row["environment"]
    counts = dict(
        conn.execute(
            "SELECT state, COUNT(*) FROM operation_items WHERE plan_id = ? GROUP BY state",
            (plan_id,),
        ).fetchall()
    )
    approved = conn.execute(
        "SELECT COUNT(*) FROM operation_items WHERE plan_id = ? AND approved = 1", (plan_id,)
    ).fetchone()[0]
    errors = conn.execute(
        "SELECT COUNT(*) FROM operation_events WHERE plan_id = ? AND level = 'error'", (plan_id,)
    ).fetchone()[0]
    recoveries = conn.execute(
        "SELECT COUNT(*) FROM operation_recoveries r JOIN operation_items i ON i.id = r.item_id "
        "WHERE i.plan_id = ?",
        (plan_id,),
    ).fetchone()[0]
    summary = [
        SummaryItem("tipo", "Tipo de atividade", REPORT_LABELS[KIND_PLAN]),
        SummaryItem("plano", "Plano nº", row["id"], INT),
        SummaryItem("ambiente", "Ambiente", environment_label(environment)),
        SummaryItem("operacao", "Tipo de ação do plano", ACTION_LABELS.get(row["operation"])),
        SummaryItem(
            "situacao", "Situação do plano", "Cancelado" if row["status"] == "cancelled" else "Aberto"
        ),
        SummaryItem("criado", "Criado em", row["created_at"], TIMESTAMP),
        SummaryItem("atualizado", "Atualizado em", row["updated_at"], TIMESTAMP),
        SummaryItem("simulacao", "Simulação de origem", row["simulation_id"], INT),
        SummaryItem("destino", "Destino do plano", row["destination"]),
        SummaryItem("acoes", "Ações", row["items_count"], INT),
        SummaryItem("selecionadas", "Ações selecionadas", approved, INT),
    ]
    for state, label in STATE_LABELS.items():
        if counts.get(state):
            summary.append(SummaryItem(f"estado_{state}", f"Estado: {label}", counts[state], INT))
    summary += [
        SummaryItem("erros", "Eventos de erro", errors, INT),
        SummaryItem("reversoes", "Tentativas de reversão", recoveries, INT),
    ]
    notices = []
    if environment == ENV_REAL:
        notices.append(REAL_OPERATIONS_BLOCKED_MESSAGE)
        notices.append(
            "Ações deste plano NÃO foram executadas: estados planejada/validada são apenas "
            "registro de planejamento."
        )
    else:
        notices.append(
            "Plano do ambiente de TESTE: operações físicas ocorreram somente na área "
            "temporária isolada criada pelos testes."
        )
    actions = Dataset(
        "acoes",
        "Ações",
        (
            Column("acao_id", "Ação nº", INT),
            Column("tipo", "Tipo de ação"),
            Column("origem", "Origem"),
            Column("destino", "Destino"),
            Column("estado", "Estado"),
            Column("selecionada", "Selecionada", BOOL),
            Column("reversivel", "Reversível", BOOL),
            Column("bloqueio", "Bloqueio"),
            Column("alertas", "Alertas"),
            Column("dependencias", "Dependências"),
            Column("tamanho", "Tamanho (bytes)", BYTES),
            Column("sha256", "SHA-256 registrado"),
            Column("atualizado", "Atualizado em", TIMESTAMP),
        ),
        "SELECT id, action_type, source_path, destination_path, state, approved, reversible, "
        "block_reason, alerts, dependencies, size_bytes, sha256, updated_at "
        "FROM operation_items WHERE plan_id = ? ORDER BY id",
        (plan_id,),
        lambda r: (
            r[0],
            ACTION_LABELS.get(r[1], r[1]),
            r[2],
            r[3],
            display_state(r[4], environment),
            r[5],
            r[6],
            r[7],
            r[8] or None,
            r[9] or None,
            r[10],
            r[11],
            r[12],
        ),
        "SELECT COUNT(*) FROM operation_items WHERE plan_id = ?",
    )
    events = Dataset(
        "eventos",
        "Eventos do plano",
        (
            Column("data", "Data", TIMESTAMP),
            Column("nivel", "Nível"),
            Column("evento", "Evento"),
            Column("mensagem", "Mensagem"),
            Column("acao_id", "Ação nº", INT),
            Column("arquivo", "Arquivo"),
        ),
        "SELECT e.occurred_at, e.level, e.code, e.message, e.item_id, i.source_path "
        "FROM operation_events e LEFT JOIN operation_items i ON i.id = e.item_id "
        "WHERE e.plan_id = ? ORDER BY e.id",
        (plan_id,),
        lambda r: (r[0], _label(LEVEL_LABELS, r[1]), r[2], r[3], r[4], r[5]),
        "SELECT COUNT(*) FROM operation_events WHERE plan_id = ?",
    )
    executions = Dataset(
        "execucoes",
        "Execuções e reconciliações",
        (
            Column("acao_id", "Ação nº", INT),
            Column("arquivo", "Arquivo"),
            Column("tipo", "Tipo"),
            Column("estado_anterior", "Estado anterior"),
            Column("inicio", "Início", TIMESTAMP),
            Column("termino", "Término", TIMESTAMP),
            Column("resultado", "Resultado"),
            Column("metodo", "Método"),
            Column("mensagem", "Mensagem"),
        ),
        "SELECT x.item_id, i.source_path, x.kind, x.prior_state, x.started_at, x.finished_at, "
        "x.result, x.method, x.message FROM operation_executions x "
        "JOIN operation_items i ON i.id = x.item_id WHERE i.plan_id = ? ORDER BY x.id",
        (plan_id,),
        lambda r: (
            r[0],
            r[1],
            _label(EXECUTION_LABELS, r[2]),
            _label(STATE_LABELS, r[3]),
            r[4],
            r[5],
            _label(STATE_LABELS, r[6]),
            r[7],
            r[8],
        ),
    )
    reversions = Dataset(
        "reversoes",
        "Tentativas de reversão",
        (
            Column("acao_id", "Ação nº", INT),
            Column("arquivo", "Arquivo"),
            Column("solicitada", "Solicitada em", TIMESTAMP),
            Column("termino", "Término", TIMESTAMP),
            Column("resultado", "Resultado"),
            Column("mensagem", "Mensagem"),
        ),
        "SELECT r.item_id, i.source_path, r.requested_at, r.finished_at, r.result, r.message "
        "FROM operation_recoveries r JOIN operation_items i ON i.id = r.item_id "
        "WHERE i.plan_id = ? ORDER BY r.id",
        (plan_id,),
        lambda r: (r[0], r[1], r[2], r[3], _label(RECOVERY_LABELS, r[4]) or "Pendente", r[5]),
    )
    return Report(
        KIND_PLAN,
        REPORT_LABELS[KIND_PLAN],
        plan_id,
        f"Plano de operações nº {plan_id} — {environment_label(environment)}",
        f"Todos os registros do plano nº {plan_id}",
        tuple(summary),
        tuple(notices),
        (actions, events, executions, reversions),
    )


# Lista de atividades --------------------------------------------------------

def activity_summary(kind: str, m1, m2, m3) -> str | None:
    """Resumo curto de uma atividade; ``None`` quando não há números gravados."""
    if kind == KIND_STORAGE and m1 is not None:
        return f"{count_text(m1)} arquivos · {bytes_text(m2).split(' (')[0]}"
    if kind == KIND_DUPLICATES and m1 is not None:
        return f"{count_text(m1)} grupos · {bytes_text(m2).split(' (')[0]} recuperáveis (estimativa)"
    if kind == KIND_COMPARISON and m1 is not None:
        return f"{count_text(m1)} idênticos · {count_text(m2)} diferentes"
    if kind == KIND_VISUAL and m1 is not None:
        return f"{count_text(m1)} grupos · {count_text(m2)} fotografias"
    if kind == KIND_ORGANIZATION and m1 is not None:
        model = MODELS.get(m3)
        return f"{count_text(m1)} arquivos · {model.label if model else m3}"
    return None


def describe_filters(filters: ActivityFilter) -> str:
    parts = []
    if filters.kind:
        parts.append(f"tipo = {ACTIVITY_LABELS.get(filters.kind, filters.kind)}")
    if filters.status:
        parts.append(f"situação = {STATUS_LABELS.get(filters.status, filters.status)}")
    if filters.text.strip():
        parts.append(f'pesquisa = "{filters.text.strip()}"')
    if filters.start:
        parts.append(f"a partir de {timestamp_text(filters.start)}")
    if filters.end:
        parts.append(f"antes de {timestamp_text(filters.end)}")
    parts.append("mais recentes primeiro" if filters.newest_first else "mais antigas primeiro")
    return "; ".join(parts)


def activities_report(conn: sqlite3.Connection, filters: ActivityFilter) -> Report:
    sql, params = activity_query(filters)
    total_all = conn.execute(
        f"SELECT COUNT(*) FROM ({activity_query(ActivityFilter())[0]})"
    ).fetchone()[0]
    selected = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
    scope = (
        f"Somente os registros filtrados: {count_text(selected)} de {count_text(total_all)}"
        if filters.is_filtered
        else f"Todos os registros: {count_text(total_all)}"
    )

    def mapper(row):
        record = activity_record(row)
        status = STATUS_LABELS.get(record.status, record.status)
        if record.status == "completed" and record.has_warnings:
            status = "Concluída com avisos"
        return (
            record.label,
            record.id,
            record.started_at,
            record.finished_at,
            status,
            record.subject,
            activity_summary(record.kind, record.m1, record.m2, record.m3),
            record.elapsed_seconds,
            record.error_message,
        )

    dataset = Dataset(
        "atividades",
        "Atividades somente leitura",
        (
            Column("tipo", "Tipo de atividade"),
            Column("id", "Identificador", INT),
            Column("inicio", "Início", TIMESTAMP),
            Column("termino", "Término", TIMESTAMP),
            Column("situacao", "Situação"),
            Column("origem", "Origem"),
            Column("resumo", "Resumo"),
            Column("duracao", "Duração (s)", SECONDS),
            Column("erro", "Mensagem de erro"),
        ),
        sql,
        params,
        mapper,
    )
    summary = (
        SummaryItem("escopo", "Escopo", scope),
        SummaryItem("filtros", "Filtros", describe_filters(filters)),
        SummaryItem("registros", "Registros exportados", selected, INT),
    )
    return Report(
        KIND_ACTIVITIES,
        REPORT_LABELS[KIND_ACTIVITIES],
        None,
        "Histórico de atividades",
        scope,
        summary,
        (
            "Planos e operações são relatados separadamente, por plano.",
            SNAPSHOT_NOTICE,
        ),
        (dataset,),
    )


BUILDERS = {
    KIND_STORAGE: storage_report,
    KIND_DUPLICATES: duplicates_report,
    KIND_COMPARISON: comparison_report,
    KIND_VISUAL: visual_report,
    KIND_ORGANIZATION: organization_report,
    KIND_PLAN: plan_report,
}


def build_report(conn: sqlite3.Connection, kind: str, source_id: int) -> Report:
    try:
        builder = BUILDERS[kind]
    except KeyError as exc:
        raise ReportNotFound(f"Tipo de relatório desconhecido: {kind}") from exc
    return builder(conn, source_id)
