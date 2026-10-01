#!/usr/bin/env bash
# Teste de interrupção no meio da tarefa + retomada em outro agente.
# Simula os JSON dos hooks e as transcrições de Claude Code, Codex e OpenCode.
# Roda em Linux, macOS, WSL e Git Bash do Windows. Uso:
#   bash tests/test-interrupcao.sh
set -uo pipefail
here() { pwd -W 2>/dev/null || pwd; }   # caminho nativo: no Git Bash, C:/... em vez de /tmp/...
export PYTHONPATH="$(cd "$(dirname "$0")/../src" && here)"
T=$(cd "$(mktemp -d)" && here); cd "$T"
# Transcrições falsas: nunca ler as conversas reais do usuário.
export CLAUDE_CONFIG_DIR="$T/claude-home" CODEX_HOME="$T/codex-home" XDG_DATA_HOME="$T/xdg"; unset CMEM_OPENCODE_DB
pass=0; fail=0
ok()   { echo "  PASSOU: $1"; pass=$((pass+1)); }
bad()  { echo "  FALHOU: $1"; fail=$((fail+1)); }
cmem() { python3 -m coimbra_memory "$@"; }
ev()   { printf '%s' "$2" | cmem hook "$1"; }   # ev <harness> <json>

git init -q repo && cd repo
P=$(here)
git config user.email t@t; git config user.name t
# Sem .gitignore e sem nenhum arquivo do coimbra-memory: o projeto não é alterado.
mkdir -p app && echo "v1" > app/models.py && git add -A && git commit -qm base
git switch -qc feature/login
SD=$(cmem path)

echo "1. Nomes de branch sem colisão"
git branch feature--login 2>/dev/null
A=$(cmem path); git switch -q feature--login; B=$(cmem path); git switch -q feature/login
[ "$A" != "$B" ] && ok "${A##*/} ≠ ${B##*/}" || bad "colisão $A = $B"

echo "2. Sessão Claude: início, estado escrito pelo agente, trabalho"
ev claude '{"hook_event_name":"SessionStart","source":"startup","cwd":"'"$P"'"}' >/dev/null
cat > "$SD/state.md" <<'EOF'
# Estado — feature/login
## Objetivo
Login por e-mail com bloqueio após 5 tentativas.
## Plano
- [x] modelo LoginAttempt
- [>] migração
- [ ] view e testes
## Etapa atual
Gerar e aplicar a migração 0002.
## Operação em curso
- Ação: python manage.py migrate
- Como verificar: python manage.py showmigrations app | grep 0002
- Seguro repetir? sim
## Próximo passo
View de login usando LoginAttempt.
## Descartado
- django-axes: conflita com o backend customizado.
<!-- fim-do-estado -->
EOF
ev claude '{"hook_event_name":"PostToolUse","tool_name":"Write","tool_use_id":"w1","cwd":"'"$P"'","tool_input":{"file_path":"'"$SD"'/state.md"}}'
[ -f "$SD/state.good.md" ] && ok "estado válido copiado para state.good.md" || bad "state.good.md não criado"

echo "v2" > app/models.py
ev claude '{"hook_event_name":"PostToolUse","tool_name":"Edit","tool_use_id":"e1","cwd":"'"$P"'","tool_input":{"file_path":"app/models.py"}}'
echo "tmp" > app/nova.py   # arquivo não rastreado

echo "3. Operação longa iniciada e processo morto antes do fim (kill -9: nenhum hook final roda)"
ev claude '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_use_id":"b1","cwd":"'"$P"'","tool_input":{"command":"python manage.py migrate --token=abc123secret"}}'
# Sem PostToolUse, sem Stop, sem SessionEnd: simula crash/kill.

echo "4. Agente reescreve o estado e é cortado no meio da gravação"
printf '# Estado\n## Objetivo\nmeio escri' > "$SD/state.md"
ev claude '{"hook_event_name":"PostToolUse","tool_name":"Write","tool_use_id":"w2","cwd":"'"$P"'","tool_input":{"file_path":"'"$SD"'/state.md"}}' 2>/dev/null
[ $? -eq 2 ] && ok "gravação incompleta detectada (exit 2 avisa o modelo)" || bad "estado truncado aceito"

echo "5. Retomada no Codex, na mesma pasta"
OUT=$(ev codex '{"hook_event_name":"SessionStart","source":"startup","cwd":"'"$P"'"}')
grep -q "Usando a última versão íntegra" <<<"$OUT" && ok "usa state.good.md" || bad "não recuperou estado íntegro"
grep -q "Gerar e aplicar a migração 0002" <<<"$OUT" && ok "etapa atual preservada" || bad "etapa perdida"
grep -q "sem confirmação de término" <<<"$OUT" && grep -q "manage.py migrate" <<<"$OUT" && ok "operação sem término sinalizada" || bad "operação pendente não sinalizada"
grep -q "abc123secret" <<<"$OUT" && bad "segredo vazou" || ok "segredo mascarado"
grep -q "app/models.py" <<<"$OUT" && ok "edição posterior ao estado listada" || bad "edição posterior ausente"

echo "6. Limite de uso no Claude (StopFailure) grava checkpoint git sem alterar o projeto"
ev claude '{"hook_event_name":"StopFailure","error_type":"rate_limit","cwd":"'"$P"'"}'
REF=$(git rev-parse -q --verify refs/coimbra-memory/wip/feature/login) && ok "refs/coimbra-memory/wip/feature/login = ${REF:0:10}" || bad "sem checkpoint"
git diff --cached --quiet && git ls-files --others --exclude-standard | grep -q nova.py && ok "índice e branch do usuário intactos" || bad "índice do usuário alterado"
[ -z "$(git status --porcelain | grep -v ' app/')" ] && [ ! -e .gitignore ] && ok "projeto intocado: git status só mostra o trabalho do usuário" || bad "arquivos do coimbra-memory no projeto: $(git status --porcelain)"

echo "7. Retomada em OUTRO worktree"
git worktree add -q ../wt "$REF" 2>/dev/null
( cd ../wt && [ "$(cat app/models.py)" = v2 ] && [ -f app/nova.py ] ) && ok "código pendente (rastreado e novo) presente" || bad "código pendente ausente"
[ ! -e ../wt/.coimbra-memory ] && git cat-file -e refs/coimbra-memory/state/feature/login:state.good.md && ok "estado numa ref separada; checkpoint de código sem sujeira" || bad "estado misturado ao código ou ausente"

echo "8. Outra máquina: clone + fetch das refs + importar"
git clone -q "$P" ../clone && (
  cd ../clone && git fetch -q origin 'refs/coimbra-memory/*:refs/coimbra-memory/*' && git switch -q feature/login
  cmem importar >/dev/null && cmem resume | grep -q "Gerar e aplicar a migração 0002"
) && ok "estado importado e usado na retomada" || bad "importação falhou"
( cd ../clone && ! cmem importar >/dev/null ) && ok "importar recusa sobrescrever estado local sem --forcar" || bad "importar sobrescreveu sem --forcar"

echo "9. Stop cobra atualização após investigação sem mudar código"
cp "$SD/state.good.md" "$SD/state.md"
# Sessão com diretório atual dentro de .git (ex.: inspecionando a pasta do estado): o hook ainda acha o repositório.
N0=$(grep -c state_ok "$SD/journal.jsonl")
ev claude '{"hook_event_name":"PostToolUse","tool_name":"Write","tool_use_id":"w3","cwd":"'"$SD"'","tool_input":{"file_path":"'"$SD"'/state.md"}}'
[ "$(grep -c state_ok "$SD/journal.jsonl")" -gt "$N0" ] && ok "hook funciona com o diretório atual dentro de .git" || bad "hook ignorou evento com cwd dentro de .git"
ev codex '{"hook_event_name":"PostToolUse","tool_name":"apply_patch","tool_use_id":"p1","cwd":"'"$P"'","tool_input":{"command":"*** Begin Patch\n*** Update File: '"$SD"'/state.md\n*** End Patch"}}'
for i in $(seq 1 12); do ev codex '{"hook_event_name":"PostToolUse","tool_name":"Read","tool_use_id":"r'$i'","cwd":"'"$P"'","tool_input":{"file_path":"x.py"}}'; done
S=$(ev codex '{"hook_event_name":"Stop","stop_hook_active":false,"cwd":"'"$P"'"}')
grep -q '"decision": "block"' <<<"$S" && ok "Stop bloqueia após 12 leituras sem estado novo" || bad "Stop liberou: $S"
S2=$(ev codex '{"hook_event_name":"Stop","stop_hook_active":true,"cwd":"'"$P"'"}')
[ -z "$S2" ] && ok "sem loop quando stop_hook_active=true" || bad "loop no Stop"

age() { python3 -c 'import os,sys,time; t=time.time()-int(sys.argv[2])*60; os.utime(sys.argv[1],(t,t))' "$1" "$2"; }  # age <arquivo> <minutos atrás>
ENC=$(python3 -c 'import os,re,sys; print(re.sub(r"[^A-Za-z0-9]","-",os.path.realpath(sys.argv[1])))' "$P")
CP="$CLAUDE_CONFIG_DIR/projects/$ENC"; CX="$CODEX_HOME/sessions/2026/10/01"; mkdir -p "$CP" "$CX"

echo "10. Ponte de conversas: Claude bate no limite de uso sem escrever estado; Codex retoma"
cat > "$CP/sess-claude-1.jsonl" <<EOF
{"type":"user","cwd":"$P","gitBranch":"feature/login","timestamp":"2026-10-01T13:00:00Z","message":{"role":"user","content":"<system-reminder>LIXO-INJETADO</system-reminder>implementar bloqueio após 5 tentativas no login"}}
{"type":"assistant","cwd":"$P","gitBranch":"feature/login","timestamp":"2026-10-01T13:01:00Z","message":{"role":"assistant","content":[{"type":"text","text":"Vou criar a migração 0002 e depois a view."},{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"export API_TOKEN=supersecreto123 && python manage.py migrate"}}]}}
{"type":"user","cwd":"$P","gitBranch":"feature/login","timestamp":"2026-10-01T13:02:00Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"SAIDA-DA-FERRAMENTA"}]}}
{"type":"assistant","cwd":"$P","gitBranch":"feature/login","isApiErrorMessage":true,"timestamp":"2026-10-01T13:03:00Z","message":{"role":"assistant","model":"<synthetic>","content":[{"type":"text","text":"You've hit your session limit · resets 5pm"}]}}
EOF
cat > "$CP/sess-outra-branch.jsonl" <<EOF
{"type":"user","cwd":"$P","gitBranch":"main","timestamp":"2026-10-01T13:10:00Z","message":{"role":"user","content":"PEDIDO-DE-OUTRA-BRANCH"}}
EOF
age "$CP/sess-claude-1.jsonl" 60; age "$CP/sess-outra-branch.jsonl" 50
OUT=$(ev codex '{"hook_event_name":"SessionStart","source":"startup","cwd":"'"$P"'"}')
grep -q "Conversa anterior — claude.*session limit" <<<"$OUT" && ok "conversa do Claude e motivo do fim presentes" || bad "conversa do Claude ausente"
grep -q "implementar bloqueio após 5 tentativas" <<<"$OUT" && grep -q "migração 0002" <<<"$OUT" && ok "último pedido e última resposta presentes" || bad "pedido/resposta ausentes"
grep -qE "LIXO-INJETADO|SAIDA-DA-FERRAMENTA|supersecreto123" <<<"$OUT" && bad "contexto injetado, saída de ferramenta ou segredo vazou" || ok "sem contexto injetado, saída de ferramenta ou segredo"
grep -q "PEDIDO-DE-OUTRA-BRANCH" <<<"$OUT" && bad "conversa de outra branch incluída" || ok "conversa de outra branch ignorada"

echo "11. Codex é cortado no meio do turno; volta ao Claude em sessão nova"
cat > "$CX/rollout-2026-10-01T14-00-00-cx1.jsonl" <<EOF
{"type":"session_meta","timestamp":"2026-10-01T14:00:00Z","payload":{"id":"cx1","cwd":"$P"}}
{"type":"response_item","timestamp":"2026-10-01T14:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"# AGENTS.md instructions LIXO-AGENTS"}]}}
{"type":"event_msg","timestamp":"2026-10-01T14:00:02Z","payload":{"type":"task_started","turn_id":"u1"}}
{"type":"response_item","timestamp":"2026-10-01T14:00:03Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"continue: criar a view de login"}]}}
{"type":"response_item","timestamp":"2026-10-01T14:00:04Z","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Criando views.py com LoginView."}]}}
{"type":"response_item","timestamp":"2026-10-01T14:00:05Z","payload":{"type":"function_call","name":"shell_command","arguments":"{\"command\":\"python manage.py test app\"}","call_id":"c1"}}
EOF
cat > "$CX/rollout-2026-10-01T14-30-00-cx2.jsonl" <<EOF
{"type":"session_meta","timestamp":"2026-10-01T14:30:00Z","payload":{"id":"cx2","cwd":"/outro/projeto"}}
{"type":"response_item","timestamp":"2026-10-01T14:30:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"CONVERSA-DE-OUTRO-PROJETO"}]}}
EOF
cat > "$CP/sess-atual.jsonl" <<EOF
{"type":"user","cwd":"$P","gitBranch":"feature/login","timestamp":"2026-10-01T15:00:00Z","message":{"role":"user","content":"CONVERSA-ATUAL"}}
EOF
age "$CX/rollout-2026-10-01T14-00-00-cx1.jsonl" 20
OUT=$(ev claude '{"hook_event_name":"SessionStart","source":"startup","session_id":"sess-atual","transcript_path":"'"$CP/sess-atual.jsonl"'","cwd":"'"$P"'"}')
grep -q "Conversa anterior — codex.*turno cortado" <<<"$OUT" && grep -q "criar a view de login" <<<"$OUT" && ok "conversa do Codex com turno cortado sinalizado" || bad "conversa do Codex ausente"
grep -qE "CONVERSA-ATUAL|CONVERSA-DE-OUTRO-PROJETO|LIXO-AGENTS" <<<"$OUT" && bad "sessão atual, outro projeto ou AGENTS.md incluídos" || ok "sessão atual, outro projeto e AGENTS.md excluídos"
grep -q "Antes dessa: claude.*implementar bloqueio" <<<"$OUT" && ok "sessão anterior do Claude listada" || bad "sessão anterior do Claude ausente"

echo "12. OpenCode (SQLite) fica sem saldo no meio da tarefa; Codex retoma"
mkdir -p "$XDG_DATA_HOME/opencode"
python3 - "$XDG_DATA_HOME/opencode/opencode.db" "$P" <<'PY'
import json, sqlite3, sys, time
con = sqlite3.connect(sys.argv[1]); cwd = sys.argv[2]; t = int(time.time() * 1000) + 60000
con.executescript("""
create table session (id text primary key, parent_id text, directory text, time_updated integer, time_archived integer);
create table message (id text primary key, session_id text, time_created integer, data text);
create table part (id text primary key, message_id text, session_id text, time_created integer, data text);""")
con.execute("insert into session values ('ses_oc1', null, ?, ?, null)", (cwd, t))
con.execute("insert into session values ('ses_sub', 'ses_oc1', ?, ?, null)", (cwd, t + 1000))
msgs = [
    ("msg_1", "ses_oc1", {"role": "user", "time": {"created": t - 3000}},
     [{"type": "text", "text": "migrar views para class-based"}, {"type": "text", "synthetic": True, "text": "SINTETICO-OPENCODE"}]),
    ("msg_2", "ses_oc1", {"role": "assistant", "time": {"created": t - 2000, "completed": t - 1500}, "finish": "tool-calls"},
     [{"type": "reasoning", "text": "RACIOCINIO-OPENCODE"}, {"type": "text", "text": "Convertendo LoginView."},
      {"type": "tool", "tool": "bash", "state": {"status": "completed", "input": {"command": "python manage.py test"}, "output": "SAIDA-OPENCODE"}}]),
    ("msg_3", "ses_oc1", {"role": "assistant", "time": {"created": t - 1000},
                          "error": {"name": "APIError", "data": {"message": "Insufficient balance"}}}, []),
    ("msg_4", "ses_sub", {"role": "user", "time": {"created": t}}, [{"type": "text", "text": "SUBAGENTE-OPENCODE"}]),
]
for mid, sid, data, parts in msgs:
    con.execute("insert into message values (?, ?, ?, ?)", (mid, sid, data["time"]["created"], json.dumps(data)))
    for i, p in enumerate(parts):
        con.execute("insert into part values (?, ?, ?, ?, ?)", (f"prt_{mid}_{i}", mid, sid, data["time"]["created"] + i, json.dumps(p)))
con.commit()
PY
OUT=$(ev codex '{"hook_event_name":"SessionStart","source":"startup","cwd":"'"$P"'"}')
grep -q "Conversa anterior — opencode.*Insufficient balance" <<<"$OUT" && grep -q "migrar views para class-based" <<<"$OUT" && grep -q "python manage.py test" <<<"$OUT" && ok "conversa do OpenCode, ferramenta e motivo do fim presentes" || bad "conversa do OpenCode ausente"
grep -qE "SINTETICO-OPENCODE|RACIOCINIO-OPENCODE|SAIDA-OPENCODE|SUBAGENTE-OPENCODE" <<<"$OUT" && bad "texto sintético, raciocínio, saída ou subagente vazou" || ok "sem texto sintético, raciocínio, saída de ferramenta ou subagente"
OUT=$(cmem resume ses_oc1)
grep -q "Conversa anterior — opencode" <<<"$OUT" && bad "resume <sessão> não excluiu a sessão atual" || ok "resume <sessão> exclui a sessão em andamento"

echo "13. Servidor MCP: protocolo, gravação parcial do estado e retomada"
OUT=$(python3 - "$P" <<'PY'
import json, subprocess, sys
root = sys.argv[1]
p = subprocess.Popen([sys.executable, "-m", "coimbra_memory", "mcp"], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, text=True, encoding="utf-8", cwd=root)
def req(i, method, params=None):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
    p.stdin.flush()
    return json.loads(p.stdout.readline())  # cada linha precisa ser JSON válido
def tool(i, name, args):
    r = req(i, "tools/call", {"name": name, "arguments": args})["result"]
    return r["isError"], r["content"][0]["text"]
init = req(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})["result"]
p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
names = sorted(t["name"] for t in req(2, "tools/list")["result"]["tools"])
print("INIT", init["serverInfo"]["name"], "instr" if "salvar_estado" in init["instructions"] else "sem-instr", ",".join(names))
err, txt = tool(3, "salvar_estado", {"diretorio": root, "secoes": {"Proximo passo": "Escrever testes da LoginView.", "Etapa atual": "MIGRACAO-OK"}, "autor": "teste"})
print("PARCIAL", "erro" if err else "ok")
err, txt = tool(4, "salvar_estado", {"diretorio": root, "secoes": {"Objetivo": ""}})
print("INVALIDO", "recusado" if err and "NÃO gravado" in txt else "aceito")
err, txt = tool(5, "retomar", {"diretorio": root})
print("RETOMAR", "ok" if "## Retomada" in txt and "MIGRACAO-OK" in txt and "Login por e-mail" in txt
      and "Escrever testes da LoginView." in txt and txt.count("## Próximo passo") == 1 else "falhou")
err, txt = tool(6, "retomar", {"diretorio": root + "/nao-existe"})
print("FORA", "erro" if err else "aceito")
print("DESCONHECIDO", req(7, "metodo/inexistente").get("error", {}).get("code"))
p.stdin.close(); p.wait(5)
PY
)
grep -q "INIT coimbra-memory instr checkpoint,conversa,retomar,salvar_estado" <<<"$OUT" && ok "initialize com instruções e 4 ferramentas" || bad "initialize/tools: $OUT"
grep -q "PARCIAL ok" <<<"$OUT" && grep -q "RETOMAR ok" <<<"$OUT" && ok "salvar_estado parcial preserva o resto e aparece no retomar" || bad "gravação parcial: $OUT"
grep -q "INVALIDO recusado" <<<"$OUT" && ok "estado inválido recusado sem gravar" || bad "estado inválido aceito"
grep -q "FORA erro" <<<"$OUT" && grep -q "DESCONHECIDO -32601" <<<"$OUT" && ok "erros voltam pelo protocolo sem derrubar o servidor" || bad "tratamento de erro: $OUT"
grep -q "state_ok" "$SD/journal.jsonl" && grep -q '"h": "mcp"' "$SD/journal.jsonl" && ok "gravação via MCP registrada no diário (Stop enxerga)" || bad "diário sem state_ok do MCP"

echo "14. Instalador: simulação, instalação com hooks, idempotência e remoção sem perder o que já existia"
export XDG_CONFIG_HOME="$T/xdg-config" CMEM_LAUNCHER="C:/Program Files/cmem/cmem.exe" CMEM_CLAUDE_CLI=none
mkdir -p "$CODEX_HOME" "$XDG_CONFIG_HOME/opencode"
cat > "$CLAUDE_CONFIG_DIR/.claude.json" <<'EOF'
{"numStartups": 5, "mcpServers": {"outro": {"type": "stdio", "command": "outro"}}}
EOF
cat > "$CLAUDE_CONFIG_DIR/settings.json" <<'EOF'
{"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo OUTRO-HOOK"}]}]}}
EOF
cat > "$CODEX_HOME/config.toml" <<'EOF'
model = "gpt-5"
# comentário com acento: configuração
[mcp_servers.outro]
command = "outro"
EOF
printf '{\n  "$schema": "https://opencode.ai/config.json",\n  "mcp": {"outro": {"type": "local", "command": ["outro"]},},\n}\n' > "$XDG_CONFIG_HOME/opencode/opencode.jsonc"
snap() { cat "$CLAUDE_CONFIG_DIR/.claude.json" "$CLAUDE_CONFIG_DIR/settings.json" "$CODEX_HOME/config.toml" "$XDG_CONFIG_HOME/opencode/opencode.jsonc" | python3 -c 'import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())'; }
H0=$(snap); SIM=$(cmem install --hooks --simular); H1=$(snap)
[ "$H0" = "$H1" ] && grep -q "SIMULAÇÃO" <<<"$SIM" && ok "--simular não altera nenhum arquivo" || bad "simulação alterou arquivos"
INS=$(cmem install --hooks)
CHK=$(python3 - "$CLAUDE_CONFIG_DIR" "$CODEX_HOME" "$XDG_CONFIG_HOME" <<'PY'
import json, sys, tomllib
c, x, o = sys.argv[1:4]
cj = json.load(open(f"{c}/.claude.json", encoding="utf-8"))
st = json.load(open(f"{c}/settings.json", encoding="utf-8"))
tm = tomllib.load(open(f"{x}/config.toml", "rb"))
oc = json.load(open(f"{o}/opencode/opencode.jsonc", encoding="utf-8"))
ours = lambda gs: sum(" hook claude" in h["command"] for g in gs for h in g["hooks"])
print("CLAUDE", cj["mcpServers"]["coimbra-memory"]["command"] == "C:/Program Files/cmem/cmem.exe" and "outro" in cj["mcpServers"] and cj["numStartups"] == 5)
print("HOOKS", sum(ours(g) for g in st["hooks"].values()) == 8 and st["model"] == "opus" and any("OUTRO-HOOK" in h["command"] for g in st["hooks"]["Stop"] for h in g["hooks"]))
print("CODEX", tm["mcp_servers"]["coimbra-memory"]["args"] == ["mcp"] and tm["mcp_servers"]["coimbra-memory"]["default_tools_approval_mode"] == "approve" and "outro" in tm["mcp_servers"] and tm["model"] == "gpt-5")
print("OPENCODE", oc["mcp"]["coimbra-memory"]["command"] == ["C:/Program Files/cmem/cmem.exe", "mcp"] and "outro" in oc["mcp"])
PY
)
grep -q "CLAUDE True" <<<"$CHK" && grep -q "HOOKS True" <<<"$CHK" && ok "Claude Code: MCP e 8 hooks adicionados, configurações existentes preservadas" || bad "Claude Code: $CHK $INS"
grep -q "CODEX True" <<<"$CHK" && ok "Codex: TOML válido, aprovação liberada, servidores existentes preservados" || bad "Codex: $CHK $INS"
grep -q "OPENCODE True" <<<"$CHK" && ok "OpenCode: JSONC lido (vírgulas finais) e servidores existentes preservados" || bad "OpenCode: $CHK $INS"
[ "$(ls "$CLAUDE_CONFIG_DIR"/*.cmem-backup-* "$CLAUDE_CONFIG_DIR"/.*.cmem-backup-* "$CODEX_HOME"/*.cmem-backup-* "$XDG_CONFIG_HOME"/opencode/*.cmem-backup-* 2>/dev/null | wc -l)" -ge 4 ] && ok "backup de cada arquivo alterado" || bad "backups ausentes"
H2=$(snap); AGAIN=$(cmem install --hooks); H3=$(snap)
[ "$H2" = "$H3" ] && [ "$(grep -c "já configurado" <<<"$AGAIN")" -ge 4 ] && ok "segunda instalação não altera nada" || bad "instalação não é idempotente: $AGAIN"
cmem uninstall >/dev/null
CHK=$(python3 - "$CLAUDE_CONFIG_DIR" "$CODEX_HOME" "$XDG_CONFIG_HOME" <<'PY'
import json, sys, tomllib
c, x, o = sys.argv[1:4]
cj = json.load(open(f"{c}/.claude.json", encoding="utf-8"))
st = json.load(open(f"{c}/settings.json", encoding="utf-8"))
raw = open(f"{x}/config.toml", encoding="utf-8").read(); tm = tomllib.loads(raw)
oc = json.load(open(f"{o}/opencode/opencode.jsonc", encoding="utf-8"))
print("LIMPO", "coimbra-memory" not in cj["mcpServers"] and "outro" in cj["mcpServers"]
      and list(st["hooks"]) == ["Stop"] and len(st["hooks"]["Stop"]) == 1
      and "coimbra-memory" not in raw and "outro" in tm["mcp_servers"] and "configuração" in raw
      and "coimbra-memory" not in oc["mcp"] and "outro" in oc["mcp"])
PY
)
grep -q "LIMPO True" <<<"$CHK" && ok "uninstall remove só o que o cmem adicionou" || bad "uninstall: $CHK"
printf '{\n  // comentário do usuário\n  "mcp": {}\n}\n' > "$XDG_CONFIG_HOME/opencode/opencode.jsonc"
H4=$(snap); OUT=$(cmem install); H5=$(snap)
grep -q "OpenCode: ⚠ o arquivo tem comentários" <<<"$OUT" && grep -q "// comentário do usuário" "$XDG_CONFIG_HOME/opencode/opencode.jsonc" && ok "OpenCode com comentários: não edita e mostra o trecho para colar" || bad "comentários do OpenCode: $OUT"

echo; echo "Resultado: $pass passaram, $fail falharam"; cd /; rm -rf "$T"
[ $fail -eq 0 ]
