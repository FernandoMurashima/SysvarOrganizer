# CLAUDE.md — Sysvar Organizer

## Identidade e isolamento (obrigatório)

- Projeto: **Sysvar Organizer** (OIA-001), aplicativo desktop independente da família Sysvar.
- Diretório de trabalho: `C:\Projetos\Sysvar\Sysvar-Organizer`.
- Repositório exclusivo de código: `FernandoMurashima/SysvarOrganizer`.
- A marca Sysvar é a única ligação com Sysvar ERP, Hub, Commerce e CRM. Não existe dependência de código, banco, API, serviços ou infraestrutura entre estes produtos.
- **Nunca modificar**, criar, remover, executar manutenção ou fazer deploy nos projetos Sysvar ERP, Hub, Commerce, CRM, seus bancos, suas configurações ou seus serviços.
- Nunca aplicar alterações fora da árvore do Organizer sem autorização expressa do proprietário. Acesso de leitura a referências de documentação não é autorização de escrita.

## Papéis e documentação

- **Proprietário:** aprova escopo, etapas, alterações de risco e homologações.
- **Jarvis:** análise, planejamento, instruções, revisão e acompanhamento. É vedada a Jarvis qualquer alteração local ou remota.
- **Claude Code e Codex:** executam somente o trabalho atribuído e autorizado. Escrita documental, inclusive neste arquivo e no Cofre, exige autorização expressa. Commits, pushes, migrations e deploys exigem autorização específica.
- Ao concluir trabalho técnico, informar alterações, validações, falhas e necessidades de registro documental ao proprietário.
- Consultar o GOV-01 em `C:\Projetos\Sysvar\Cofre\08 - Governança e Agentes` antes de planejar ou atuar.
- O documento de referência é `09 - Sysvar Organizer/Plano de Gerenciamento do Projeto - Minuta 0.2.md` no Cofre `sysvar-vault`, aprovado em 01/11/2026.

## Diretrizes operacionais

- Trabalhar somente na etapa/pacote expressamente solicitado, evitando expansão de escopo.
- Preservar arquivos e mudanças existentes; não usar comandos destrutivos de Git para obter árvore limpa.
- Nunca excluir, mover ou sobrescrever arquivos do usuário automaticamente. Toda operação de risco requer aprovação explícita e, quando aplicável, mecanismos de reversão.
- Testar inicialmente com conjuntos fictícios isolados, nunca com arquivos pessoais de produção.
- Priorizar análise somente leitura, tratamento de erros, logs sem exposição de dados pessoais e verificações rápidas e proporcionais ao risco.
- Encaminhar necessidades de documentação ao proprietário para decisão de escopo e executor.
- Instalar dependências globais e atuar em outros projetos exige autorização específica.
