"""Gestão de operações sobre arquivos: plano, revisão, execução e recuperação.

Primeira fase de operações reais (Bloco 10): copiar, mover e reverter SOMENTE
dentro de áreas de homologação autorizadas explicitamente pelo usuário, com
seleção individual, validação física imediata e confirmação final. Planos sem
área autorizada podem ser revisados, mas nunca executados. Envio à Lixeira e
exclusão continuam bloqueados; exclusão definitiva não existe.

* ``models``: tipos de ação, estados, modos e rótulos.
* ``safety``: regras gerais de segurança de caminhos.
* ``authorization``: política das áreas autorizadas.
* ``test_area``: área isolada dos testes automatizados (fora do aplicativo distribuído).
* ``associations``: arquivos associados (RAW, XMP, JPEG do mesmo disparo).
* ``planning``: plano de ações a partir de uma prévia do Bloco 06.
* ``executor``: validação, execução, reversão e reconciliação seguras.
* ``runner``: execução em lote por unidade, com cancelamento e registro.
* ``recycle``: Lixeira (segunda fase, desabilitada).
"""
