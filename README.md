# coimbra-memory

**Retoma a tarefa quando o agente de código cai** — limite de uso, contexto esgotado, Ctrl+C ou travamento — e deixa você continuar em **outro** agente: Claude Code, Codex ou OpenCode.

```
Claude Code bate no limite no meio da tarefa
        ↓
você abre o Codex na mesma pasta e diz "continue"
        ↓
o Codex já sabe: o que você pediu, o que o Claude fez, onde parou e por quê
```

Sem dependências (Python puro), sem servidor na nuvem, sem alterar nenhum arquivo do seu projeto.

**Por que usar**

- **Funciona depois que o agente caiu.** Não depende de ele "lembrar" de escrever um resumo antes do limite: lê a conversa que o próprio programa já salvou em disco.
- **Instala uma vez, vale em todos os projetos git.** Nada para configurar por projeto, nada no `git status`.
- **Troca de verdade entre programas.** Claude Code → Codex → OpenCode → Claude Code, em qualquer ordem, inclusive com o mesmo programa em sessões diferentes.
- **Não repete o que é perigoso.** Um comando que começou e não terminou (migração, script de dados) aparece marcado para ser verificado antes de rodar de novo.
- **Privado.** Nada sai do seu computador.

---

## Sumário

- [O problema](#o-problema)
- [Como funciona](#como-funciona)
- [Instalação](#instalação)
- [O que a instalação altera](#o-que-a-instalação-altera)
- [Uso no dia a dia](#uso-no-dia-a-dia)
- [Ferramentas MCP](#ferramentas-mcp)
- [Comandos](#comandos)
- [O estado da tarefa (`state.md`)](#o-estado-da-tarefa-statemd)
- [Continuar em outro worktree ou máquina](#continuar-em-outro-worktree-ou-máquina)
- [Privacidade e segurança](#privacidade-e-segurança)
- [O que pode ser perdido](#o-que-pode-ser-perdido)
- [Limitações conhecidas](#limitações-conhecidas)
- [Configuração avançada](#configuração-avançada)
- [Desinstalar](#desinstalar)
- [Desenvolvimento](#desenvolvimento)

---

## O problema

Cada assinatura de IA vem presa a um programa: Claude no Claude Code, ChatGPT no Codex, outros modelos no OpenCode. Em projetos grandes, o limite acaba no meio da tarefa e você precisa trocar de programa. O novo agente começa do zero: não sabe o que foi pedido, o que já foi feito, o que foi descartado nem qual comando estava rodando.

Soluções de *handoff* existentes pedem que o agente que está saindo escreva um resumo. Mas quando o limite chega, **ele já não consegue escrever nada**.

O coimbra-memory resolve o caso que importa: ele funciona **depois** que o agente parou.

## Como funciona

Pense numa troca de plantão num hospital. Quem chega precisa de três coisas:

| Peça | Analogia | Quem escreve | Custo |
|---|---|---|---|
| **Estado da tarefa** (`state.md`) | O prontuário: objetivo, etapa atual, próximo passo, o que foi descartado | O agente, durante o trabalho (pela ferramenta `salvar_estado`) | Uma chamada curta por etapa |
| **Diário mecânico** (`journal.jsonl`) | O registro automático dos aparelhos: "rodou tal comando às 14h02, não terminou" | Hooks do Claude Code (opcional) | Zero tokens |
| **Ponte de conversas** | A gravação da última conversa do plantão anterior | Ninguém: lê o histórico que Claude Code, Codex e OpenCode **já salvam sozinhos** em disco | Zero tokens |

A **ponte de conversas** é o diferencial. Os três programas gravam cada conversa no disco enquanto ela acontece — inclusive quando a sessão morre por limite de uso. Quem retoma lê o fim dessa conversa: seus últimos pedidos, as últimas respostas do agente, as ferramentas chamadas (sem a saída delas) e o motivo do fim ("You've hit your session limit", "Insufficient balance", "turno cortado sem conclusão"). Quem resume é o agente novo, que tem tokens sobrando.

Tudo isso chega ao agente num **pacote de retomada** (~450 a 2000 tokens), pela ferramenta MCP `retomar` ou, no Claude Code com hooks, automaticamente no início da sessão.

### Onde fica cada coisa

Nada é criado dentro da pasta do seu projeto. O estado fica dentro de `.git/`, que o git nunca versiona:

```
seu-projeto/.git/coimbra-memory/state/<branch>/
├── state.md        ← estado escrito pelo agente
├── state.good.md   ← última versão válida (proteção contra gravação pela metade)
└── journal.jsonl   ← diário mecânico
```

Cada branch tem seu próprio estado (`feature/login` e `main` não se misturam). Worktrees do mesmo repositório compartilham o estado.

### Relação com outras ferramentas

- **ai-memory** e similares guardam **conhecimento de longo prazo** (decisões, regras, histórico pesquisável). O coimbra-memory cuida da **continuidade da tarefa em andamento**. Os dois convivem.
- **Skills de "session handoff"** dependem de o agente escrever antes de parar. O coimbra-memory funciona mesmo quando ele não consegue.
- **cass** e similares **buscam** no histórico de agentes. O coimbra-memory **entrega** o fim da última conversa pronto, sem você precisar saber o que procurar.

---

## Instalação

Funciona em **Windows 11, Linux e WSL** (testado). macOS deve funcionar, mas ainda não foi testado.

### 1. Requisitos

- **git**
- **uv** — instalador de Python que baixa o próprio Python (você não precisa ter Python instalado):

  ```powershell
  # Windows (PowerShell)
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
  ```bash
  # Linux, WSL, macOS
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

> **Regra importante:** use os três programas **no mesmo sistema** — todos no Windows ou todos no Linux/WSL. A ponte reconhece o projeto pelo caminho da pasta, e o mesmo projeto tem caminhos diferentes no Windows (`C:\projetos\app`) e no WSL (`/mnt/c/projetos/app`).

### 2. Instalar o comando

```bash
uv tool install git+https://github.com/elvertoni/coimbra-memory
```

Isso cria os comandos `cmem` e `coimbra-memory`. Se o terminal não encontrar `cmem`, rode `uv tool update-shell` e abra um terminal novo.

### 3. Ver o que vai mudar (recomendado)

```bash
cmem install --simular
```

Mostra cada arquivo que seria alterado, sem gravar nada.

### 4. Configurar os programas

```bash
cmem install            # registra o servidor MCP no Claude Code, Codex e OpenCode
cmem install --hooks    # o mesmo + hooks do Claude Code (retomada 100% automática lá)
```

Reinicie os programas que estiverem abertos.

### 5. Conferir

```bash
cmem doctor
```

```
✓ git
✓ cmem permanente: C:/Users/voce/.local/bin/cmem.exe
✓ servidor MCP responde
✓ Claude Code: MCP
✓ Claude Code: 8 hooks (opcionais)
✓ Codex: MCP
✓ OpenCode: MCP
Conversas recentes encontradas: 12 (claude, codex, opencode)
```

Rodado dentro de um projeto, o `doctor` também mostra quantas conversas a ponte encontrou ali.

### Atualizar

```bash
uv tool upgrade coimbra-memory
```

---

## O que a instalação altera

Tudo em nível de **usuário** (vale para todos os projetos). Antes de alterar qualquer arquivo, o instalador cria uma cópia `<arquivo>.cmem-backup-AAAAMMDD-HHMMSS`. O resultado é validado antes de gravar, e a instalação pode ser repetida sem duplicar nada.

| Programa | Arquivo | O que entra |
|---|---|---|
| Claude Code | `~/.claude.json` (via `claude mcp add --scope user`) | servidor MCP `coimbra-memory` |
| Claude Code (`--hooks`) | `~/.claude/settings.json` | 8 hooks: `SessionStart`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PreCompact`, `Stop`, `StopFailure`, `SessionEnd`. Seus hooks existentes são mantidos |
| Codex | `~/.codex/config.toml` | um bloco delimitado por comentários `# >>> coimbra-memory` … `# <<< coimbra-memory <<<` |
| OpenCode | `~/.config/opencode/opencode.json` ou `.jsonc` | entrada `coimbra-memory` em `"mcp"` |

O bloco do Codex:

```toml
# >>> coimbra-memory (gerado por `cmem install`; remova com `cmem uninstall`) >>>
[mcp_servers.coimbra-memory]
command = "C:/Users/voce/.local/bin/cmem.exe"
args = ["mcp"]
# Libera salvar_estado e checkpoint sem pedir aprovação: só gravam em .git/coimbra-memory
default_tools_approval_mode = "approve"
# <<< coimbra-memory <<<
```

Se o `opencode.jsonc` tiver comentários, o instalador **não** o edita (os comentários seriam perdidos) e mostra o trecho para você colar.

Variáveis respeitadas: `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `XDG_CONFIG_HOME`.

### Hooks do Claude Code: vale a pena?

Sem hooks, o agente recebe instruções do servidor MCP para chamar `retomar` no início e `salvar_estado` antes de etapas longas. Funciona bem na prática (nos testes, o Codex chamou `retomar` sozinho quando o pedido foi para retomar a tarefa, sem citar a ferramenta), mas depende de o agente obedecer.

Com `--hooks`, no Claude Code:

- o pacote de retomada entra **automaticamente** no início de cada sessão;
- cada comando e edição vira registro no diário (zero tokens), e um comando que começou e não terminou aparece como **"operação sem confirmação de término"**;
- um checkpoint git é gravado na compactação, no fim do turno, em falha de API e a cada 5 minutos de edições;
- a cada 8 ações sem estado novo, o agente recebe um lembrete curto; no fim do turno, se houve progresso sem estado salvo, ele é cobrado **uma vez**.

O custo: cada ferramenta usada dispara um processo Python rápido.

---

## Uso no dia a dia

### Em qualquer projeto: nada a configurar

A instalação vale para o computador inteiro. Todo projeto que seja **repositório git** já está coberto; na primeira vez, o estado é criado sozinho dentro de `.git/coimbra-memory/`.

1. Abra o programa **na pasta do projeto** (ou numa subpasta), como sempre.
2. Trabalhe normalmente.
3. Quando o limite acabar, abra outro programa **na mesma pasta** e diga *"continue de onde parou"*.

| Condição | Se não for o caso |
|---|---|
| O projeto é um repositório git | `git init` na pasta |
| Os programas rodam no mesmo sistema | Não misture Windows e WSL no mesmo projeto |

Para conferir um projeto, rode `cmem doctor` dentro dele: o final mostra o repositório reconhecido e quantas conversas anteriores a ponte encontrou ali. O `doctor` só lê, não cria nada.

### Um exemplo real

Você não precisa fazer nada de diferente:

1. No Claude Code: *"implemente bloqueio após 5 tentativas de login"*. Antes de rodar a migração, o agente chama `salvar_estado` com a etapa atual e como verificar se a migração terminou.
2. O limite acaba no meio da migração.
3. Você abre o Codex na mesma pasta: *"continue de onde parou"*.
4. O Codex chama `retomar` e recebe:

```
## Retomada — branch `feature/login` — HEAD a1b2c3d
### Estado semântico (escrito pelo agente)
## Objetivo
Login por e-mail com bloqueio após 5 tentativas.
## Etapa atual
Gerar e aplicar a migração 0002.
## Operação em curso
- Ação: python manage.py migrate
- Como verificar: python manage.py showmigrations app | grep 0002
- Seguro repetir? sim
...
### ⚠ Operações iniciadas sem confirmação de término
- [2026-10-01 14:02:11] claude Bash: python manage.py migrate
### Conversa anterior — claude, 2026-10-01 14:03 — terminou com erro: You've hit your session limit
- [13:58] você: implemente bloqueio após 5 tentativas de login
- [14:01] agente: Vou criar a migração 0002 e depois a view.
- [14:02] ferramentas: Bash: python manage.py migrate
### git status (até 15 linhas)
 M app/models.py
```

5. O Codex verifica se a migração foi aplicada **antes** de repetir, e segue.
6. Quando o limite do Claude renova, você volta para ele, e o mesmo acontece ao contrário: ele recebe o fim da conversa com o Codex.

**Ordem de confiança** que o agente segue: estado real do repositório (git, arquivos, testes) > diário mecânico > estado salvo e conversa anterior (vale o mais recente) > memórias antigas. O estado salvo é tratado como hipótese a conferir, nunca como verdade absoluta.

---

## Ferramentas MCP

| Ferramenta | O que faz | Grava? |
|---|---|---|
| `retomar` | Pacote de retomada: estado, operações sem término, atividade posterior ao estado, fim da conversa anterior, `git status` | não |
| `salvar_estado` | Atualiza **só as seções enviadas** do `state.md`; valida antes e grava de forma atômica. Se faltar seção obrigatória, nada é gravado e o erro volta ao agente | só em `.git/coimbra-memory` |
| `conversa` | Trecho maior do fim da última conversa, quando o pacote não basta | não |
| `checkpoint` | Grava agora o código (inclusive arquivos novos) e o estado em refs git fora das branches | só refs `refs/coimbra-memory/*` |

Todas aceitam `diretorio` (raiz do projeto); sem ele, usam o diretório onde o programa iniciou o servidor. O servidor também envia instruções curtas ao agente dizendo quando usar cada ferramenta.

---

## Comandos

| Comando | Para quê |
|---|---|
| `cmem install [--hooks] [--simular]` | Configura os três programas |
| `cmem uninstall [--simular]` | Desfaz a configuração (o estado dos projetos é mantido) |
| `cmem doctor` | Diagnóstico da instalação e do repositório atual |
| `cmem resume` | Imprime o pacote de retomada (útil em qualquer outro agente: cole no chat) |
| `cmem conversa [caracteres]` | Fim da última conversa deste repositório, de qualquer programa |
| `cmem checkpoint [motivo]` | Checkpoint manual antes de algo arriscado |
| `cmem importar [ref] [--forcar]` | Traz o estado de um checkpoint (outro clone ou máquina) |
| `cmem modelo` | Modelo do `state.md` |
| `cmem path` | Pasta de estado da branch atual |
| `cmem validate` | Valida o `state.md` da branch atual |
| `cmem mcp` | Inicia o servidor MCP (os programas chamam isso sozinhos) |

**Outro agente sem MCP?** Rode `cmem resume` e cole a saída no chat.

---

## O estado da tarefa (`state.md`)

O agente mantém um arquivo curto (máximo 80 linhas):

```markdown
# Estado — <branch>
_Atualizado: AAAA-MM-DD HH:MM · <harness/modelo>_

## Objetivo
<a tarefa maior, em 1–2 linhas>

## Plano
- [x] <feito>
- [>] <atual>
- [ ] <pendente>

## Etapa atual
<o que está sendo feito agora e qual é o raciocínio>

## Operação em curso
- Ação: <comando ou mudança; "nenhuma" se não houver>
- Como verificar se terminou: <comando, arquivo, consulta>
- Seguro repetir? <sim / não / só após verificar>

## Próximo passo
<concreto e acionável por quem não viu a conversa>

## Descartado
- <tentativa> — <por que falhou>

## Hipóteses abertas
- <suspeita não confirmada> — como confirmar: <passo>
<!-- fim-do-estado -->
```

**Obrigatórias:** Objetivo, Etapa atual, Operação em curso, Próximo passo e o marcador final. Um estado sem elas é recusado; se uma gravação for cortada no meio, a retomada usa a última versão válida (`state.good.md`).

A regra de ouro, que o servidor ensina ao agente: **escrever ANTES** de cada etapa longa ou operação com efeito colateral (migração, script de dados, deploy, refatoração ampla) — porque a sessão pode acabar sem aviso.

### Reforço opcional por projeto

Se quiser reforçar o protocolo num projeto específico, acrescente ao `AGENTS.md` (Codex e OpenCode) ou ao `CLAUDE.md`:

```markdown
## Continuidade (coimbra-memory)
- Ao começar ou retomar: chame a ferramenta `retomar` (MCP coimbra-memory).
- Antes de etapa longa ou operação com efeito colateral: `salvar_estado` com
  Etapa atual, Operação em curso (ação, como verificar, se é seguro repetir)
  e Próximo passo. Depois de cada etapa relevante, só as seções que mudaram.
- Operação "iniciada sem confirmação": verifique o efeito antes de repetir.
```

---

## Continuar em outro worktree ou máquina

O `checkpoint` grava duas refs git fora das branches, sem tocar no seu índice nem na sua branch:

| Ref | Conteúdo |
|---|---|
| `refs/coimbra-memory/wip/<branch>` | O código como estava, inclusive arquivos novos não ignorados. Pode virar branch sem levar arquivos do sistema |
| `refs/coimbra-memory/state/<branch>` | Só o estado e o diário, com histórico próprio |

```bash
# Outro worktree, mesma máquina (o estado já é compartilhado)
git worktree add ../retomada refs/coimbra-memory/wip/feature/login
cd ../retomada && git switch -c feature/login-retomada

# Outra máquina, via remoto PRIVADO
git push origin 'refs/coimbra-memory/*:refs/coimbra-memory/*'      # na origem
git fetch origin 'refs/coimbra-memory/*:refs/coimbra-memory/*'     # no destino
git switch feature/login && cmem importar
```

Histórico dos checkpoints: `git reflog refs/coimbra-memory/wip/<branch>`.

---

## Privacidade e segurança

- **Nada sai do seu computador.** Não há servidor remoto, telemetria nem chamada de rede. O servidor MCP roda localmente, iniciado pelo próprio programa.
- **A ponte lê suas conversas locais** do Claude Code (`~/.claude/projects/`), do Codex (`~/.codex/sessions/`) e do OpenCode (`~/.local/share/opencode/opencode.db`), apenas do repositório e da branch atuais, e só o fim delas. O texto vai para o contexto do próximo agente; **nunca é gravado** no estado nem nos checkpoints.
- **Banco do OpenCode:** aberto em modo somente leitura, consultando só as tabelas de conversa (o mesmo arquivo guarda contas e credenciais, que nunca são lidas).
- **Segredos** com cara de token (`token=…`, `Bearer …`, `sk-…`, `ghp_…`, `AKIA…`) são mascarados no diário e na ponte. A máscara é uma rede de proteção, não uma garantia: não cole senhas no chat.
- **O que é omitido:** saída de ferramentas, contexto injetado pelos programas e raciocínio interno do modelo.
- **Checkpoints incluem todo arquivo não ignorado pelo `.gitignore`.** Confira se `.env` e dados sensíveis estão ignorados antes de enviar `refs/coimbra-memory/*` a qualquer remoto, e só envie a remotos privados. As refs ficam locais até você mandá-las explicitamente.

---

## O que pode ser perdido

Nenhum mecanismo recupera o que não foi escrito em lugar nenhum. Com o coimbra-memory:

| Cenário | Recuperado | Pode ser perdido |
|---|---|---|
| Limite de uso | Conversa até a última mensagem, motivo do fim, estado salvo; com hooks no Claude, diário e checkpoint no momento | Raciocínio que o agente não chegou a escrever na resposta |
| Contexto esgotado (compactação) | Tudo acima; com hooks no Claude, o pacote é reinjetado após a compactação | Idem |
| Ctrl+C, terminal fechado | Conversa até a última mensagem; operação em andamento aparece como "sem confirmação de término" (com hooks) | Resultado da ferramenta que estava rodando |
| `kill -9`, travamento, queda de energia | Conversa e diário até o último registro gravado | A última linha que estava sendo gravada no instante da queda |

---

## Limitações conhecidas

- **Formatos privados.** O jeito como Claude Code, Codex e OpenCode gravam conversas não é API pública e pode mudar numa atualização. Se mudar, só a seção "Conversa anterior" some (com um aviso); o resto da retomada continua. Abra uma issue.
- **Mesmo sistema.** Programas em Windows e WSL ao mesmo tempo não enxergam as conversas uns dos outros.
- **Sem hooks, depende do agente.** No Codex e no OpenCode, a retomada acontece quando o agente chama `retomar` (as instruções do servidor pedem isso). Os hooks do Codex não são instalados: em algumas máquinas Windows eles abrem janelas de terminal.
- **OpenCode** foi testado com o formato atual do banco (SQLite); o uso real com o programa ainda não foi validado de ponta a ponta.
- **macOS** não foi testado.
- **O hook prova que o estado foi gravado depois do progresso e está íntegro, não que o conteúdo está correto.**

---

## Configuração avançada

Variáveis de ambiente (opcionais):

| Variável | Padrão | Efeito |
|---|---|---|
| `CMEM_BRIDGE_CHARS` | 6000 | Tamanho do trecho da conversa anterior no pacote (~1500 tokens) |
| `CMEM_BRIDGE_DAYS` | 14 | Idade máxima das conversas consideradas |
| `CMEM_NUDGE_EVENTS` | 8 | Ações com efeito colateral sem estado novo até o lembrete (hooks) |
| `CMEM_STOP_MIN_TOOLS` | 12 | Ferramentas sem estado novo até a cobrança no fim do turno (hooks) |
| `CMEM_CKPT_MIN` | 5 | Intervalo mínimo entre checkpoints automáticos, em minutos (hooks) |
| `CMEM_JOURNAL_KEEP` | 400 | Linhas mantidas no diário |
| `CMEM_OPENCODE_DB` | — | Caminho do banco do OpenCode, se não for o padrão |

---

## Desinstalar

```bash
cmem uninstall --simular   # ver o que sai
cmem uninstall             # remove só o que o cmem adicionou
uv tool uninstall coimbra-memory
```

O estado dos projetos (`.git/coimbra-memory/` e `refs/coimbra-memory/*`) é mantido. Para apagar num projeto:

```bash
rm -rf .git/coimbra-memory
git for-each-ref --format='%(refname)' refs/coimbra-memory | xargs -n1 git update-ref -d
```

---

## Desenvolvimento

```bash
git clone https://github.com/elvertoni/coimbra-memory && cd coimbra-memory
bash tests/test-interrupcao.sh      # 41 verificações; Linux, WSL, macOS ou Git Bash no Windows
uvx --from . cmem doctor            # rodar a partir do código-fonte
```

A suíte simula, num repositório temporário, os eventos reais dos hooks, transcrições falsas dos três programas, o servidor MCP e o instalador (com configurações falsas). Ela nunca lê suas conversas nem suas configurações reais.

Estrutura:

```
src/coimbra_memory/
├── core.py      estado, diário, checkpoint, ponte de conversas, pacote de retomada, hooks, CLI
├── mcp.py       servidor MCP (JSON-RPC por stdio, sem SDK)
└── install.py   install / uninstall / doctor
tests/test-interrupcao.sh
```

## Licença

MIT
