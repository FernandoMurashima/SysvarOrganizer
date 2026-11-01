"""Carregamento de miniaturas sob demanda, fora da thread da interface.

As imagens são decodificadas em segundo plano (no máximo duas por vez),
reduzidas a 160 px e mantidas num cache limitado (LRU). Pedidos de um grupo
anterior são descartados quando outro grupo é selecionado.
"""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap

from sysvar_organizer.engines.visual_analysis.signatures import safe_thumbnail

THUMBNAIL_SIDE = 160
CACHE_CAPACITY = 120  # ~12 MB de miniaturas no máximo


class _Relay(QObject):
    loaded = Signal(object)  # (geração, chave, bytes | None, largura, altura)


class _ThumbnailTask(QRunnable):
    def __init__(self, generation: int, key: str, path: str, relay: _Relay) -> None:
        super().__init__()
        self._generation = generation
        self._key = key
        self._path = path
        self._relay = relay

    def run(self) -> None:
        result = safe_thumbnail(self._path, THUMBNAIL_SIDE)
        if result is None:
            self._relay.loaded.emit((self._generation, self._key, None, 0, 0))
        else:
            data, width, height = result
            self._relay.loaded.emit((self._generation, self._key, data, width, height))


class ThumbnailLoader(QObject):
    ready = Signal(str, object)  # chave, QPixmap | None

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._relay = _Relay()
        self._relay.loaded.connect(self._on_loaded)
        self._cache: OrderedDict[str, QPixmap | None] = OrderedDict()
        self._generation = 0

    def reset(self) -> None:
        """Descarta pedidos pendentes (ex.: outro grupo foi selecionado)."""
        self._generation += 1
        self._pool.clear()

    def request(self, key: str, path: str) -> QPixmap | None | bool:
        """Retorna a miniatura em cache, ou False se ela foi solicitada."""
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        self._pool.start(_ThumbnailTask(self._generation, key, path, self._relay))
        return False

    def shutdown(self) -> None:
        self.reset()
        self._pool.waitForDone(3000)

    def _on_loaded(self, payload: tuple) -> None:
        generation, key, data, width, height = payload
        pixmap = None
        if data is not None:
            image = QImage(data, width, height, width * 3, QImage.Format.Format_RGB888).copy()
            pixmap = QPixmap.fromImage(image)
        self._cache[key] = pixmap
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_CAPACITY:
            self._cache.popitem(last=False)
        if generation == self._generation:
            self.ready.emit(key, pixmap)
