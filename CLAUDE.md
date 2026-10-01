# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Comandos

```bash
bash tests/test-interrupcao.sh                      # suíte completa (41 verificações); Git Bash no Windows, WSL, Linux, macOS
PYTHONPATH=src python -m coimbra_memory <cmd>       # rodar do código-fonte (é assim que a suíte chama)
uvx --from . cmem doctor                            # rodar como pacote instalado
```

Não há teste unitário isolado: cada seção da suíte depende do estado da anterior, num repositório temporário. A suíte usa `pwd -W` para mandar caminhos nativos no JSON (no Git Bash, `$PWD` seria `/tmp/...`, que o Python do Windows não abre). `uv` reutiliza builds em cache de diretório local com a mesma versão: para testar uma instalação real, use `uv tool install --reinstall --no-cache .`.

## Arquitetura

Só stdlib. **Nunca altera arquivos do projeto do usuário**: o estado fica em `<git-common-dir>/coimbra-memory/state/<branch codificada>/` (compartilhado entre worktrees, nunca versionado).

- `core.py` — tudo o que não é protocolo nem instalação:
  - **Estado semântico** (`state.md`), escrito pelo agente. `validate_text` exige as seções de `REQUIRED`, o marcador `END_MARK` e ≤80 linhas. Válido → cópia para `state.good.md` + evento `state_ok` no diário. `update_state` (usado pelo MCP) substitui só as seções enviadas, casando nomes sem acento/caixa via `plain()`, e só grava se o resultado for válido.
  - **Diário mecânico** (`journal.jsonl`), escrito pelos hooks: `start`/`end` pareados por `tool_use_id`; `start` sem `end` = operação sem confirmação de término.
  - **Checkpoint**: dois commits fora das branches com índice temporário (`GIT_INDEX_FILE`): `refs/coimbra-memory/wip/<branch>` (só working tree) e `refs/coimbra-memory/state/<branch>` (só estado). `import_state` lê a segunda.
  - **Ponte de conversas**: `find_transcripts` → `parse_claude` / `parse_codex` / `parse_opencode` → `bridge_pack`. Lê o fim das transcrições que os harnesses gravam (`$CLAUDE_CONFIG_DIR/projects/`, `$CODEX_HOME/sessions/`, SQLite do OpenCode com pseudo-ref `<db>#<session_id>`). Escolhe a mais recente do repo/branch, excluindo a sessão atual. Roda num `try` em `resume_pack`.
  - `hook(harness)`: dispatcher por `hook_event_name`. `main`/`entry`: CLI.
- `mcp.py` — JSON-RPC 2.0 por linha em stdio, sem SDK; stdout é exclusivo do protocolo. `INSTRUCTIONS` vai no `initialize` e é o que faz o agente chamar `retomar` sozinho. `readOnlyHint` nas ferramentas de leitura é necessário: sem ele o Codex exige aprovação e, com política `never`, bloqueia.
- `install.py` — edita configurações de usuário: Claude (`claude mcp add` ou `~/.claude.json`; hooks em `settings.json`), Codex (bloco delimitado por `BEGIN`/`END` no `config.toml`, com `default_tools_approval_mode = "approve"`), OpenCode (`opencode.json[c]`, recusa editar se houver comentários). Backup antes de gravar, validação antes de gravar, idempotente. `launcher()` recusa caminhos efêmeros do `uvx` (cache do uv).

## Invariantes

- **Hook nunca derruba a sessão**: `entry()` engole exceções e sai 0. Único exit não-zero intencional no modo hook é `2` para `state.md` inválido.
- **UTF-8 forçado** em stdin/stdout/stderr (`entry()`) e no `run()` do git. Sem isso, no Windows com saída redirecionada o Python usa cp1252 e quebra no `⚠`.
- **Stop**: `decision: block` só uma vez (`stop_hook_active`). **`Interrupt`/`SessionEnd`**: só diário, sem checkpoint (Codex dá 1–3 s).
- **Gravação** via `atomic_write` (tmp + `os.replace`); diário com `fsync`; leitura ignora linha truncada.
- **Segredos**: todo alvo passa por `describe()`/`short()` → `redact()`. Nunca registrar saída de ferramenta. Texto de transcrição só é impresso, nunca gravado no estado (iria para a ref de checkpoint).
- **OpenCode**: banco aberto `mode=ro`, só tabelas `session`/`message`/`part` — o mesmo arquivo guarda contas e credenciais.
- **Diagnóstico não cria nada**: use `state_dir(root, create=False)` em código só de leitura.
- Caminhos impressos com `/` (`slash()`): entram em JSON e valem em todos os shells.

## Acoplamentos que quebram fácil

- A suíte faz `grep` em strings exatas da saída em português (`"Usando a última versão íntegra"`, `"sem confirmação de término"`, `"Conversa anterior — <harness>"`, `"já configurado"`, `'"decision": "block"'`). Mudou mensagem → atualizar o teste.
- A suíte exporta `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `CMEM_LAUNCHER` e `CMEM_CLAUDE_CLI=none` para não tocar em dados nem configurações reais. Teste novo deve manter isso.
- `clean_user()` decide o que é contexto injetado pelo harness; prefixo novo → adicionar lá e no teste.
- `is_ours()` identifica hooks do cmem pelo comando (`" hook claude"` + `cmem`); mudar o formato do comando quebra `uninstall`.
- O README cita números (41 verificações, padrões das variáveis `CMEM_*`, 80 linhas, 8 hooks): manter em sincronia.

Documentação e mensagens em português com acentuação correta.
