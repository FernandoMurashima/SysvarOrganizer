"""Mapeamento central de extensões em categorias (tipo aparente do arquivo).

A extensão indica apenas o tipo aparente: o conteúdo real dos arquivos não é
aberto nem verificado. Para estender, acrescente extensões em ``CATEGORIES``
ou em ``AMBIGUOUS``; nenhuma outra parte do código precisa mudar.

Extensões ambíguas (que podem pertencer a mais de uma categoria) recebem a
classificação conservadora "Não classificados ou ambíguos" e ficam para
revisão, com a explicação das alternativas possíveis.
"""

from __future__ import annotations

from dataclasses import dataclass

# Chave -> (rótulo, nome da pasta proposta)
CATEGORY_LABELS: dict[str, tuple[str, str]] = {
    "images": ("Fotografias e imagens", "Fotos"),
    "videos": ("Vídeos", "Vídeos"),
    "audio": ("Áudios e músicas", "Áudios"),
    "documents": ("Documentos", "Documentos"),
    "spreadsheets": ("Planilhas", "Planilhas"),
    "presentations": ("Apresentações", "Apresentações"),
    "archives": ("Arquivos compactados", "Compactados"),
    "executables": ("Instaladores e executáveis", "Executáveis"),
    "code": ("Projetos e código-fonte", "Código-fonte"),
    "data": ("Bancos de dados e arquivos estruturados", "Dados"),
    "other": ("Outros arquivos", "Outros"),
    "no_extension": ("Arquivos sem extensão", "Sem extensão"),
    "ambiguous": ("Não classificados ou ambíguos", "Não classificados"),
}

CATEGORIES: dict[str, frozenset[str]] = {
    "images": frozenset(
        "jpg jpeg jpe jfif png gif bmp tif tiff webp heic heif avif ico psd "
        "raw cr2 cr3 nef nrw arw dng orf rw2 raf srw pef".split()
    ),
    "videos": frozenset(
        "mp4 m4v mov avi mkv wmv flv webm mpg mpeg 3gp 3g2 mts m2ts vob ogv".split()
    ),
    "audio": frozenset("mp3 wav flac aac m4a ogg oga opus wma aif aiff mid midi amr ape".split()),
    "documents": frozenset("pdf doc docx docm odt rtf txt md epub mobi azw3 tex wpd pages".split()),
    "spreadsheets": frozenset("xls xlsx xlsm xlsb ods numbers".split()),
    "presentations": frozenset("ppt pptx pptm pps ppsx odp".split()),
    "archives": frozenset("zip rar 7z tar gz tgz bz2 tbz2 xz txz zst lz cab".split()),
    "executables": frozenset(
        "exe msi msix msixbundle appx appxbundle com scr apk dmg pkg deb rpm appimage dll sys".split()
    ),
    "code": frozenset(
        "py pyw pyi ipynb js mjs cjs jsx tsx java kt kts c h cpp cc cxx hpp cs go rs rb php "
        "swift vb vbs lua pl r dart scala css scss sass less vue svelte sh bash zsh ps1 psm1 "
        "bat cmd sln csproj vbproj vcxproj gradle".split()
    ),
    "data": frozenset(
        "db sqlite sqlite3 mdb accdb dbf mdf ldf ndf frm ibd parquet avro yaml yml toml ini "
        "cfg conf".split()
    ),
}

# Extensão ambígua -> categorias possíveis (explicação ao usuário).
AMBIGUOUS: dict[str, tuple[str, ...]] = {
    "csv": ("spreadsheets", "data"),
    "tsv": ("spreadsheets", "data"),
    "json": ("data", "code"),
    "xml": ("data", "code", "documents"),
    "sql": ("code", "data"),
    "html": ("documents", "code"),
    "htm": ("documents", "code"),
    "svg": ("images", "code"),
    "ts": ("code", "videos"),  # TypeScript ou fluxo de vídeo MPEG-TS
    "m": ("code",),  # Objective-C ou MATLAB
    "iso": ("archives", "executables"),
    "img": ("archives", "images"),
    "jar": ("executables", "code"),
    "key": ("presentations", "data"),  # Keynote ou chave/certificado
    "dat": ("data", "other"),
    "bin": ("data", "executables"),
    "log": ("documents", "data"),
    "bak": ("other",),
    "tmp": ("other",),
}

_BY_EXTENSION: dict[str, str] = {}
for _category, _extensions in CATEGORIES.items():
    for _extension in _extensions:
        _BY_EXTENSION.setdefault(_extension, _category)


@dataclass(frozen=True)
class Classification:
    category: str
    ambiguous: bool
    unknown_extension: bool
    justification: str


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, (category, category))[0]


def category_folder(category: str) -> str:
    return CATEGORY_LABELS.get(category, (category, category))[1]


def classify(extension: str) -> Classification:
    """Classifica pela extensão (já em minúsculas, sem ponto)."""
    note = " Classificação pelo tipo aparente (extensão); o conteúdo não foi verificado."
    if not extension:
        return Classification(
            "no_extension", False, False, "Arquivo sem extensão: tipo não identificável." + note
        )
    if extension in AMBIGUOUS:
        options = ", ".join(category_label(c) for c in AMBIGUOUS[extension])
        return Classification(
            "ambiguous",
            True,
            False,
            f"Extensão .{extension} é ambígua (pode ser: {options}). Classificação "
            "conservadora: não classificado, para revisão." + note,
        )
    category = _BY_EXTENSION.get(extension)
    if category is None:
        return Classification(
            "other", False, True, f"Extensão .{extension} desconhecida: classificada como outros." + note
        )
    return Classification(
        category, False, False, f"Extensão .{extension} → {category_label(category)}." + note
    )
