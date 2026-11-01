"""Classificação de arquivos e simulação (prévia) de organização.

Somente planejamento sobre os metadados indexados: nenhum arquivo ou pasta é
criado, copiado, movido, renomeado ou excluído, nem mesmo no destino simulado.

* ``categories``: mapeamento central e extensível de extensões em categorias.
* ``rules``: modelos de organização (A, B, C), validação de nomes e caminhos
  do Windows e alertas sobre o destino simulado.
* ``sensitive``: detecção de estruturas que não devem ser desmontadas.
* ``planner``: execução da simulação sobre uma análise concluída.
"""
