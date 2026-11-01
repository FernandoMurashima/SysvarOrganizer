"""Identificação conservadora de arquivos associados (unidade lógica de revisão).

Regras (sempre na MESMA pasta; nunca entre pastas diferentes):

* ``foto.CR3.xmp`` (estilo darktable) pertence exatamente a ``foto.CR3``.
* ``foto.xmp`` pertence à fotografia de mesmo nome-base somente se houver
  UMA fotografia RAW com esse nome-base; sem RAW, somente se houver UMA
  imagem comum (JPEG, HEIC, TIFF, PNG) com esse nome-base.
* RAW e JPEG/HEIC com o mesmo nome-base (gravação RAW+JPEG da câmera) formam
  uma unidade.
* Se um ``.xmp`` tiver mais de um candidato possível (por exemplo, ``foto.CR2``
  e ``foto.NEF``), a associação é AMBÍGUA: a unidade é bloqueada para revisão.
* Um ``.xmp`` sem candidato na mesma pasta não é associado a nada.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

RAW_EXTENSIONS = frozenset(
    "cr2 cr3 crw nef nrw arw srf sr2 dng orf rw2 raf pef srw x3f 3fr iiq rwl erf mrw".split()
)
COMPANION_IMAGE_EXTENSIONS = frozenset("jpg jpeg heic heif tif tiff png".split())
SIDECAR_EXTENSIONS = frozenset({"xmp"})


@dataclass
class Unit:
    members: list[int]  # índices dos arquivos na lista de entrada
    primary: int
    ambiguous: bool = False
    reason: str = ""
    names: list[str] = field(default_factory=list)


def _split(name: str) -> tuple[str, str]:
    stem, ext = os.path.splitext(name)
    return stem.casefold(), ext[1:].casefold()


def find_units(files: Iterable[tuple[str, str]]) -> list[Unit]:
    """``files``: (pasta, nome). Retorna apenas unidades com 2+ arquivos."""
    entries = list(files)
    by_folder: dict[str, list[int]] = defaultdict(list)
    for index, (folder, _name) in enumerate(entries):
        by_folder[os.path.normcase(folder)].append(index)

    units: list[Unit] = []
    for indices in by_folder.values():
        by_full_name = {entries[i][1].casefold(): i for i in indices}
        raws: dict[str, list[int]] = defaultdict(list)
        images: dict[str, list[int]] = defaultdict(list)
        sidecars: list[int] = []
        for i in indices:
            stem, ext = _split(entries[i][1])
            if ext in RAW_EXTENSIONS:
                raws[stem].append(i)
            elif ext in COMPANION_IMAGE_EXTENSIONS:
                images[stem].append(i)
            elif ext in SIDECAR_EXTENSIONS:
                sidecars.append(i)

        groups: dict[int, Unit] = {}  # primário -> unidade
        assigned: dict[int, int] = {}

        def unit_for(primary: int) -> Unit:
            if primary not in groups:
                groups[primary] = Unit([primary], primary)
                assigned[primary] = primary
            return groups[primary]

        # RAW + JPEG/HEIC do mesmo disparo
        for stem, raw_list in raws.items():
            if len(raw_list) == 1 and stem in images:
                unit = unit_for(raw_list[0])
                for companion in images[stem]:
                    unit.members.append(companion)
                    assigned[companion] = raw_list[0]

        for sidecar in sidecars:
            name = entries[sidecar][1]
            inner = name[: -len(".xmp")].casefold() if name.casefold().endswith(".xmp") else ""
            if inner in by_full_name and by_full_name[inner] != sidecar:
                target = by_full_name[inner]  # foto.CR3.xmp -> foto.CR3
                owner = assigned.get(target, target)
                unit_for(owner).members.append(sidecar)
                continue
            stem, _ext = _split(name)
            raw_candidates = raws.get(stem, [])
            image_candidates = images.get(stem, [])
            if len(raw_candidates) == 1:
                unit_for(raw_candidates[0]).members.append(sidecar)
            elif len(raw_candidates) > 1 or (not raw_candidates and len(image_candidates) > 1):
                candidates = raw_candidates or image_candidates
                primary = candidates[0]
                unit = unit_for(primary)
                for other in candidates[1:]:
                    if other not in unit.members:
                        unit.members.append(other)
                unit.members.append(sidecar)
                unit.ambiguous = True
                unit.reason = (
                    f"'{name}' pode pertencer a mais de um arquivo: "
                    + ", ".join(entries[c][1] for c in candidates)
                )
            elif len(image_candidates) == 1:
                owner = assigned.get(image_candidates[0], image_candidates[0])
                unit_for(owner).members.append(sidecar)
            # sem candidato: o .xmp não é associado a nada

        for unit in groups.values():
            if len(unit.members) > 1:
                unit.names = [entries[m][1] for m in unit.members]
                units.append(unit)
    return units
