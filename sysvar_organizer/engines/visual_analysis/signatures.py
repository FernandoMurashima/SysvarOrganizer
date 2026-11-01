"""Assinaturas perceptuais de imagens (pHash e dHash de 64 bits).

pHash: imagem em tons de cinza reduzida a 32x32, DCT-II bidimensional, bloco
8x8 de baixas frequências comparado com a mediana (algoritmo usual de pHash).
dHash: imagem 9x8 em tons de cinza, comparação de pixels vizinhos na
horizontal. As duas assinaturas são usadas em conjunto: um pHash coincidente
não é tratado, sozinho, como prova de igualdade visual.

Segurança: arquivos são abertos apenas para leitura; só os decodificadores
de JPEG, PNG, WEBP, BMP e TIFF são aceitos; dimensões e tamanhos excessivos
são recusados antes da decodificação completa; a orientação EXIF é aplicada.
"""

from __future__ import annotations

import math
import os
import stat
import warnings
from dataclasses import dataclass

try:  # Dependências declaradas no pyproject; a interface avisa se faltarem.
    import numpy as np
    from PIL import Image, ImageOps, UnidentifiedImageError
    from PIL import __version__ as PILLOW_VERSION
except ImportError:  # pragma: no cover - verificado por dependencies_error()
    np = None
    Image = ImageOps = UnidentifiedImageError = None
    PILLOW_VERSION = "ausente"

from sysvar_organizer.engines.comparison.hashing import CLOUD_ONLY_ATTRIBUTES as _CLOUD_ONLY
from sysvar_organizer.engines.file_analysis.scanner import (
    IO_REPARSE_TAG_MOUNT_POINT,
    IO_REPARSE_TAG_SYMLINK,
    to_filesystem_path,
)

_LINK_TAGS = (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT)

# Extensões consideradas imagens candidatas (vídeos e documentos não são abertos).
IMAGE_EXTENSIONS = ("jpg", "jpeg", "jpe", "jfif", "png", "webp", "bmp", "tif", "tiff")
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP", "BMP", "TIFF")

MAX_FILE_BYTES = 512 * 1024 * 1024  # arquivos maiores não são decodificados
MAX_PIXELS = 120_000_000  # ~120 megapixels (largura x altura)
MIN_SIDE = 16  # imagens menores que 16 px não geram assinatura confiável

# Versão do algoritmo: muda a chave do cache quando o cálculo ou o Pillow mudam.
SIGNATURE_VERSION = f"phash32-dhash9x8-v1/pillow{PILLOW_VERSION.split('.')[0]}"

ORIENTATION_TAG = 0x0112


class ImageRejected(Exception):
    """A imagem não pode ser analisada com segurança."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


ISSUE_UNSUPPORTED = "unsupported_format"
ISSUE_CORRUPT = "corrupt_image"
ISSUE_TOO_LARGE = "too_large"
ISSUE_TOO_SMALL = "too_small"
ISSUE_CHANGED = "changed_during_read"


@dataclass(frozen=True)
class Signature:
    phash: int
    dhash: int
    width: int  # dimensões já com a orientação EXIF aplicada
    height: int
    format: str


def dependencies_error() -> str | None:
    """Mensagem quando Pillow ou numpy não estão instalados no ambiente."""
    if Image is None or np is None:
        return (
            "As bibliotecas Pillow e numpy não estão instaladas neste ambiente. "
            "Execute 'python -m pip install -e .' no ambiente virtual do projeto."
        )
    return None


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def is_image_name(name: str) -> bool:
    return os.path.splitext(name)[1][1:].lower() in IMAGE_EXTENSIONS


# Matriz da DCT-II (32 pontos). A escala não altera a comparação com a mediana.
_DCT_SIZE = 32
_DCT = None


def _dct_matrix():
    global _DCT
    if _DCT is None:
        n = np.arange(_DCT_SIZE)
        k = n.reshape(-1, 1)
        _DCT = np.cos(math.pi * (2 * n + 1) * k / (2 * _DCT_SIZE))
    return _DCT


def _bits_to_int(bits) -> int:
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bool(bit))
    return value


def phash_of(gray) -> int:
    """pHash de uma imagem Pillow em modo 'L'."""
    small = gray.resize((_DCT_SIZE, _DCT_SIZE), Image.Resampling.LANCZOS)
    pixels = np.asarray(small, dtype=np.float64)
    matrix = _dct_matrix()
    dct = matrix @ pixels @ matrix.T
    low = dct[:8, :8]
    return _bits_to_int(low > np.median(low))


def dhash_of(gray) -> int:
    """dHash de uma imagem Pillow em modo 'L'."""
    small = gray.resize((9, 8), Image.Resampling.LANCZOS)
    pixels = np.asarray(small, dtype=np.int16)
    return _bits_to_int(pixels[:, 1:] > pixels[:, :-1])


def _flatten_to_gray(image):
    """Converte para tons de cinza, compondo transparência sobre fundo branco."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        image = background
    return image.convert("L")


def _open_checked(handle):
    """Abre a imagem lendo apenas o cabeçalho e aplica os limites de segurança."""
    try:
        image = Image.open(handle, formats=ALLOWED_FORMATS)
    except UnidentifiedImageError as exc:
        raise ImageRejected(ISSUE_UNSUPPORTED, "Formato de imagem não suportado.") from exc
    width, height = image.size
    if width * height > MAX_PIXELS:
        raise ImageRejected(
            ISSUE_TOO_LARGE, f"Dimensões excessivas ({width} x {height} pixels)."
        )
    if min(width, height) < MIN_SIDE:
        raise ImageRejected(ISSUE_TOO_SMALL, f"Imagem muito pequena ({width} x {height}).")
    return image


def _oriented_size(image) -> tuple[int, int]:
    width, height = image.size
    try:
        orientation = image.getexif().get(ORIENTATION_TAG, 1)
    except Exception:  # noqa: BLE001 - EXIF malformado não impede a análise
        orientation = 1
    if orientation in (5, 6, 7, 8):
        return height, width
    return width, height


def _decode(path: str, target: tuple[int, int]):
    """Decodifica com redução antecipada (JPEG) e orientação EXIF aplicada.

    Retorna (imagem, largura, altura, formato, stat antes da leitura).
    """
    fs_path = to_filesystem_path(path)
    st = os.stat(fs_path, follow_symlinks=False)
    if st.st_size > MAX_FILE_BYTES:
        raise ImageRejected(ISSUE_TOO_LARGE, "Arquivo de imagem grande demais para análise.")
    with open(fs_path, "rb") as handle:
        before = os.fstat(handle.fileno())
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                image = _open_checked(handle)
                width, height = _oriented_size(image)
                fmt = image.format or "?"
                if fmt == "JPEG":
                    image.draft("RGB", target)  # decodificação reduzida, mais rápida
                image.seek(0)
                image.load()
                image = ImageOps.exif_transpose(image)
            except ImageRejected:
                raise
            except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                raise ImageRejected(ISSUE_TOO_LARGE, "Dimensões excessivas.") from exc
            except (OSError, ValueError, SyntaxError, EOFError) as exc:
                raise ImageRejected(ISSUE_CORRUPT, "Imagem corrompida ou ilegível.") from exc
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ImageRejected(ISSUE_CHANGED, "Arquivo alterado durante a leitura.")
    return image, width, height, fmt, before


def compute_signature(path: str, expected: tuple[int, int] | None = None) -> Signature:
    """Assinatura da imagem. ``expected`` = (tamanho, mtime_ns) validados antes."""
    image, width, height, fmt, before = _decode(path, (256, 256))
    if expected is not None and (before.st_size, before.st_mtime_ns) != expected:
        raise ImageRejected(ISSUE_CHANGED, "Arquivo alterado após a validação.")
    gray = _flatten_to_gray(image)
    return Signature(phash_of(gray), dhash_of(gray), width, height, fmt)


def load_thumbnail(path: str, max_side: int = 160) -> tuple[bytes, int, int]:
    """Miniatura RGB (bytes, largura, altura) para exibição. Somente leitura."""
    image, _, _, _, _ = _decode(path, (max_side * 2, max_side * 2))
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        image = background
    image = image.convert("RGB")
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return image.tobytes(), image.width, image.height


def safe_thumbnail(path: str, max_side: int = 160) -> tuple[bytes, int, int] | None:
    """Miniatura somente leitura; None se o arquivo for link/junção, estiver só
    na nuvem, não for arquivo comum ou não puder ser decodificado com segurança."""
    if dependencies_error() is not None:
        return None
    try:
        st = os.stat(to_filesystem_path(path), follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or getattr(st, "st_reparse_tag", 0) in _LINK_TAGS:
            return None
        if getattr(st, "st_file_attributes", 0) & _CLOUD_ONLY:
            return None
        return load_thumbnail(path, max_side)
    except (ImageRejected, OSError):
        return None


def to_hex(value: int) -> str:
    return f"{value:016x}"


def from_hex(text: str) -> int:
    return int(text, 16)
