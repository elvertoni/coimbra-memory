#!/usr/bin/env python3
"""coimbra-memory (cmem) — retoma a tarefa quando o agente de código cai.

Sem dependências. Chamado pelos hooks de Claude Code, Codex e OpenCode, pelo
servidor MCP ou manualmente. Não altera nenhum arquivo do projeto: o estado
fica em .git/coimbra-memory/, que o git nunca versiona.

Subcomandos:
  install [--hooks] [--simular]   registra o servidor MCP no Claude Code, Codex e OpenCode
  uninstall [--simular]           desfaz o install (o estado dos projetos é mantido)
  doctor           diagnóstico da instalação e do repositório atual
  hook <harness>   lê o JSON do evento no stdin e age conforme o evento
  mcp              servidor MCP (stdio): retomar, salvar_estado, conversa, checkpoint
  resume [sessão]  imprime o pacote de retomada (curto) da branch atual; a ponte
                   ignora a conversa com esse id (a sessão em andamento)
  checkpoint [motivo]   grava código + estado em refs/coimbra-memory/wip/<branch>
  importar [ref] [--forcar]  traz o estado de um checkpoint (outra máquina ou clone)
  validate         valida o state.md da branch atual
  path             imprime a pasta de estado da branch atual
  modelo           imprime o modelo de state.md
  conversa [caracteres]  fim da última conversa (qualquer harness) neste repositório

O que este script NÃO faz: escrever o resumo semântico. Ele só registra fatos
mecânicos (ferramenta, alvo, início/fim, horário) e cobra o agente. A ponte de
conversas apenas lê o que o próprio harness já gravou em disco; quem interpreta
é o agente que retoma.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata

# ---------------------------------------------------------------- parâmetros
NAME = "coimbra-memory"
WIP_REF = f"refs/{NAME}/wip/"     # checkpoints: refs/coimbra-memory/wip/<branch>
CKPT_DIR = f".{NAME}"             # pasta do estado dentro da árvore do checkpoint


def param(name, default):
    return int(os.environ.get("CMEM_" + name, default))


NUDGE_EVENTS = param("NUDGE_EVENTS", 8)      # eventos relevantes sem estado -> lembrete
STOP_MIN_TOOLS = param("STOP_MIN_TOOLS", 12)  # investigação longa sem estado -> cobra no Stop
CKPT_MIN = param("CKPT_MIN", 5)              # intervalo mínimo entre checkpoints git (min)
JOURNAL_KEEP = param("JOURNAL_KEEP", 400)     # linhas mantidas no diário
BRIDGE_CHARS = param("BRIDGE_CHARS", 6000)    # fim da conversa anterior no pacote (~1500 tokens)
BRIDGE_DAYS = param("BRIDGE_DAYS", 14)        # idade máxima das conversas consideradas
STATE_FILES = ("state.md", "state.good.md", "journal.jsonl")  # o que vai no checkpoint
TAIL_BYTES = 4 * 1024 * 1024                                     # só o fim de cada transcrição é lido
STATE_MAX_LINES = 80
REQUIRED = ["## Objetivo", "## Etapa atual", "## Operação em curso", "## Próximo passo"]
END_MARK = "<!-- fim-do-estado -->"
TEMPLATE = """# Estado — <branch>
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
"""

SIDE_EFFECT = re.compile(r"^(bash|shell|exec_command|apply_patch|edit|write|multiedit|notebookedit|patch)$", re.I)
EDIT_TOOLS = re.compile(r"^(apply_patch|edit|write|multiedit|notebookedit|patch)$", re.I)
PLAN_TOOLS = re.compile(r"(todo|update_plan|^task(create|update)?$)", re.I)
SECRET = re.compile(
    r"(bearer\s+\S+|(?:api[_-]?key|token|secret|passw(?:or)?d|pwd)\s*[=:]\s*\S+"
    r"|sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]+)",
    re.I,
)


# ---------------------------------------------------------------- utilidades
def run(cmd, cwd, env=None):
    # utf-8 explícito: no Windows o padrão seria cp1252 e quebraria nomes com acento
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return r.returncode, r.stdout.strip()


def git(root, *args, env=None):
    code, out = run(["git", *args], root, env)
    return out if code == 0 else ""


def repo_root(cwd):
    code, out = run(["git", "rev-parse", "--show-toplevel"], cwd or os.getcwd())
    return out if code == 0 else None


def current_branch(root):
    return git(root, "branch", "--show-current") or "detached"


def encode(branch):
    # Sem ambiguidade: '%' vira %25 antes, '/' vira %2F.
    return branch.replace("%", "%25").replace("/", "%2F")


def common_dir(root):
    """Pasta .git compartilhada por todos os worktrees do repositório."""
    d = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.normpath(d or os.path.join(root, ".git"))


def state_dir(root, branch=None, create=True):
    # Dentro do .git: nunca versionado, nada a acrescentar ao .gitignore, e os
    # worktrees do mesmo repositório enxergam o mesmo estado por branch.
    d = os.path.join(common_dir(root), NAME, "state", encode(branch or current_branch(root)))
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def slash(p):
    # Barras normais: valem em todos os sistemas e não viram escape dentro de JSON.
    return p.replace(os.sep, "/")


def atomic_write(path, text):
    """Grava em arquivo temporário e renomeia: ou fica a versão antiga, ou a nova."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def redact(s):
    return SECRET.sub("[REDACTED]", s)


def short(s, n=200):
    s = redact(" ".join(str(s).split()))
    return s if len(s) <= n else s[: n - 1] + "…"


def now():
    return time.time()


def stamp(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or now()))


# ---------------------------------------------------------------- diário
def journal_path(d):
    return os.path.join(d, "journal.jsonl")


def append(d, rec):
    rec.setdefault("ts", now())
    line = json.dumps(rec, ensure_ascii=False)
    with open(journal_path(d), "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    rotate(d)


def read_journal(d):
    out = []
    try:
        with open(journal_path(d), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass  # linha truncada por interrupção: ignora
    except FileNotFoundError:
        pass
    return out


def rotate(d):
    recs = read_journal(d)
    if len(recs) <= JOURNAL_KEEP * 2:
        return
    old, keep = recs[:-JOURNAL_KEEP], recs[-JOURNAL_KEEP:]
    # Preserva o último state_ok, mesmo que antigo.
    last_ok = [r for r in old if r.get("ev") == "state_ok"][-1:]
    atomic_write(journal_path(d), "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in last_ok + keep))


# ---------------------------------------------------------------- normalização
def describe(tool, tinput):
    """Resumo mecânico do alvo da ferramenta, sem saída e sem segredos."""
    if not isinstance(tinput, dict):
        return short(tinput)
    if PLAN_TOOLS.search(tool or ""):
        return short(json.dumps(tinput, ensure_ascii=False), 600)
    cmd = tinput.get("command") or tinput.get("cmd")
    if isinstance(cmd, list):
        cmd = " ".join(map(str, cmd))
    if cmd and "*** Begin Patch" in str(cmd):
        files = re.findall(r"\*\*\* (?:Add|Update|Delete) File: (.+)", str(cmd))
        return "patch: " + ", ".join(files[:8])
    for k in ("file_path", "filePath", "path", "notebook_path"):
        if tinput.get(k):
            return str(tinput[k])
    if cmd:
        return short(cmd)
    for k in ("pattern", "query", "url"):
        if tinput.get(k):
            return f"{k}: {short(tinput[k], 120)}"
    return short(",".join(sorted(tinput.keys())), 80)


def touched_state(tool, tinput, root):
    if not isinstance(tinput, dict):
        return False
    blob = json.dumps(tinput, ensure_ascii=False)
    return EDIT_TOOLS.match(tool or "") is not None and "state.md" in blob and NAME in blob


# ---------------------------------------------------------------- estado semântico
def validate_text(text):
    problems = [f"falta a seção '{h}'" for h in REQUIRED if h not in text]
    if END_MARK not in text:
        problems.append(f"falta o marcador final {END_MARK} (gravação incompleta?)")
    n = text.count("\n")
    if n > STATE_MAX_LINES:
        problems.append(f"{n} linhas (máx. {STATE_MAX_LINES}): resuma")
    return problems


def validate(root):
    d = state_dir(root)
    p = os.path.join(d, "state.md")
    if not os.path.exists(p):
        return None, ["state.md não existe"]
    with open(p, encoding="utf-8") as f:
        text = f.read()
    return text, validate_text(text)


def on_state_written(root, d, harness):
    text, problems = validate(root)
    if problems:
        append(d, {"ev": "state_invalid", "h": harness, "why": "; ".join(problems)})
        return problems
    atomic_write(os.path.join(d, "state.good.md"), text)
    append(d, {"ev": "state_ok", "h": harness, "head": git(root, "rev-parse", "--short", "HEAD")})
    return []


def plain(name):
    """Chave de comparação de seção: sem '#', caixa ou acento ('Proximo passo' = '## Próximo passo')."""
    s = unicodedata.normalize("NFKD", name.strip().lstrip("#").strip().casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


KNOWN_SECTIONS = {plain(h): h[3:] for h in re.findall(r"^## .+$", TEMPLATE, re.M)}


def update_state(root, sections, author=None, harness="mcp"):
    """Substitui só as seções informadas no state.md, valida e grava de forma atômica.

    Seção com texto vazio é removida. Se o resultado for inválido, nada é gravado.
    Devolve a lista de problemas (vazia = gravado).
    """
    d = state_dir(root)
    p = os.path.join(d, "state.md")
    text = ""
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            text = f.read()
    head, order, body = [], [], {}
    current = None
    for line in text.replace(END_MARK, "").splitlines():
        m = re.match(r"^## (.+)$", line)
        if m:
            current = m.group(1).strip()
            order.append(current)
            body[current] = []
        elif current is None:
            head.append(line)
        else:
            body[current].append(line)
    for name, value in sections.items():
        key = next((n for n in order if plain(n) == plain(name)), None)
        if key is None:
            key = KNOWN_SECTIONS.get(plain(name), name.strip().lstrip("#").strip())
            order.append(key)
        if str(value).strip():
            body[key] = [str(value).strip(), ""]
        else:
            order.remove(key)
            body.pop(key, None)
    head = [l for l in head if l.strip() and not l.startswith("_Atualizado:")]
    if not any(l.startswith("# ") for l in head):
        head.insert(0, f"# Estado — {current_branch(root)}")
    head.insert(1, f"_Atualizado: {stamp()[:16]} · {author or harness}_")
    out = head + [""]
    for name in order:
        out.append(f"## {name}")
        out.extend(body[name] or [""])
    new = "\n".join(out).rstrip() + "\n" + END_MARK + "\n"
    problems = validate_text(new)
    if problems:
        return problems
    atomic_write(p, new)
    return on_state_written(root, d, harness)


def since_last_state(recs):
    idx = max((i for i, r in enumerate(recs) if r.get("ev") == "state_ok"), default=-1)
    return recs[idx + 1 :], (recs[idx] if idx >= 0 else None)


def open_operations(recs):
    ended = {r.get("id") for r in recs if r.get("ev") == "end" and r.get("id")}
    return [r for r in recs if r.get("ev") == "start" and r.get("id") and r["id"] not in ended]


def progress_counts(events):
    ends = [r for r in events if r.get("ev") == "end"]
    side = [r for r in ends if SIDE_EFFECT.match(r.get("tool") or "")]
    return len(ends), len(side)


# ---------------------------------------------------------------- checkpoint git
def commit_tree(root, env, msg, parent):
    tree = git(root, "write-tree", env=env)
    if not tree:
        return None
    return git(root, "commit-tree", tree, *(["-p", parent] if parent else []), "-m", msg) or None


def checkpoint(root, reason, force=False):
    """Dois commits fora de qualquer branch, ambos com índice temporário (o índice
    e a branch do usuário não são tocados):

    refs/coimbra-memory/wip/<branch>    working tree, inclusive arquivos novos não
                                        ignorados; pode virar branch sem sujeira
    refs/coimbra-memory/state/<branch>  só os arquivos de estado; histórico próprio
    """
    d = state_dir(root)
    marker = os.path.join(d, ".last-ckpt")
    if not force and os.path.exists(marker) and now() - os.path.getmtime(marker) < CKPT_MIN * 60:
        return None
    gitdir = git(root, "rev-parse", "--absolute-git-dir")
    if not gitdir:
        return None
    branch = current_branch(root)
    msg = f"{NAME} checkpoint: {reason} @ {stamp()}"
    env = dict(os.environ, GIT_INDEX_FILE=os.path.join(gitdir, f"{NAME}-index"))
    head = git(root, "rev-parse", "--verify", "-q", "HEAD")
    run(["git", "read-tree", head or "--empty"], root, env)
    run(["git", "add", "-A", "."], root, env)
    commit = commit_tree(root, env, msg, head)
    if not commit:
        return None
    run(["git", "update-ref", "--create-reflog", "-m", reason, WIP_REF + branch, commit], root)

    state_ref = f"refs/{NAME}/state/{branch}"
    run(["git", "read-tree", "--empty"], root, env)
    for name in STATE_FILES:
        p = os.path.join(d, name)
        blob = git(root, "hash-object", "-w", "--", p) if os.path.exists(p) else ""
        if blob:
            run(["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}"], root, env)
    state_commit = commit_tree(root, env, msg, git(root, "rev-parse", "--verify", "-q", state_ref))
    if state_commit:
        run(["git", "update-ref", "--create-reflog", "-m", reason, state_ref, state_commit], root)

    with open(marker, "w") as f:
        f.write(commit)
    append(d, {"ev": "checkpoint", "why": reason, "commit": commit[:10]})
    return commit


def import_state(root, ref=None, force=False):
    """Traz o estado gravado por um checkpoint (outro clone, outra máquina)."""
    branch = current_branch(root)
    ref = ref or f"refs/{NAME}/state/{branch}"
    d = state_dir(root)
    if os.path.exists(os.path.join(d, "state.md")) and not force:
        return f"já existe estado local para '{branch}' em {d}; use --forcar para substituir"
    got = []
    for name in STATE_FILES:
        r = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=root, capture_output=True)
        if r.returncode == 0:
            atomic_write(os.path.join(d, name), r.stdout.decode("utf-8", "replace"))
            got.append(name)
    if not got:
        return f"nada encontrado em {ref}"
    return f"importado de {ref}: {', '.join(got)}"


# ---------------------------------------------------------------- ponte de conversas
# Claude Code e Codex gravam a conversa inteira em disco enquanto ela acontece,
# inclusive quando a sessão morre por limite de uso. Quem retoma lê o fim dela.
def claude_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def norm(p):
    return os.path.normcase(os.path.realpath(p))


def inside(path, root):
    if not path:
        return False
    a, b = norm(path), norm(root)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def tail_records(path, nbytes=TAIL_BYTES):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            lines = f.read().split(b"\n")
    except OSError:
        return []
    if size > nbytes:
        lines = lines[1:]  # primeira linha cortada pelo seek
    out = []
    for raw in lines:
        try:
            out.append(json.loads(raw))
        except ValueError:
            pass
    return out


def head_records(path, n=30):
    out = []
    try:
        with open(path, "rb") as f:
            for _ in range(n):
                raw = f.readline()
                if not raw:
                    break
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def find_transcripts(root):
    """[(mtime, harness, caminho)] das conversas deste repositório, mais recentes primeiro."""
    limit = now() - BRIDGE_DAYS * 86400
    found = []
    projects = os.path.join(claude_home(), "projects")
    prefix = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(root))
    try:
        dirs = [x for x in os.listdir(projects) if x == prefix or x.startswith(prefix + "-")]
    except OSError:
        dirs = []
    for x in dirs:
        base = os.path.join(projects, x)
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if name.endswith(".jsonl") and os.path.getmtime(p) >= limit:
                cwd = next((r["cwd"] for r in head_records(p) if r.get("cwd")), None)
                if cwd is None or inside(cwd, root):  # prefixo do nome pode colidir: confere o cwd
                    found.append((os.path.getmtime(p), "claude", p))
    for dirpath, _, files in os.walk(os.path.join(codex_home(), "sessions")):
        for name in files:
            p = os.path.join(dirpath, name)
            if name.startswith("rollout-") and name.endswith(".jsonl") and os.path.getmtime(p) >= limit:
                meta = head_records(p, 1)
                cwd = (meta[0].get("payload") or {}).get("cwd") if meta else None
                if inside(cwd, root):
                    found.append((os.path.getmtime(p), "codex", p))
    found.extend(opencode_sessions(root, limit))
    found.sort(reverse=True)
    return found


# OpenCode guarda as conversas em SQLite. O banco também tem tabelas de contas e
# credenciais: só session, message e part são lidas, sempre em modo somente leitura.
def opencode_db():
    if os.environ.get("CMEM_OPENCODE_DB"):
        return os.environ["CMEM_OPENCODE_DB"]
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "opencode", "opencode.db")


def opencode_connect(db):
    import pathlib
    import sqlite3
    return sqlite3.connect(pathlib.Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def opencode_sessions(root, limit):
    db = opencode_db()
    if not os.path.exists(db):
        return []
    con = opencode_connect(db)
    try:
        rows = con.execute("select id, directory, time_updated from session where parent_id is null "
                           "and time_archived is null and time_updated >= ?", (int(limit * 1000),)).fetchall()
    finally:
        con.close()
    # Subagentes (parent_id) ficam de fora: a conversa que importa é a principal.
    return [(t / 1000, "opencode", f"{db}#{sid}") for sid, directory, t in rows if inside(directory, root)]


def opencode_records(ref, max_messages=80):
    db, sid = ref.rsplit("#", 1)
    con = opencode_connect(db)
    try:
        msgs = con.execute("select id, data from message where session_id = ? "
                           "order by time_created desc, id desc limit ?", (sid, max_messages)).fetchall()[::-1]
        if not msgs:
            return []
        first = json.loads(msgs[0][1]).get("time", {}).get("created", 0)
        parts = {}
        for mid, data in con.execute("select message_id, data from part where session_id = ? and time_created >= ? "
                                     "order by time_created, id", (sid, first)):
            parts.setdefault(mid, []).append(json.loads(data))
    finally:
        con.close()
    return [dict(json.loads(data), parts=parts.get(mid, [])) for mid, data in msgs]


def parse_opencode(msgs):
    items, unfinished = [], False
    for m in msgs:
        ts, role = (m.get("time") or {}).get("created"), m.get("role")
        for p in m["parts"]:
            kind = p.get("type")
            if kind == "text" and not p.get("synthetic") and not p.get("ignored"):
                s = clean_user(p.get("text", "")) if role == "user" else (p.get("text") or "").strip()
                if s:
                    items.append(("user" if role == "user" else "assistant", s, ts))
            elif kind == "tool":
                st = p.get("state") or {}
                items.append(("tool", f"{p.get('tool')}: {describe(p.get('tool'), st.get('input') or {})}", ts))
            elif kind == "compaction":
                items.append(("note", "contexto compactado", ts))
        err = m.get("error") or {}
        if err.get("name") == "MessageAbortedError":
            items.append(("note", "turno abortado (Aborted)", ts))
        elif err:
            items.append(("error", f"{err.get('name')}: {(err.get('data') or {}).get('message', '')}", ts))
        if role == "user":
            unfinished = True
        elif role == "assistant":
            # "tool-calls" sem mensagem seguinte = o laço do agente parou no meio.
            done = (m.get("time") or {}).get("completed") and m.get("finish") != "tool-calls"
            unfinished = not done and not err
    return items, None, unfinished


def clean_user(s):
    """Texto digitado pelo usuário, sem contexto injetado pelo harness."""
    s = re.sub(r"<system-reminder>.*?</system-reminder>", "", s or "", flags=re.S).strip()
    m = re.search(r"<command-name>(.*?)</command-name>", s)
    if m:
        args = re.search(r"<command-args>(.*?)</command-args>", s, re.S)
        return (m.group(1) + " " + (args.group(1) if args else "")).strip()
    if s.startswith("<") or s.startswith("# AGENTS.md instructions") or s.startswith("Caveat:"):
        return ""
    if s.startswith("[Request interrupted"):
        return "(interrompido pelo usuário)"
    return s


def parse_claude(recs):
    items, branch = [], None
    for r in recs:
        if r.get("isSidechain") or r.get("isMeta"):
            continue
        if r.get("gitBranch") not in (None, "", "HEAD"):
            branch = r["gitBranch"]
        m, ts = r.get("message") or {}, r.get("timestamp")
        c = m.get("content")
        if r.get("type") == "user":
            texts = [c] if isinstance(c, str) else [x.get("text", "") for x in c or [] if x.get("type") == "text"]
            for s in map(clean_user, texts):
                if s:
                    items.append(("user", s, ts))
        elif r.get("type") == "assistant" and isinstance(c, list):
            if r.get("isApiErrorMessage") or m.get("model") == "<synthetic>":
                items.append(("error", " ".join(x.get("text", "") for x in c), ts))
                continue
            for x in c:
                if x.get("type") == "text" and x.get("text", "").strip():
                    items.append(("assistant", x["text"], ts))
                elif x.get("type") == "tool_use":
                    items.append(("tool", f"{x.get('name')}: {describe(x.get('name'), x.get('input'))}", ts))
    kinds = [k for k, _, _ in items if k != "note"]
    unfinished = bool(kinds) and kinds[-1] in ("user", "tool")
    return items, branch, unfinished


def parse_codex(recs):
    items, open_turn = [], False
    for r in recs:
        p, ts = r.get("payload") or {}, r.get("timestamp")
        kind = p.get("type")
        if r.get("type") == "response_item":
            if kind == "message" and p.get("role") in ("user", "assistant"):
                for x in p.get("content") or []:
                    s = x.get("text") or ""
                    s = clean_user(s) if p["role"] == "user" else s.strip()
                    if s:
                        items.append((p["role"], s, ts))
            elif kind == "function_call":
                try:
                    args = json.loads(p.get("arguments") or "{}")
                except ValueError:
                    args = p.get("arguments")
                items.append(("tool", f"{p.get('name')}: {describe(p.get('name'), args)}", ts))
            elif kind == "custom_tool_call":
                items.append(("tool", f"{p.get('name')}: {describe(p.get('name'), {'command': p.get('input') or ''})}", ts))
            elif kind == "local_shell_call":
                items.append(("tool", "shell: " + describe("shell", p.get("action") or {}), ts))
        elif r.get("type") == "event_msg":
            if kind == "task_started":
                open_turn = True
            elif kind == "task_complete":
                open_turn = False
            elif kind == "turn_aborted":
                open_turn = False
                items.append(("note", f"turno abortado ({p.get('reason', '?')})", ts))
            elif kind in ("error", "stream_error"):
                items.append(("error", p.get("message") or kind, ts))
            elif kind == "context_compacted":
                items.append(("note", "contexto compactado", ts))
    return items, None, open_turn


def load_session(harness, path, mtime):
    if harness == "opencode":
        items, branch, unfinished = parse_opencode(opencode_records(path))
    else:
        items, branch, unfinished = (parse_codex if harness == "codex" else parse_claude)(tail_records(path))
    return {"h": harness, "path": path, "mtime": mtime,
            "items": items, "branch": branch, "unfinished": unfinished}


def recent_sessions(root, exclude=(), want=3):
    """Últimas conversas com conteúdo, da branch atual, exceto a sessão em andamento."""
    branch, out = current_branch(root), []
    for mtime, h, p in find_transcripts(root)[: want * 3]:
        if any(e and (norm(p) == norm(e) if os.sep in e or "/" in e else e in os.path.basename(p)) for e in exclude):
            continue
        s = load_session(h, p, mtime)
        if not any(k in ("user", "assistant") for k, _, _ in s["items"]):
            continue
        if s["branch"] and s["branch"] != branch:
            continue
        out.append(s)
        if len(out) == want:
            break
    return out


def hhmm(ts):
    try:
        if isinstance(ts, (int, float)):  # OpenCode: milissegundos
            return datetime.datetime.fromtimestamp(ts / 1000).strftime("%H:%M")
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except (ValueError, OSError, OverflowError):
        return "--:--"


LABEL = {"user": "você", "assistant": "agente", "tool": "ferramentas", "error": "⚠ erro", "note": "·"}
LIMIT = {"user": 700, "assistant": 900, "error": 300, "note": 120}


def fmt_row(row, long=False):
    kind, ts, text = row
    if kind == "tool":
        body = "; ".join(short(t, 100) for t in text[:4]) + (f" (+{len(text) - 4})" if len(text) > 4 else "")
    else:
        body = short(text, 1600 if long else LIMIT[kind])
    return f"- [{hhmm(ts)}] {LABEL[kind]}: {body}"


def bridge_lines(items, budget):
    """Fim da conversa em ordem cronológica, cabendo no orçamento; sempre inclui um pedido do usuário."""
    rows = []
    for kind, text, ts in items:
        if kind == "tool" and rows and rows[-1][0] == "tool":
            rows[-1][2].append(text)
        else:
            rows.append([kind, ts, [text] if kind == "tool" else text])
    last_asst = max((i for i, r in enumerate(rows) if r[0] == "assistant"), default=-1)
    out, used, first = [], 0, len(rows)
    for i in range(len(rows) - 1, -1, -1):
        line = fmt_row(rows[i], long=i == last_asst)
        if out and used + len(line) > budget:
            break
        out.append(line)
        used += len(line)
        first = i
    out.reverse()
    if first > 0:
        out.insert(0, "- …")
        users = [i for i, r in enumerate(rows[:first]) if r[0] == "user"]
        if users and not any(r[0] == "user" for r in rows[first:]):
            out.insert(0, fmt_row(rows[users[-1]]))
    return out


def last_user(s):
    return next((t for k, t, _ in reversed(s["items"]) if k == "user"), "")


def bridge_pack(root, exclude=(), budget=BRIDGE_CHARS):
    sessions = recent_sessions(root, exclude)
    if not sessions:
        return []
    s = sessions[0]
    errors = [t for k, t, _ in s["items"] if k == "error"]
    end = ""
    if s["items"] and s["items"][-1][0] == "error":
        end = f" — terminou com erro: {short(errors[-1], 120)}"
    elif s["unfinished"]:
        end = " — turno cortado sem conclusão (limite, crash ou Ctrl+C)"
    out = [f"\n### Conversa anterior — {s['h']}, {stamp(s['mtime'])}{end}",
           "Fim da conversa (saídas de ferramentas omitidas). É o que foi dito, não necessariamente feito: "
           "confira no repositório. Mais: `cmem conversa`."]
    out.extend(bridge_lines(s["items"], budget))
    if len(sessions) > 1:
        out.append("Antes dessa: " + "; ".join(
            f"{o['h']} {stamp(o['mtime'])[5:16]} «{short(last_user(o), 90)}»" for o in sessions[1:]))
    return out


# ---------------------------------------------------------------- retomada
def resume_pack(root, max_events=10, exclude=()):
    d = state_dir(root)
    recs = read_journal(d)
    after, last_ok = since_last_state(recs)
    branch = current_branch(root)
    head = git(root, "rev-parse", "--short", "HEAD") or "(sem commits)"
    out = [f"## Retomada — branch `{branch}` — HEAD {head}"]
    out.append("Ordem de confiança: estado real do repositório > diário mecânico > "
               "state.md e conversa anterior (vale a mais recente) > memórias antigas.")

    text, problems = validate(root)
    good = os.path.join(d, "state.good.md")
    if text is None and not os.path.exists(good):
        out.append(f"\n### Estado semântico\nNenhum state.md para esta branch. Antes da primeira etapa longa, "
                   f"crie {slash(os.path.join(d, 'state.md'))} a partir do modelo (`cmem modelo`).")
    else:
        if problems and os.path.exists(good):
            out.append(f"\n⚠ state.md inválido ({'; '.join(problems)}). Usando a última versão íntegra (state.good.md).")
            with open(good, encoding="utf-8") as f:
                text = f.read()
        elif problems:
            out.append(f"\n⚠ state.md inválido ({'; '.join(problems)}) e não há versão íntegra anterior.")
        out.append("\n### Estado semântico (escrito pelo agente)\n" + (text or "").strip())

    if last_ok:
        if last_ok.get("head") and last_ok["head"] != head:
            out.append(f"\n⚠ O estado foi escrito com HEAD {last_ok['head']}; agora é {head}. Houve commits depois: confira `git log {last_ok['head']}..HEAD`.")
        out.append(f"\nEstado gravado em {stamp(last_ok['ts'])} por {last_ok.get('h', '?')}.")

    pend = open_operations(recs)
    if pend:
        out.append("\n### ⚠ Operações iniciadas sem confirmação de término")
        out.append("Verifique o efeito de cada uma ANTES de repetir:")
        for r in pend[-5:]:
            out.append(f"- [{stamp(r['ts'])}] {r.get('h', '?')} {r.get('tool')}: {r.get('target')}")

    stops = [r for r in after if r.get("ev") in ("interrupted", "stop_failure", "session_end")]
    if stops:
        r = stops[-1]
        out.append(f"\nÚltima sessão terminou por: {r.get('ev')} {r.get('why', '')} ({r.get('h', '?')}, {stamp(r['ts'])}).")

    acts = [r for r in after if r.get("ev") in ("end", "plan")]
    if acts:
        total, side = progress_counts(after)
        out.append(f"\n### Atividade registrada DEPOIS do último estado ({total} ferramentas, {side} com efeito colateral)")
        out.append("O state.md pode estar desatualizado em relação a estes eventos:")
        groups = []  # agrupa repetições consecutivas
        for r in acts:
            key = (r.get("h"), r.get("tool"), r.get("target"), r.get("ok"))
            if groups and groups[-1][0] == key:
                groups[-1][2] += 1
            else:
                groups.append([key, r, 1])
        for key, r, n in groups[-max_events:]:
            mark = "✗" if r.get("ok") is False else "·"
            rep = f" (×{n})" if n > 1 else ""
            out.append(f"{mark} [{stamp(r['ts'])[11:16]}] {r.get('h', '?')} {r.get('tool')}: {r.get('target')}{rep}")

    try:
        out.extend(bridge_pack(root, exclude))
    except Exception as e:  # formato de transcrição mudou: o resto do pacote vale mesmo assim
        out.append(f"\n(ponte de conversas indisponível: {e})")

    status = git(root, "status", "--short").splitlines()
    if status:
        out.append("\n### git status (até 15 linhas)")
        out.extend(status[:15])
        if len(status) > 15:
            out.append(f"… +{len(status) - 15} arquivos")

    idx = os.path.join(root, ".ai", "decisions.md")
    if os.path.exists(idx):
        with open(idx, encoding="utf-8") as f:
            rows = [l.rstrip() for l in f if l.startswith("|")][:18]
        if rows:
            out.append("\n### Índice de decisões")
            out.extend(rows)

    out.append("\nAntes de alterar código, reconcilie nesta ordem: git status e HEAD > operações sem término "
               "(verifique o efeito antes de repetir) > atividade posterior ao estado > conversa anterior e state.md. "
               "Aponte divergências em até 5 linhas. Não releia o que já está acima.")
    return "\n".join(out)


# ---------------------------------------------------------------- hooks
def emit_json(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


def hook(harness):
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    root = repo_root(data.get("cwd"))
    if not root:
        return 0
    d = state_dir(root)
    ev = data.get("hook_event_name", "")
    tool = data.get("tool_name") or ""
    tinput = data.get("tool_input")
    tid = data.get("tool_use_id") or data.get("call_id")

    if ev == "SessionStart":
        append(d, {"ev": "session", "h": harness, "why": data.get("source", "")})
        # A conversa em andamento já está no contexto do modelo: a ponte mostra a anterior.
        print(resume_pack(root, exclude=(data.get("transcript_path"), data.get("session_id"))))
        return 0

    if ev == "PreToolUse":
        if SIDE_EFFECT.match(tool):
            append(d, {"ev": "start", "h": harness, "tool": tool, "id": tid, "target": describe(tool, tinput)})
        return 0

    if ev in ("PostToolUse", "PostToolUseFailure"):
        ok = ev == "PostToolUse"
        kind = "plan" if PLAN_TOOLS.search(tool) else "end"
        append(d, {"ev": kind, "h": harness, "tool": tool, "id": tid, "ok": ok, "target": describe(tool, tinput)})
        if touched_state(tool, tinput, root):
            problems = on_state_written(root, d, harness)
            if problems:
                sys.stderr.write("state.md inválido: " + "; ".join(problems) + ". Corrija só o necessário.\n")
                return 2  # Claude/Codex mostram o stderr ao modelo
            return 0
        if kind == "end" and EDIT_TOOLS.match(tool):
            checkpoint(root, "periódico")  # respeita CMEM_CKPT_MIN
        after, _ = since_last_state(read_journal(d))
        total, side = progress_counts(after)
        if side and side % NUDGE_EVENTS == 0:
            # Lembrete dentro do turno: entra no contexto junto do resultado da ferramenta,
            # sem gerar chamada extra ao modelo.
            emit_json({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                       "additionalContext": f"[cmem] {side} ações com efeito colateral desde o último state.md. "
                                            "Atualize as seções alteradas (Edit), sem reescrever o arquivo."}})
        return 0

    if ev in ("PreCompact", "PostCompact"):
        append(d, {"ev": "compact", "h": harness, "why": data.get("trigger", "")})
        checkpoint(root, "compactação", force=True)
        return 0

    if ev == "StopFailure":
        append(d, {"ev": "stop_failure", "h": harness, "why": data.get("error_type") or data.get("error", "")})
        checkpoint(root, f"falha: {data.get('error_type', '')}", force=True)
        return 0

    if ev in ("Interrupt", "SessionEnd"):
        # Codex limita esses hooks a 1–3 s: só o diário, sem checkpoint git.
        append(d, {"ev": "interrupted" if ev == "Interrupt" else "session_end", "h": harness,
                   "why": data.get("reason", "")})
        return 0

    if ev == "Idle":  # OpenCode session.idle: não pode bloquear
        checkpoint(root, "ocioso")
        return 0

    if ev == "Stop":
        checkpoint(root, "fim do turno")
        if data.get("stop_hook_active"):
            return 0
        recs = read_journal(d)
        after, last_ok = since_last_state(recs)
        total, side = progress_counts(after)
        if side or total >= STOP_MIN_TOOLS or (last_ok is None and total):
            emit_json({"decision": "block", "reason":
                       f"[cmem] {total} ferramentas ({side} com efeito colateral) desde o último state.md. "
                       "Atualize as seções alteradas de " + slash(os.path.join(d, "state.md")) +
                       " (Etapa atual, Operação em curso, Próximo passo, Descartado/Hipóteses se mudaram) e encerre."})
        return 0

    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "hook":
        return hook(argv[2] if len(argv) > 2 else "?")
    if cmd == "mcp":
        from .mcp import serve
        return serve()
    if cmd in ("install", "uninstall", "doctor"):
        from . import install
        return getattr(install, "run_" + cmd)(argv[2:])
    if cmd in ("--version", "versao"):
        from . import __version__
        print(__version__)
        return 0
    root = repo_root(os.getcwd())
    if not root:
        print("fora de um repositório git", file=sys.stderr)
        return 1
    if cmd == "resume":
        # Argumento opcional: id da sessão em andamento, para a ponte mostrar a anterior.
        print(resume_pack(root, exclude=tuple(argv[2:3])))
    elif cmd == "checkpoint":
        c = checkpoint(root, argv[2] if len(argv) > 2 else "manual", force=True)
        print(c or "falhou")
    elif cmd == "validate":
        _, problems = validate(root)
        print("ok" if not problems else "\n".join(problems))
        if not problems:
            on_state_written(root, state_dir(root), "manual")
        return 1 if problems else 0
    elif cmd == "path":
        print(slash(state_dir(root)))
    elif cmd == "modelo":
        print(TEMPLATE.replace("<branch>", current_branch(root)))
    elif cmd == "importar":
        args = [a for a in argv[2:] if a != "--forcar"]
        msg = import_state(root, args[0] if args else None, force="--forcar" in argv)
        print(msg)
        return 0 if msg.startswith("importado") else 1
    elif cmd == "conversa":
        lines = bridge_pack(root, budget=int(argv[2]) if len(argv) > 2 else 30000)
        print("\n".join(lines).strip() or "nenhuma conversa recente deste repositório")
    else:
        print(__doc__)
        return 1
    return 0


def entry():
    """Ponto de entrada dos comandos `cmem` e `coimbra-memory`."""
    # Harnesses trocam JSON e texto em UTF-8. No Windows, com saída redirecionada,
    # o Python usaria cp1252 e quebraria em acentos e no símbolo ⚠.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main(sys.argv))
    except Exception as e:  # um hook nunca pode derrubar a sessão
        sys.stderr.write(f"cmem: {e}\n")
        sys.exit(0)


if __name__ == "__main__":
    entry()
