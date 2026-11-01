"""Relatórios a partir dos dados já gravados no banco local.

Os relatórios apenas LEEM o SQLite: nenhuma varredura, hash, assinatura ou
comparação é refeita, e nenhum arquivo analisado é aberto ou alterado. A única
gravação é o arquivo de relatório, no caminho escolhido pelo usuário.
"""
