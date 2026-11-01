"""Busca de pares semelhantes e formação de grupos.

Faixas (distância de Hamming entre assinaturas de 64 bits), calibradas com
imagens artificiais e variantes conhecidas no desenvolvimento do Bloco 05:

* variantes de uma mesma imagem (redução 50%/20%, recompressão JPEG q30, PNG,
  brilho): pHash <= 2 e dHash <= 2; recompressão q10 e contraste: <= 6;
* mesma cena com um objeto deslocado: pHash <= 6, dHash <= 3;
* recorte de 3% das bordas: pHash <= 10, dHash <= 8;
* imagens diferentes (66 pares): pHash >= 18 e dHash >= 20.

Faixas adotadas (pHash e dHash precisam atender ao limite ao mesmo tempo):

* muito alta:  pHash <= 4  e dHash <= 4  e proporção semelhante
* provável:    pHash <= 8  e dHash <= 10 e proporção semelhante
* incerta:     pHash <= 12 e dHash <= 16 (exige revisão humana)

Proporção semelhante: razões largura/altura com diferença relativa <= 5%;
caso contrário o par fica no máximo como "incerta".

Busca de candidatos: dois índices multi-bloco, um para o pHash e outro para
o dHash (3 blocos de 21-22 bits cada). Pelo princípio da casa dos pombos, duas
assinaturas a distância <= 8 coincidem em pelo menos um bloco com distância
<= 2; só esses vizinhos são consultados, sem comparar cada imagem com todas as
demais. A busca é completa para todo par com pHash <= 8 OU dHash <= 8, o que
cobre integralmente as faixas "muito alta" e "provável". Na faixa "incerta",
um par com pHash e dHash ambos acima de 8 pode não ser encontrado (limitação
assumida: um índice exaustivo até 12 bits seria praticamente quadrático).

Formação dos grupos (sem encadeamento): os pares são percorridos do mais
semelhante para o menos semelhante. Um par sem grupo forma um grupo; uma
imagem só entra num grupo existente se for semelhante a TODOS os integrantes
(ligação completa). Grupos nunca são unidos. Assim, A~B e B~C não colocam A e
C no mesmo grupo se A e C não forem semelhantes entre si.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

BAND_EXACT = "exact"
BAND_VERY_HIGH = "very_high"
BAND_PROBABLE = "probable"
BAND_UNCERTAIN = "uncertain"
BAND_ORDER = {BAND_EXACT: 0, BAND_VERY_HIGH: 1, BAND_PROBABLE: 2, BAND_UNCERTAIN: 3}

THRESHOLDS = {
    BAND_VERY_HIGH: (4, 4),
    BAND_PROBABLE: (8, 10),
    BAND_UNCERTAIN: (12, 16),
}
MAX_ASPECT_DIFFERENCE = 0.05
MAX_GROUP_SIZE = 200

# Índice multi-bloco: 3 blocos cobrindo os 64 bits; completo até distância 8.
_BLOCKS = ((0, 22), (22, 21), (43, 21))
INDEX_RADIUS = 8
_BLOCK_RADIUS = INDEX_RADIUS // len(_BLOCKS)  # = 2


@dataclass(frozen=True)
class ImageSig:
    id: int
    phash: int
    dhash: int
    width: int
    height: int


def criteria_description() -> str:
    parts = [f"{band}: pHash<={p}, dHash<={d}" for band, (p, d) in THRESHOLDS.items()]
    return "; ".join(parts) + f"; proporção<={MAX_ASPECT_DIFFERENCE:.0%}"


def _aspect_close(a: ImageSig, b: ImageSig) -> bool:
    ra, rb = a.width / a.height, b.width / b.height
    return abs(ra - rb) / max(ra, rb) <= MAX_ASPECT_DIFFERENCE


def classify_pair(a: ImageSig, b: ImageSig) -> tuple[str, int, int] | None:
    """Faixa do par e distâncias (pHash, dHash), ou None se não for semelhante."""
    dp = (a.phash ^ b.phash).bit_count()
    dd = (a.dhash ^ b.dhash).bit_count()
    aspect_ok = _aspect_close(a, b)
    for band in (BAND_VERY_HIGH, BAND_PROBABLE, BAND_UNCERTAIN):
        max_p, max_d = THRESHOLDS[band]
        if dp <= max_p and dd <= max_d:
            if band != BAND_UNCERTAIN and not aspect_ok:
                band = BAND_UNCERTAIN
            return band, dp, dd
    return None


def _block(value: int, start: int, length: int) -> int:
    return (value >> (64 - start - length)) & ((1 << length) - 1)


def _masks(length: int, radius: int) -> list[int]:
    masks = [0]
    for r in range(1, radius + 1):
        for bits in combinations(range(length), r):
            mask = 0
            for bit in bits:
                mask |= 1 << bit
            masks.append(mask)
    return masks


def _index_pairs(values: Sequence[int], found: set[tuple[int, int]]) -> None:
    tables = [defaultdict(list) for _ in _BLOCKS]
    for index, value in enumerate(values):
        for table, (start, length) in zip(tables, _BLOCKS):
            table[_block(value, start, length)].append(index)
    masks = {length: _masks(length, _BLOCK_RADIUS) for _, length in _BLOCKS}
    for i, value in enumerate(values):
        for table, (start, length) in zip(tables, _BLOCKS):
            block = _block(value, start, length)
            get = table.get
            for mask in masks[length]:
                bucket = get(block ^ mask)
                if bucket:
                    for j in bucket:
                        if j > i:
                            found.add((i, j))


def candidate_pairs(signatures: Sequence[ImageSig]) -> set[tuple[int, int]]:
    """Pares de índices (i < j) com pHash <= 8 ou dHash <= 8 (sempre incluídos),
    além de outros vizinhos de bloco. A confirmação é feita por classify_pair."""
    found: set[tuple[int, int]] = set()
    _index_pairs([s.phash for s in signatures], found)
    _index_pairs([s.dhash for s in signatures], found)
    return found


@dataclass
class Group:
    members: list[int]  # índices em signatures
    band: str


def similar_pairs(signatures: Sequence[ImageSig]) -> list[tuple[str, int, int, int, int]]:
    """Pares semelhantes confirmados: (faixa, dp, dd, i, j)."""
    pairs = []
    for i, j in sorted(candidate_pairs(signatures)):
        result = classify_pair(signatures[i], signatures[j])
        if result is not None:
            band, dp, dd = result
            pairs.append((band, dp, dd, i, j))
    pairs.sort(key=lambda p: (BAND_ORDER[p[0]], p[1], p[2], p[3], p[4]))
    return pairs


def build_groups(signatures: Sequence[ImageSig], pairs) -> list[Group]:
    """Grupos por ligação completa, sem encadeamento e sem união de grupos."""
    group_of: dict[int, int] = {}
    groups: list[Group] = []
    for band, _dp, _dd, i, j in pairs:
        gi, gj = group_of.get(i), group_of.get(j)
        if gi is None and gj is None:
            group_of[i] = group_of[j] = len(groups)
            groups.append(Group([i, j], band))
        elif (gi is None) != (gj is None):
            target, newcomer = (gi, j) if gi is not None else (gj, i)
            group = groups[target]
            if len(group.members) >= MAX_GROUP_SIZE:
                continue
            worst = band
            for member in group.members:
                result = classify_pair(signatures[member], signatures[newcomer])
                if result is None:
                    break
                if BAND_ORDER[result[0]] > BAND_ORDER[worst]:
                    worst = result[0]
            else:
                group.members.append(newcomer)
                group_of[newcomer] = target
                if BAND_ORDER[worst] > BAND_ORDER[group.band]:
                    group.band = worst
    return groups
