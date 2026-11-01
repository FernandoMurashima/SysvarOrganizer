"""Gestão de operações sobre arquivos: plano, revisão, execução e recuperação.

ATENÇÃO: nesta etapa (Bloco 07) a execução física só é possível dentro de uma
área de testes isolada, criada pelo conjunto de testes com marcador e token
próprios. No aplicativo distribuído normalmente, nenhuma operação altera
arquivos reais: planos do ambiente real podem ser consultados e revisados,
mas nunca executados. Exclusão definitiva não existe neste módulo.

* ``models``: tipos de ação, estados e rótulos.
* ``safety``: área de testes isolada e validação de caminhos.
* ``associations``: arquivos associados (RAW, XMP, JPEG do mesmo disparo).
* ``planning``: plano de ações a partir de uma prévia do Bloco 06.
* ``executor``: validação, execução, reversão e reconciliação seguras.
"""
