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
- **ChatGPT (Jarvis):** único responsável pela escrita, revisão, manutenção e publicação da documentação do projeto no Cofre oficial `FernandoMurashima/sysvar-vault`.
- **Claude Code:** implementa apenas código-fonte, testes necessários, arquivos técnicos de configuração e empacotamento do Organizer, conforme tarefa aprovada.
- **Codex:** não participa do desenvolvimento deste produto. Quando usado em outros produtos, atua apenas sobre código, nunca sobre documentação.
- O Claude Code **não deve criar ou editar documentos** Markdown (incluindo README, manuais, planos, decisões, guias ou notas do Cofre). Este `CLAUDE.md` também é mantido exclusivamente pelo responsável documental; qualquer proposta de alteração deve ser reportada, não aplicada.
- Ao concluir trabalho técnico, informar arquivos de código alterados, validações realizadas, falhas e fatos que precisem ser documentados. Não editar o Cofre.
- O documento de referência é `09 - Sysvar Organizer/Plano de Gerenciamento do Projeto - Minuta 0.2.md` no Cofre `sysvar-vault`, aprovado em 01/11/2026.

## Diretrizes operacionais

- Trabalhar somente na etapa/pacote expressamente solicitado, evitando expansão de escopo.
- Preservar arquivos e mudanças existentes; não usar comandos destrutivos de Git para obter árvore limpa.
- Nunca excluir, mover ou sobrescrever arquivos do usuário automaticamente. Toda operação de risco requer aprovação explícita e, quando aplicável, mecanismos de reversão.
- Testar inicialmente com conjuntos fictícios isolados, nunca com arquivos pessoais de produção.
- Priorizar análise somente leitura, tratamento de erros, logs sem exposição de dados pessoais e verificações rápidas e proporcionais ao risco.
- Não criar documentação técnica por iniciativa própria; comunicar necessidade ao ChatGPT para registro no Cofre.
- Não fazer commit, push, instalar dependências globais nem executar ações em outros projetos sem autorização específica.
