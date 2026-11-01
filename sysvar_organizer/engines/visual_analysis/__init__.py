"""Motor de análise visual: fotografias semelhantes por assinatura perceptual.

* ``signatures``: pHash e dHash de 64 bits (Pillow + numpy), com orientação
  EXIF e limites de segurança; miniaturas para exibição.
* ``grouping``: faixas de semelhança, busca de candidatos por índice
  multi-bloco e formação de grupos sem encadeamento.
* ``similar``: execução da busca sobre uma análise de armazenamento.

Somente leitura: nenhuma imagem é alterada, convertida sobre o original,
movida ou excluída.
"""
