"""Execução de tarefas curtas de leitura (exportação, verificação) fora da interface.

A função recebe um ``threading.Event`` de cancelamento e um callback de
progresso ``(feitos, total)``. Resultado e erro voltam à interface pelo sinal
``finished``; a interface nunca fica bloqueada enquanto a tarefa roda.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

logger = logging.getLogger(__name__)

TaskFunction = Callable[[threading.Event, Callable[[int, int], None]], object]


class FunctionWorker(QObject):
    """Executa ``function`` numa QThread. Deve ser movido para a thread antes de ``run``."""

    progress = Signal(int, int)
    finished = Signal(object, object)  # resultado, exceção (ou None)

    def __init__(self, function: TaskFunction) -> None:
        super().__init__()
        self._function = function
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        result = error = None
        try:
            result = self._function(self._cancel, self.progress.emit)
        except Exception as exc:  # noqa: BLE001 - o erro é devolvido à interface
            error = exc
        self.finished.emit(result, error)
        QThread.currentThread().quit()


class BackgroundTask(QObject):
    """Controla uma tarefa em segundo plano por vez (início, cancelamento, espera)."""

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: FunctionWorker | None = None

    def is_running(self) -> bool:
        return self._thread is not None

    def start(
        self,
        function: TaskFunction,
        on_finished: Callable[[object, object], None],
        on_progress: Callable[[int, int], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> bool:
        if self.is_running():
            return False
        self._worker = FunctionWorker(function)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(on_finished)
        if on_progress is not None:
            self._worker.progress.connect(on_progress)
        self._thread.finished.connect(lambda: self._cleanup(on_done))
        self._thread.start()
        return True

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def wait(self, milliseconds: int) -> bool:
        if self._thread is None:
            return True
        self.cancel()
        return bool(self._thread.wait(milliseconds))

    def _cleanup(self, on_done: Callable[[], None] | None) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        if on_done is not None:
            on_done()
