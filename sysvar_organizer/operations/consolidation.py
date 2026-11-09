"""Consolidação de unidades: mover para o destino principal o que só existe na origem.

Regras (decisão aprovada pelo proprietário):

* O DESTINO é a unidade principal e é integralmente preservado: nenhum arquivo
  existente nele é alterado, renomeado ou excluído; só se criam pastas e
  arquivos novos, sempre sem sobrescrita.
* Exclusivo = conteúdo (SHA-256) da origem que NÃO existe no destino,
  independentemente do nome. Conteúdo já presente no destino não é movido.
  Várias cópias idênticas só na origem: apenas uma (escolha determinística)
  é movida; as demais permanecem na origem.
* Movimentação entre unidades = transferência verificada + remoção da
  origem somente depois de conferir o SHA-256 do arquivo gravado no destino
  e confirmar que a origem não mudou.
* Arquivos ignorados, inacessíveis, alterados ou não verificados NUNCA são
  tratados como protegidos no destino: ficam como pendências e impedem
  declarar a origem coberta.
* Não há exclusão em massa, formatação nem remoção de duplicados restantes.

Análise (prévia, somente leitura dos discos):

1. Lê da indexação SQLite os arquivos das duas pastas raiz (sem nova varredura).
2. Filtro por tamanho: só calcula SHA-256 de arquivos da origem cujo tamanho
   existe no destino ou se repete na própria origem, e dos arquivos do
   destino com tamanhos presentes na origem. Arquivos de tamanho único na
   origem e ausente no destino são exclusivos sem leitura.
3. Hashes anteriores (``file_hash_cache``) são reaproveitados somente se
   tamanho, data em ns, volume e identificador do arquivo atuais coincidirem;
   todo arquivo é revalidado contra o índice antes do uso.
4. Classifica cada arquivo e define o destino: mesmo caminho relativo sob a
   pasta base do destino; caminho ocupado → nome alternativo determinístico.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.engines.comparison.hashing import (
    Cancelled,
    FileVerifier,
    Unavailable,
)
from sysvar_organizer.engines.file_analysis.locations import (
    is_same_or_inside,
    protected_location_warning,
)
from sysvar_organizer.engines.file_analysis.scanner import to_filesystem_path
from sysvar_organizer.operations.safety import UnsafePath, is_link_like
from sysvar_organizer.persistence.consolidation_store import (
    CLASS_IN_DESTINATION,
    CLASS_PENDING,
    CLASS_SOURCE_DUPLICATE,
    CLASS_TRANSFER,
    STATE_EXCLUDED,
    BulkHashCache,
    ConsolidationStore,
    ItemRecord,
    JobRecord,
)
from sysvar_organizer.persistence.repositories import utc_now

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024
SPACE_MARGIN = 64 * 1024 * 1024
BATCH = 2000
TEMP_MARK = ".sysvar-cons-"
ISSUE_LABELS = {
    "missing": "Não encontrado (removido ou renomeado depois da análise)",
    "modified_since_scan": "Modificado depois da análise",
    "changed_during_read": "Alterado durante a leitura",
    "access_denied": "Acesso negado",
    "read_error": "Erro de leitura",
    "link_not_followed": "Link ou junção (não seguido)",
    "not_regular_file": "Não é um arquivo comum",
    "cloud_only": "Somente na nuvem (não baixado)",
}

Progress = Callable[[str, int, int], None]


class ConsolidationRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Cancelled(Exception):
    pass


# Raízes ------------------------------------------------------------------------

def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def root_identity(root: str) -> tuple[str, str]:
    try:
        st = os.lstat(to_filesystem_path(root))
    except OSError as exc:
        raise ConsolidationRefused("root_unavailable", f"A pasta {root} não está acessível.") from exc
    if not stat.S_ISDIR(st.st_mode) or is_link_like(st):
        raise ConsolidationRefused("root_invalid", f"{root} não é uma pasta comum.")
    return str(st.st_dev), str(st.st_ino)


def check_roots(source_root: str, dest_root: str, dest_base: str, protected_folders=None) -> None:
    """Regras das pastas raiz da consolidação (antes da análise e da execução)."""
    from sysvar_organizer.operations.authorization import application_folders

    for label, root in (("origem", source_root), ("destino", dest_root)):
        if not root or not os.path.isabs(root):
            raise ConsolidationRefused("root_invalid", f"A {label} deve ter caminho completo.")
        if root.startswith(("\\\\", "//")):
            raise ConsolidationRefused("root_network", f"Pastas de rede não são aceitas ({label}).")
        root_identity(root)
        if _key(os.path.realpath(root)) != _key(root):
            raise ConsolidationRefused("root_link", f"A {label} não pode passar por links ou junções.")
        if protected_location_warning(root):
            raise ConsolidationRefused(
                "root_protected",
                f"A {label} é ou contém pastas protegidas do sistema (unidade do Windows).",
            )
    if is_same_or_inside(source_root, dest_root) or is_same_or_inside(dest_root, source_root):
        raise ConsolidationRefused("roots_overlap", "Origem e destino não podem se sobrepor.")
    if not is_same_or_inside(dest_base, dest_root):
        raise ConsolidationRefused("base_outside", "A pasta base precisa ficar dentro do destino.")
    for folder in application_folders() if protected_folders is None else protected_folders:
        if is_same_or_inside(folder, source_root):
            raise ConsolidationRefused(
                "root_application", "A origem contém pastas do próprio Organizer."
            )


class _Guard:
    """Confina caminhos a uma raiz, sem links/junções em nenhum componente."""

    def __init__(self, root: str) -> None:
        self.root = os.path.normpath(root)

    def resolve(self, path: str) -> str:
        if not path or not os.path.isabs(path):
            raise UnsafePath("relative_path", "Caminho relativo não é permitido.")
        normalized = os.path.normpath(path)
        if not is_same_or_inside(normalized, self.root) or _key(normalized) == _key(self.root):
            raise UnsafePath("outside_root", "Caminho fora da unidade autorizada.")
        relative = os.path.relpath(normalized, self.root)
        current = self.root
        for part in relative.split(os.sep):
            current = os.path.join(current, part)
            try:
                st = os.lstat(to_filesystem_path(current))
            except FileNotFoundError:
                break
            if is_link_like(st):
                raise UnsafePath("link_in_path", "Links simbólicos e junções não são seguidos.")
        if protected_location_warning(normalized):
            raise UnsafePath("protected_location", "Caminho em local protegido do sistema.")
        return normalized


def alternative_path(path: str, label: str, number: int) -> str:
    """Nome alternativo determinístico: ``foto (origem H).jpg``, ``foto (origem H 2).jpg``."""
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    suffix = f" (origem {label})" if number == 1 else f" (origem {label} {number})"
    return os.path.join(folder, f"{stem}{suffix}{ext}")


def source_label(source_root: str) -> str:
    drive = os.path.splitdrive(source_root)[0].rstrip(":")
    if drive and _key(os.path.dirname(source_root)) == _key(source_root):
        return drive.upper()
    return os.path.basename(os.path.normpath(source_root)) or "origem"


# Análise ----------------------------------------------------------------------

def folders_under(conn, analysis_id: int, root: str) -> dict[int, str]:
    rows = conn.execute(
        "SELECT id, path FROM indexed_folders WHERE analysis_id = ?", (analysis_id,)
    ).fetchall()
    return {int(r[0]): r[1] for r in rows if is_same_or_inside(r[1], root)}


def iter_files(conn, analysis_id: int):
    last = 0
    while True:
        rows = conn.execute(
            "SELECT id, folder_id, name, size_bytes, modified_at FROM indexed_files "
            "WHERE analysis_id = ? AND id > ? ORDER BY id LIMIT ?",
            (analysis_id, last, BATCH),
        ).fetchall()
        if not rows:
            return
        yield from rows
        last = int(rows[-1][0])


class ConsolidationAnalyzer:
    """Monta a prévia (somente leitura dos discos)."""

    def __init__(self, store: ConsolidationStore, cancel: threading.Event | None = None,
                 progress: Progress | None = None) -> None:
        self._store = store
        self._cancel = cancel or threading.Event()
        self._progress = progress or (lambda _phase, _done, _total: None)

    def create(self, source_analysis_id: int, source_root: str, dest_analysis_id: int,
               dest_root: str, dest_base: str | None = None) -> int:
        dest_base = os.path.normpath(dest_base or dest_root)
        check_roots(source_root, dest_root, dest_base)
        return self._store.create_job(
            source_analysis_id, os.path.normpath(source_root), dest_analysis_id,
            os.path.normpath(dest_root), dest_base,
        )

    def run(self, job_id: int) -> JobRecord:
        job = self._store.job(job_id)
        conn = self._store.database.open()
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            check_roots(job.source_root, job.dest_root, job.dest_base)
            source_volume, source_index = root_identity(job.source_root)
            dest_volume, dest_index = root_identity(job.dest_root)
            verifier = FileVerifier(BulkHashCache(conn), self._cancel)
            counts = self._analyze(conn, job, verifier)
            conn.commit()
            self._store.set_job_status(
                job_id, ("analyzing",), "planned",
                analyzed_at=utc_now(), source_volume=source_volume, source_dir_index=source_index,
                dest_volume=dest_volume, dest_dir_index=dest_index,
                hashed_count=verifier.read_count, reused_count=verifier.reused,
                bytes_read=verifier.bytes_read, message="Prévia concluída; nenhum arquivo foi alterado.",
                **counts,
            )
            logger.info(
                "Prévia de consolidação concluída: job=%s exclusivos=%d no_destino=%d dup_origem=%d "
                "pendencias=%d conflitos=%d hashes_lidos=%d reaproveitados=%d",
                job_id, counts["transfer_count"], counts["in_dest_count"], counts["source_dup_count"],
                counts["pending_count"], counts["conflict_count"], verifier.read_count, verifier.reused,
            )
        except (_Cancelled, Cancelled):
            conn.rollback()
            self._store.set_job_status(job_id, ("analyzing",), "cancelled",
                                       message="Prévia cancelada; refaça a análise.")
        except ConsolidationRefused as exc:
            conn.rollback()
            self._store.set_job_status(job_id, ("analyzing",), "failed", message=exc.message)
            raise
        finally:
            conn.close()
        return self._store.job(job_id)

    def _tick(self, phase: str, done: int, total: int) -> None:
        if self._cancel.is_set():
            raise _Cancelled
        if done % 500 == 0 or done == total:
            self._progress(phase, done, total)

    def _analyze(self, conn, job: JobRecord, verifier: FileVerifier) -> dict[str, int]:
        now = utc_now()
        source_folders = folders_under(conn, job.source_analysis_id, job.source_root)
        dest_folders = folders_under(conn, job.dest_analysis_id, job.dest_root)
        if not source_folders:
            raise ConsolidationRefused("source_not_indexed", "A origem não faz parte da análise escolhida.")
        if not dest_folders:
            raise ConsolidationRefused("dest_not_indexed", "O destino não faz parte da análise escolhida.")

        conn.execute("CREATE TEMP TABLE cons_dest (path TEXT, size INTEGER, mtime INTEGER)")
        batch = []
        for file_id, folder_id, name, size, mtime in iter_files(conn, job.dest_analysis_id):
            folder = dest_folders.get(folder_id)
            if folder is not None:
                batch.append((os.path.join(folder, name), size, mtime))
            if len(batch) >= BATCH:
                conn.executemany("INSERT INTO cons_dest VALUES (?, ?, ?)", batch)
                batch = []
        conn.executemany("INSERT INTO cons_dest VALUES (?, ?, ?)", batch)
        conn.execute("CREATE INDEX temp.idx_cons_dest_size ON cons_dest (size)")

        batch, total = [], 0
        for file_id, folder_id, name, size, mtime in iter_files(conn, job.source_analysis_id):
            folder = source_folders.get(folder_id)
            if folder is None:
                continue
            full = os.path.join(folder, name)
            rel = os.path.relpath(full, job.source_root)
            batch.append((job.id, file_id, full, rel, os.path.normcase(rel), size, mtime, now))
            total += 1
            if len(batch) >= BATCH:
                self._insert_items(conn, batch)
                batch = []
        self._insert_items(conn, batch)

        # Itens não indexados (inacessíveis/ignorados) da origem: pendências.
        source_issues = 0
        for kind, path, message in conn.execute(
            "SELECT kind, path, message FROM analysis_issues WHERE analysis_id = ?",
            (job.source_analysis_id,),
        ).fetchall():
            if not is_same_or_inside(path, job.source_root):
                continue
            source_issues += 1
            rel = os.path.relpath(path, job.source_root) if path != job.source_root else "."
            conn.execute(
                "INSERT INTO consolidation_items (job_id, source_path, rel_path, rel_key, "
                "classification, state, reason, updated_at) VALUES (?, ?, ?, ?, 'pending', "
                "'pending', ?, ?)",
                (job.id, path, rel, os.path.normcase(rel),
                 f"Não verificado na análise da origem ({kind}){': ' + message if message else ''}",
                 now),
            )
        dest_issues = sum(
            1 for (path,) in conn.execute(
                "SELECT path FROM analysis_issues WHERE analysis_id = ?", (job.dest_analysis_id,)
            ) if is_same_or_inside(path, job.dest_root)
        )
        conn.commit()

        # Tamanhos que exigem SHA-256.
        conn.execute(
            "CREATE TEMP TABLE cons_hsize AS SELECT size_bytes AS size, COUNT(*) AS n "
            "FROM consolidation_items WHERE job_id = ? AND file_id IS NOT NULL GROUP BY size_bytes",
            (job.id,),
        )
        conn.execute("CREATE INDEX temp.idx_cons_hsize ON cons_hsize (size)")
        needed = {
            int(r[0]) for r in conn.execute(
                "SELECT size FROM cons_hsize WHERE n > 1 OR size IN (SELECT size FROM cons_dest)"
            )
        }

        # SHA-256 do destino: somente tamanhos presentes na origem.
        conn.execute("CREATE TEMP TABLE cons_dhash (sha TEXT, path TEXT)")
        dest_filter = "size IN (SELECT size FROM cons_hsize)"
        dest_total = conn.execute(f"SELECT COUNT(*) FROM cons_dest WHERE {dest_filter}").fetchone()[0]
        dest_unverified = done = 0
        for _rowid, path, size, mtime in self._paged(
            conn, f"SELECT rowid, path, size, mtime FROM cons_dest WHERE {dest_filter} AND rowid > ? "
            "ORDER BY rowid LIMIT ?", ()
        ):
            done += 1
            self._tick("Conferindo conteúdo do destino", done, dest_total)
            try:
                st = verifier.validate(path, size, mtime)
                digest, _ = verifier.sha256(path, st, size)
            except Unavailable:
                dest_unverified += 1  # não serve como cobertura
                continue
            conn.execute("INSERT INTO cons_dhash VALUES (?, ?)", (digest, path))
            if done % 200 == 0:
                conn.commit()
        conn.execute("CREATE INDEX temp.idx_cons_dhash ON cons_dhash (sha)")
        conn.commit()

        # Revalida todos os arquivos da origem; SHA-256 só quando necessário.
        done = 0
        for item_id, path, size, mtime in self._paged(
            conn, "SELECT id, source_path, size_bytes, modified_at FROM consolidation_items "
            "WHERE job_id = ? AND file_id IS NOT NULL AND id > ? ORDER BY id LIMIT ?", (job.id,)
        ):
            done += 1
            self._tick("Conferindo arquivos da origem", done, total)
            try:
                st = verifier.validate(path, size, mtime)
                digest = verifier.sha256(path, st, size)[0] if size in needed else None
            except Unavailable as exc:
                conn.execute(
                    "UPDATE consolidation_items SET classification = 'pending', state = 'pending', "
                    "reason = ? WHERE id = ?",
                    (ISSUE_LABELS.get(exc.kind, exc.message), item_id),
                )
                continue
            conn.execute(
                "UPDATE consolidation_items SET sha256 = ?, src_volume = ?, src_index = ?, "
                "src_mtime_ns = ? WHERE id = ?",
                (digest, str(st.st_dev), str(st.st_ino), st.st_mtime_ns, item_id),
            )
            if done % 200 == 0:
                conn.commit()
        conn.commit()

        # Conteúdo já presente (e conferido) no destino: não é movido.
        conn.execute(
            "UPDATE consolidation_items SET classification = 'in_destination', state = 'covered', "
            "match_path = (SELECT path FROM cons_dhash WHERE sha = consolidation_items.sha256 LIMIT 1) "
            "WHERE job_id = ? AND classification IS NULL AND sha256 IN (SELECT sha FROM cons_dhash)",
            (job.id,),
        )
        # Cópias idênticas só na origem: move uma (menor caminho relativo), mantém as demais.
        current, representative = None, None
        for item_id, digest, path in conn.execute(
            "SELECT id, sha256, source_path FROM consolidation_items WHERE job_id = ? AND "
            "classification IS NULL AND sha256 IS NOT NULL ORDER BY sha256, rel_key, id",
            (job.id,),
        ).fetchall():
            if digest != current:
                current, representative = digest, (item_id, path)
                conn.execute(
                    "UPDATE consolidation_items SET classification = 'transfer' WHERE id = ?", (item_id,)
                )
            else:
                conn.execute(
                    "UPDATE consolidation_items SET classification = 'source_duplicate', state = 'kept', "
                    "representative_id = ?, match_path = ? WHERE id = ?",
                    (representative[0], representative[1], item_id),
                )
        conn.execute(
            "UPDATE consolidation_items SET classification = 'transfer' "
            "WHERE job_id = ? AND classification IS NULL",
            (job.id,),
        )
        conn.commit()

        conflicts = self._assign_destinations(conn, job)
        self._carry_exclusions(conn, job)
        totals = {
            (r[0], r[1]): (int(r[2]), int(r[3]))
            for r in conn.execute(
                "SELECT classification, state, COUNT(*), COALESCE(SUM(size_bytes), 0) "
                "FROM consolidation_items WHERE job_id = ? GROUP BY classification, state",
                (job.id,),
            )
        }

        def total_of(classification, states=None):
            pairs = [v for (c, s), v in totals.items() if c == classification and (states is None or s in states)]
            return sum(v[0] for v in pairs), sum(v[1] for v in pairs)

        transfer = total_of(CLASS_TRANSFER, ("planned", STATE_EXCLUDED))
        in_dest = total_of(CLASS_IN_DESTINATION)
        source_dup = total_of(CLASS_SOURCE_DUPLICATE)
        pending = total_of(CLASS_PENDING)
        return {
            "files_total": total,
            "transfer_count": transfer[0],
            "transfer_bytes": transfer[1],
            "in_dest_count": in_dest[0],
            "in_dest_bytes": in_dest[1],
            "source_dup_count": source_dup[0],
            "source_dup_bytes": source_dup[1],
            "conflict_count": conflicts,
            "pending_count": pending[0],
            "source_issues": source_issues,
            "dest_issues": dest_issues,
            "dest_unverified": dest_unverified,
        }

    @staticmethod
    def _carry_exclusions(conn, job: JobRecord) -> None:
        """Uma nova prévia das MESMAS unidades mantém as exclusões feitas pelo
        usuário na prévia anterior (mesmo caminho relativo): regerar a prévia
        nunca devolve à transferência um arquivo que foi desmarcado."""
        previous = conn.execute(
            "SELECT id FROM consolidation_jobs WHERE id < ? AND source_root = ? AND dest_root = ? "
            "AND dest_base = ? AND analyzed_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (job.id, job.source_root, job.dest_root, job.dest_base),
        ).fetchone()
        if previous is None:
            return
        conn.execute(
            "UPDATE consolidation_items SET state = 'excluded', reason = ?, updated_at = ? "
            "WHERE job_id = ? AND classification = 'transfer' AND state = 'planned' AND rel_key IN "
            "(SELECT rel_key FROM consolidation_items WHERE job_id = ? AND state = 'excluded')",
            (f"Excluído da transferência pelo usuário (mantido da consolidação nº {previous[0]}).",
             utc_now(), job.id, previous[0]),
        )
        conn.commit()

    @staticmethod
    def _paged(conn, sql: str, params: tuple):
        """Percorre uma consulta em páginas pela chave (memória limitada)."""
        last = 0
        while True:
            rows = conn.execute(sql, (*params, last, BATCH)).fetchall()
            if not rows:
                return
            yield from rows
            last = rows[-1][0]

    @staticmethod
    def _insert_items(conn, batch) -> None:
        if batch:
            conn.executemany(
                "INSERT INTO consolidation_items (job_id, file_id, source_path, rel_path, rel_key, "
                "size_bytes, modified_at, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'analyzing', ?)",
                batch,
            )

    def _assign_destinations(self, conn, job: JobRecord) -> int:
        """Destino = mesmo caminho relativo sob a pasta base; ocupado → nome alternativo."""
        label = source_label(job.source_root)
        planned: set[str] = set()
        folder_ok: dict[str, bool] = {}
        conflicts = 0
        rows = conn.execute(
            "SELECT id, rel_path FROM consolidation_items WHERE job_id = ? AND classification = 'transfer' "
            "ORDER BY rel_key, id",
            (job.id,),
        ).fetchall()
        for done, (item_id, rel) in enumerate(rows, 1):
            self._tick("Definindo destinos", done, len(rows))
            target = os.path.join(job.dest_base, rel)
            if not self._folder_usable(os.path.dirname(target), job.dest_root, folder_ok):
                conn.execute(
                    "UPDATE consolidation_items SET classification = 'pending', state = 'pending', "
                    "reason = ? WHERE id = ?",
                    ("Conflito de pasta no destino (um arquivo ou link ocupa o nome da pasta); "
                     "requer revisão.", item_id),
                )
                continue
            candidate, number = target, 0
            while _key(candidate) in planned or os.path.lexists(to_filesystem_path(candidate)):
                number += 1
                candidate = alternative_path(target, label, number)
            planned.add(_key(candidate))
            renamed = int(number > 0)
            conflicts += renamed
            conn.execute(
                "UPDATE consolidation_items SET dest_path = ?, renamed = ?, state = 'planned' WHERE id = ?",
                (candidate, renamed, item_id),
            )
            if done % 500 == 0:
                conn.commit()
        conn.commit()
        return conflicts

    @staticmethod
    def _folder_usable(folder: str, root: str, cache: dict[str, bool]) -> bool:
        """A pasta pode existir ou ser criada sem esbarrar em arquivo/link do destino."""
        chain = []
        current = folder
        while _key(current) != _key(root) and is_same_or_inside(current, root):
            if current in cache:
                break
            chain.append(current)
            current = os.path.dirname(current)
        result = cache.get(current, True)
        for path in reversed(chain):
            if result:
                try:
                    st = os.lstat(to_filesystem_path(path))
                    result = stat.S_ISDIR(st.st_mode) and not is_link_like(st)
                except FileNotFoundError:
                    result = True
            cache[path] = result
        return cache.get(folder, result)


# Execução ---------------------------------------------------------------------

@dataclass
class ConsolidationOutcome:
    job_id: int
    status: str
    message: str
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(
        ("transferred", "covered", "blocked", "failed", "reconciled"), 0))


# Exclusão individual ---------------------------------------------------------

EXCLUDABLE_STATES = ("planned", "blocked", "failed")


@dataclass
class ExclusionResult:
    changed: list[int] = field(default_factory=list)
    refused: list[tuple[int, str]] = field(default_factory=list)


def _editable_job(store: ConsolidationStore, job_id: int) -> JobRecord:
    job = store.job(job_id)
    if job is None:
        raise ConsolidationRefused("job_missing", "Consolidação inexistente.")
    if job.analyzed_at is None or job.status in ("analyzing", "running"):
        raise ConsolidationRefused(
            "job_busy", "A seleção só pode ser alterada com a prévia concluída e sem execução em andamento."
        )
    return job


def item_for_path(store: ConsolidationStore, job_id: int, path: str) -> ItemRecord | None:
    """Item da consolidação para um caminho completo da origem (ex.: H:\\pasta\\a.bak)."""
    job = store.job(job_id)
    path = os.path.normpath(path.strip().strip('"'))
    if job is None or not os.path.isabs(path) or not is_same_or_inside(path, job.source_root):
        return None
    return store.item_by_key(job_id, os.path.normcase(os.path.relpath(path, job.source_root)))


def exclude_items(store: ConsolidationStore, job_id: int, item_ids) -> ExclusionResult:
    """Desmarca arquivos da transferência. O item passa ao estado ``excluded``,
    que o executor nunca processa: o arquivo não é copiado, movido, renomeado
    nem excluído. Troca de estado por comparação: se a execução já tiver
    reservado o item, a exclusão é recusada (nada fica pela metade)."""
    _editable_job(store, job_id)
    result = ExclusionResult()
    for item_id in dict.fromkeys(int(i) for i in item_ids):
        item = store.item(item_id)
        if item is None or item.job_id != job_id:
            result.refused.append((item_id, "Item não pertence a esta consolidação."))
            continue
        if item.state == STATE_EXCLUDED:
            result.refused.append((item_id, "Já estava excluído da transferência."))
            continue
        if item.classification != CLASS_TRANSFER:
            result.refused.append((item_id, "Este arquivo não seria movido (não é exclusivo da origem)."))
            continue
        if item.state not in EXCLUDABLE_STATES:
            reason = {
                "transferred": "Já foi transferido e conferido; não pode mais ser excluído.",
                "covered": "Já está coberto no destino e será mantido na origem.",
                "interrupted": "Transferência interrompida: execute a retomada antes de excluir.",
                "running": "Em transferência neste momento.",
            }.get(item.state, f"Estado atual não permite exclusão ({item.state}).")
            result.refused.append((item_id, reason))
            continue
        note = f"Excluído da transferência pelo usuário em {utc_now()}."
        if store.transition(item_id, EXCLUDABLE_STATES, STATE_EXCLUDED, reason=note):
            store.add_event(job_id, item_id, "info", "excluded", f"Excluído da transferência: {item.source_path}")
            result.changed.append(item_id)
        else:
            result.refused.append((item_id, "O estado mudou durante a operação; nada foi alterado."))
    if result.changed:
        logger.info("Consolidação %s: %d arquivo(s) excluído(s) da transferência", job_id, len(result.changed))
    return result


def restore_items(store: ConsolidationStore, job_id: int, item_ids) -> ExclusionResult:
    """Devolve à transferência arquivos excluídos (revalidados na execução)."""
    _editable_job(store, job_id)
    result = ExclusionResult()
    for item_id in dict.fromkeys(int(i) for i in item_ids):
        item = store.item(item_id)
        if item is None or item.job_id != job_id or item.state != STATE_EXCLUDED:
            result.refused.append((item_id, "Não está excluído da transferência."))
            continue
        if store.transition(item_id, (STATE_EXCLUDED,), "planned", reason=None):
            store.add_event(job_id, item_id, "info", "restored", f"Devolvido à transferência: {item.source_path}")
            result.changed.append(item_id)
    return result


def execution_token(job: JobRecord) -> str:
    """Impressão digital da prévia confirmada pelo usuário."""
    text = f"{job.id}|{job.analyzed_at}|{job.transfer_count}|{job.transfer_bytes}|{job.dest_base}"
    return hashlib.sha256(text.encode()).hexdigest()


def _refusal(exc: OSError) -> ConsolidationRefused | None:
    if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or getattr(exc, "winerror", 0) in (39, 112):
        return ConsolidationRefused("no_space", "Espaço insuficiente no destino.")
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        return ConsolidationRefused("permission", "Sem permissão para concluir a transferência.")
    return None


def sha256_file(path: str, cancel: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with open(to_filesystem_path(path), "rb", buffering=0) as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise _Cancelled
            chunk = handle.read(CHUNK)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


class ConsolidationExecutor:
    """Transfere os exclusivos da prévia: cópia verificada + remoção segura da origem."""

    def __init__(self, store: ConsolidationStore, cancel: threading.Event | None = None,
                 progress: Progress | None = None, fault: Callable[[str], None] | None = None) -> None:
        self._store = store
        self._cancel = cancel or threading.Event()
        self._progress = progress or (lambda _phase, _done, _total: None)
        self._fault = fault or (lambda _point: None)
        self._created: tuple[str, tuple[int, int]] | None = None

    # Autorização -----------------------------------------------------------

    def authorize(self, job_id: int) -> JobRecord:
        """Autorização explícita das DUAS unidades (registrada com a identidade física)."""
        job = self._ready_job(job_id, require_authorization=False)
        self._check_identity(job)
        self._store.update_job(job_id, authorized_at=utc_now())
        self._store.add_event(job_id, None, "info", "authorized",
                              f"Unidades autorizadas: origem {job.source_root} → destino {job.dest_root}.")
        return self._store.job(job_id)

    def _ready_job(self, job_id: int, require_authorization: bool = True) -> JobRecord:
        job = self._store.job(job_id)
        if job is None:
            raise ConsolidationRefused("job_missing", "Consolidação inexistente.")
        if job.analyzed_at is None or job.status not in ("planned", "partial", "interrupted", "cancelled", "completed"):
            raise ConsolidationRefused("not_planned", "A prévia não foi concluída; refaça a análise.")
        if require_authorization and job.authorized_at is None:
            raise ConsolidationRefused("not_authorized", "As unidades ainda não foram autorizadas.")
        check_roots(job.source_root, job.dest_root, job.dest_base)
        return job

    @staticmethod
    def _check_identity(job: JobRecord) -> None:
        if root_identity(job.source_root) != (job.source_volume, job.source_dir_index):
            raise ConsolidationRefused("source_changed", "A unidade de origem não é a mesma da prévia.")
        if root_identity(job.dest_root) != (job.dest_volume, job.dest_dir_index):
            raise ConsolidationRefused("dest_changed", "A unidade de destino não é a mesma da prévia.")

    # Lote ------------------------------------------------------------------

    def execute(self, job_id: int, token: str) -> ConsolidationOutcome:
        job = self._ready_job(job_id)
        if token != execution_token(job):
            raise ConsolidationRefused("confirmation_mismatch", "A prévia mudou depois da confirmação.")
        self._check_identity(job)
        self._store.set_job_status(job_id, ("planned", "partial", "interrupted", "cancelled", "completed"),
                                   "running", message="Consolidação em andamento.")
        outcome = ConsolidationOutcome(job_id, "running", "")
        source, dest = _Guard(job.source_root), _Guard(job.dest_root)
        try:
            for item in list(self._store.iter_items(job_id, CLASS_TRANSFER, "interrupted")):
                outcome.counts["reconciled"] += 1
                self._reconcile(job, item, source, dest)
            total = self._store.count_items(job_id, CLASS_TRANSFER, "planned")
            done = 0
            stop_reason = None
            for item in self._store.iter_items(job_id, CLASS_TRANSFER, "planned"):
                if self._cancel.is_set():
                    stop_reason = "cancelled"
                    break
                self._progress("Transferindo", done, total)
                free = _free_space(job.dest_root)
                if free is not None and free < item.size_bytes + SPACE_MARGIN:
                    self._store.add_event(job_id, item.id, "warning", "no_space",
                                          "Espaço livre insuficiente no destino; consolidação pausada.")
                    stop_reason = "no_space"
                    break
                result = self._transfer(job, item, source, dest)
                outcome.counts[result] = outcome.counts.get(result, 0) + 1
                done += 1
                if result == "stop_no_space":
                    stop_reason = "no_space"
                    break
                if result == "cancelled":
                    stop_reason = "cancelled"
                    break
            self._progress("Transferindo", done, total)
        except DatabaseError:
            logger.error("Banco inacessível durante a consolidação (job=%s)", job_id)
            raise
        remaining = self._store.count_items(job_id, CLASS_TRANSFER, ("planned", "running", "interrupted"))
        problems = self._store.count_items(job_id, CLASS_TRANSFER, ("blocked", "failed"))
        status = "cancelled" if stop_reason == "cancelled" else ("partial" if remaining or problems else "completed")
        message = {
            "cancelled": "Consolidação cancelada com segurança; arquivos não concluídos permanecem na origem.",
            "no_space": "Consolidação pausada por falta de espaço no destino.",
        }.get(stop_reason) or (
            f"Consolidação encerrada com {problems} arquivo(s) bloqueado(s) ou com falha, mantidos na origem. "
            "Consulte o relatório de conferência." if problems
            else "Consolidação concluída. Consulte o relatório de conferência."
        )
        excluded = self._store.count_items(job_id, CLASS_TRANSFER, STATE_EXCLUDED)
        if excluded:
            message += f" {excluded} arquivo(s) excluído(s) pelo usuário permanecem intocados na origem."
        self._store.set_job_status(job_id, ("running",), status, message=message)
        outcome.status, outcome.message = status, message
        logger.info("Consolidação encerrada: job=%s estado=%s contagens=%s", job_id, status, outcome.counts)
        return outcome

    def retry_failed(self, job_id: int) -> int:
        """Devolve falhas e bloqueios de transferência à fila (nova validação no disco)."""
        return self._store.reset_items(job_id, CLASS_TRANSFER, ("failed", "blocked"))

    # Um arquivo ------------------------------------------------------------

    def _transfer(self, job: JobRecord, item: ItemRecord, source_root: _Guard, dest_root: _Guard) -> str:
        if not self._store.transition(item.id, ("planned",), "running"):
            return "skipped"
        temp = None
        self._created = None
        source_path = None
        try:
            source_path = source_root.resolve(item.source_path)
            st = FileVerifier(_NoCache(), self._cancel).validate(source_path, item.size_bytes, item.modified_at)
            if item.src_index and (str(st.st_dev), str(st.st_ino)) != (item.src_volume, item.src_index):
                raise ConsolidationRefused("replaced", "O arquivo foi substituído depois da prévia.")
            target = dest_root.resolve(item.dest_path)
            target, covered = self._free_target(job, item, target, source_path, dest_root)
            if covered:
                self._store.transition(item.id, ("running",), "covered", match_path=covered,
                                       reason="Conteúdo idêntico encontrado no destino; origem mantida.")
                self._store.add_event(job.id, item.id, "info", "covered", f"Conteúdo já existente em {covered}.")
                return "covered"
            self._make_parents(target, dest_root)
            temp = os.path.join(os.path.dirname(target), f".{os.path.basename(target)}{TEMP_MARK}{item.id}.tmp")
            digest = self._copy(source_path, temp, st)
            if item.sha256 and digest != item.sha256:
                raise ConsolidationRefused("content_changed", "O conteúdo da origem mudou desde a prévia.")
            _finalize_without_overwrite(temp, target)
            temp = None
            created = os.lstat(to_filesystem_path(target))
            self._created = (target, (created.st_dev, created.st_ino))
            # Destino publicado: identidade e hash registrados ANTES de conferir e remover a origem.
            self._store.transition(
                item.id, ("running",), "running", dest_path=target, sha256=digest,
                dst_volume=str(created.st_dev), dst_index=str(created.st_ino),
                renamed=int(item.renamed or _key(target) != _key(item.dest_path or "")),
            )
            self._fault("after_publish")
            if sha256_file(target) != digest:
                raise RuntimeError("Conferência do destino falhou (SHA-256 diferente).")
            current = os.lstat(to_filesystem_path(source_path))
            if (current.st_size, current.st_mtime_ns, current.st_ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
                raise ConsolidationRefused("source_changed", "A origem mudou durante a transferência.")
            self._fault("before_remove")
            os.unlink(to_filesystem_path(source_path))
            self._created = None
            if os.path.lexists(to_filesystem_path(source_path)):
                raise RuntimeError("A origem ainda existe após a remoção.")
            self._store.transition(item.id, ("running",), "transferred", reason=None)
            return "transferred"
        except DatabaseError:
            _remove_quietly(temp)
            raise
        except (_Cancelled, Cancelled):
            note = self._abort(temp, source_path, item)
            self._store.transition(item.id, ("running",), "planned", reason="Cancelada antes da conclusão." + note)
            return "cancelled"
        except (OSError, ConsolidationRefused, UnsafePath, Unavailable) as exc:
            note = self._abort(temp, source_path, item)
            refused = _refusal(exc) if isinstance(exc, OSError) else exc
            if refused is None:
                return self._failed(job, item, exc, note)
            message = getattr(refused, "message", str(refused)) + note
            self._store.transition(item.id, ("running",), "blocked", reason=message)
            self._store.add_event(job.id, item.id, "warning", getattr(refused, "code", getattr(refused, "kind", "blocked")), message)
            return "stop_no_space" if getattr(refused, "code", "") == "no_space" else "blocked"
        except Exception as exc:  # noqa: BLE001
            note = self._abort(temp, source_path, item)
            return self._failed(job, item, exc, note)

    def _failed(self, job: JobRecord, item: ItemRecord, exc: BaseException, note: str) -> str:
        message = f"Falha: {exc}. A origem foi preservada.{note}"
        logger.error("Falha na consolidação (job=%s, item=%s, tipo=%s)", job.id, item.id, type(exc).__name__)
        self._store.transition(item.id, ("running",), "failed", reason=message)
        self._store.add_event(job.id, item.id, "error", "failed", message)
        return "failed"

    def _free_target(self, job, item, target, source_path, dest_root):
        """Nunca sobrescreve: destino ocupado com o MESMO conteúdo → coberto;
        com outro conteúdo → próximo nome alternativo determinístico."""
        label = source_label(job.source_root)
        base = os.path.join(job.dest_base, item.rel_path)
        number = 0
        candidate = target
        source_hash = None
        while os.path.lexists(to_filesystem_path(candidate)):
            st = os.lstat(to_filesystem_path(candidate))
            if stat.S_ISREG(st.st_mode) and not is_link_like(st) and st.st_size == item.size_bytes:
                source_hash = source_hash or item.sha256 or sha256_file(source_path, self._cancel)
                if sha256_file(candidate, self._cancel) == source_hash:
                    return candidate, candidate
            number += 1
            candidate = dest_root.resolve(alternative_path(base, label, number))
        return candidate, None

    def _copy(self, source: str, temp: str, st: os.stat_result) -> str:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(to_filesystem_path(temp), flags, 0o600)
        digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as out, open(to_filesystem_path(source), "rb", buffering=0) as src:
                before = os.fstat(src.fileno())
                if (before.st_size, before.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                    raise ConsolidationRefused("source_changed", "A origem mudou antes da cópia.")
                while True:
                    if self._cancel.is_set():
                        raise _Cancelled
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
                after = os.fstat(src.fileno())
            if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                raise ConsolidationRefused("source_changed", "A origem mudou durante a cópia.")
            self._fault("after_temp_write")
            import shutil

            shutil.copystat(to_filesystem_path(source), to_filesystem_path(temp))
        except BaseException:
            _remove_quietly(temp)
            raise
        return digest.hexdigest()

    def _make_parents(self, target: str, dest_root: _Guard) -> None:
        missing = []
        current = os.path.dirname(target)
        while not os.path.lexists(to_filesystem_path(current)):
            missing.append(current)
            current = os.path.dirname(current)
        for folder in reversed(missing):
            dest_root.resolve(folder)
            os.mkdir(to_filesystem_path(folder))
        dest_root.resolve(target)

    def _abort(self, temp: str | None, source_path: str | None, item: ItemRecord) -> str:
        """Descarta o temporário; se a origem segue intacta, desfaz o arquivo que
        ESTA execução criou no destino (nunca toca em arquivo preexistente)."""
        _remove_quietly(temp)
        created, self._created = self._created, None
        if created is None or source_path is None:
            return ""
        target, identity = created
        try:
            src = os.lstat(to_filesystem_path(source_path))
            dst = os.lstat(to_filesystem_path(target))
        except OSError:
            return ""
        if (dst.st_dev, dst.st_ino) != identity or src.st_size != item.size_bytes:
            return " Arquivo no destino preservado para revisão."
        try:
            os.unlink(to_filesystem_path(target))
        except OSError:
            return " Não foi possível desfazer o arquivo criado no destino; revisão manual."
        return " O arquivo criado nesta execução no destino foi desfeito; a origem permanece intacta."

    # Retomada ----------------------------------------------------------------

    def _reconcile(self, job: JobRecord, item: ItemRecord, source_root: _Guard, dest_root: _Guard) -> str:
        """Ação interrompida: decide pelo estado físico, sem duplicar nem sobrescrever."""
        source = item.source_path
        target = item.dest_path
        if target:
            for name in os.listdir(to_filesystem_path(os.path.dirname(target))) if os.path.isdir(
                to_filesystem_path(os.path.dirname(target))
            ) else []:
                if name.endswith(f"{TEMP_MARK}{item.id}.tmp"):
                    temp = os.path.join(os.path.dirname(target), name)
                    dest_root.resolve(temp)
                    _remove_quietly(temp)
        src_exists = os.path.lexists(to_filesystem_path(source))
        dst_exists = bool(target) and os.path.lexists(to_filesystem_path(target))
        ours = False
        if dst_exists and item.dst_index:
            st = os.lstat(to_filesystem_path(target))
            ours = (str(st.st_dev), str(st.st_ino)) == (item.dst_volume, item.dst_index)
        dst_ok = dst_exists and item.sha256 is not None and sha256_file(target) == item.sha256
        if ours and dst_ok:
            if not src_exists:
                state, note = "transferred", "Retomada: transferência encontrada concluída e conferida."
            elif sha256_file(source) == item.sha256:
                source_root.resolve(source)
                os.unlink(to_filesystem_path(source))
                state, note = "transferred", "Retomada: destino conferido; origem removida após conferência."
            else:
                state, note = "failed", "Origem alterada após a cópia; destino e origem mantidos. Revisão manual."
        elif dst_exists and not ours:
            if src_exists and item.sha256 and dst_ok:
                state, note = "covered", "Conteúdo idêntico encontrado no destino; origem mantida."
            else:
                state, note = "failed", "Destino ocupado por arquivo não confirmado; nada removido. Revisão manual."
        elif src_exists:
            state, note = "planned", "Retomada: transferência não concluída; será refeita."
        else:
            state, note = "failed", "Origem e destino ausentes após interrupção. Revisão manual."
        self._store.transition(item.id, ("interrupted",), state, reason=note if state != "transferred" else None)
        self._store.add_event(job.id, item.id, "info" if state != "failed" else "warning", "reconciled", note)
        return state


class _NoCache:
    def cached_hash(self, *args):
        return None

    def save_hash(self, *args):
        return None


def _finalize_without_overwrite(temp: str, target: str) -> None:
    temp_fs, target_fs = to_filesystem_path(temp), to_filesystem_path(target)
    try:
        os.link(temp_fs, target_fs)
    except FileExistsError as exc:
        raise ConsolidationRefused("destination_occupied", "O destino foi ocupado.") from exc
    except OSError:
        if os.name == "nt":
            os.rename(temp_fs, target_fs)  # no Windows, falha se o destino existir
            return
        if os.path.lexists(target_fs):
            raise ConsolidationRefused("destination_occupied", "O destino foi ocupado.") from None
        os.rename(temp_fs, target_fs)
        return
    os.unlink(temp_fs)


def _remove_quietly(path: str | None) -> None:
    if path and os.path.lexists(to_filesystem_path(path)):
        try:
            os.unlink(to_filesystem_path(path))
        except OSError:
            logger.warning("Não foi possível remover o temporário da consolidação")


def _free_space(root: str) -> int | None:
    import shutil

    try:
        return shutil.disk_usage(to_filesystem_path(root)).free
    except OSError:
        return None


# Cobertura ----------------------------------------------------------------------

@dataclass(frozen=True)
class Coverage:
    transferred: int
    in_destination: int
    duplicates_covered: int
    uncovered: int
    source_issues: int
    proven: bool
    reasons: tuple[str, ...]


def coverage(store: ConsolidationStore, job_id: int) -> Coverage:
    """A origem só pode ser considerada coberta se TODO arquivo indexado tiver
    cópia conferida no destino e não houver itens não verificados."""
    job = store.job(job_id)
    summary = store.summary(job_id)

    def count(classification, states=None):
        return sum(v[0] for (c, s), v in summary.items() if c == classification and (states is None or s in states))

    transferred = count(CLASS_TRANSFER, ("transferred",)) + count(CLASS_TRANSFER, ("covered",))
    in_dest = count(CLASS_IN_DESTINATION)
    with store.database.connection() as conn:
        dup_covered = int(conn.execute(
            "SELECT COUNT(*) FROM consolidation_items d JOIN consolidation_items r "
            "ON r.id = d.representative_id WHERE d.job_id = ? AND d.classification = 'source_duplicate' "
            "AND r.state IN ('transferred', 'covered')",
            (job_id,),
        ).fetchone()[0])
    dup_total = count(CLASS_SOURCE_DUPLICATE)
    excluded = count(CLASS_TRANSFER, (STATE_EXCLUDED,))
    transfer_open = count(CLASS_TRANSFER) - transferred - excluded
    pending = count(CLASS_PENDING)
    uncovered = transfer_open + excluded + pending + (dup_total - dup_covered)
    reasons = []
    if excluded:
        reasons.append(f"{excluded} arquivo(s) excluído(s) da transferência pelo usuário (existem só na origem)")
    if transfer_open:
        reasons.append(f"{transfer_open} arquivo(s) exclusivo(s) ainda não transferido(s)")
    if dup_total - dup_covered:
        reasons.append(f"{dup_total - dup_covered} duplicado(s) da origem cuja cópia ainda não está no destino")
    if pending:
        reasons.append(f"{pending} pendência(s) não verificada(s)")
    if job.source_issues:
        reasons.append(f"{job.source_issues} item(ns) da origem não verificado(s) na análise")
    return Coverage(transferred, in_dest, dup_covered, uncovered, job.source_issues,
                    uncovered == 0 and job.source_issues == 0, tuple(reasons))
