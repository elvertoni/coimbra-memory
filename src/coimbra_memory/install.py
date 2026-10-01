"""Instalador: registra o servidor MCP no Claude Code, no Codex e no OpenCode e,
com --hooks, os hooks do Claude Code. Tudo em nível de usuário: nenhum projeto
é alterado.

Regras: backup antes de cada arquivo alterado; o resultado é validado antes de
gravar; o que não dá para editar com segurança vira instrução manual.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

from . import __version__, core

KEY = core.NAME
BEGIN = f"# >>> {KEY} (gerado por `cmem install`; remova com `cmem uninstall`) >>>"
END = f"# <<< {KEY} <<<"
HOOKS = [  # evento, matcher, timeout (s)
    ("SessionStart", "startup|resume|clear|compact", 20),
    ("PreToolUse", "Bash|Edit|Write|MultiEdit|NotebookEdit", 10),
    ("PostToolUse", None, 30),
    ("PostToolUseFailure", None, 10),
    ("PreCompact", None, 30),
    ("Stop", None, 30),
    ("StopFailure", None, 30),
    ("SessionEnd", None, 10),
]


# ---------------------------------------------------------------- caminhos
def home(*p):
    return os.path.join(os.path.expanduser("~"), *p)


def claude_json():
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(d, ".claude.json") if d else home(".claude.json")


def claude_settings():
    return os.path.join(core.claude_home(), "settings.json")


def codex_toml():
    return os.path.join(core.codex_home(), "config.toml")


def opencode_config():
    d = os.path.join(os.environ.get("XDG_CONFIG_HOME") or home(".config"), "opencode")
    for name in ("opencode.jsonc", "opencode.json"):
        if os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
    return os.path.join(d, "opencode.json")


def ephemeral(path):
    p = path.replace("\\", "/").lower()
    return "/archive-v" in p or "/uv/cache/" in p or "/.cache/uv/" in p


def launcher():
    """Executável `cmem` permanente que os harnesses vão chamar (barras normais: valem em todo shell)."""
    if os.environ.get("CMEM_LAUNCHER"):
        return os.environ["CMEM_LAUNCHER"]
    for c in (sys.argv[0], sys.argv[0] + ".exe", shutil.which("cmem")):
        if c and os.path.isfile(c) and os.path.basename(c).lower().startswith(("cmem", KEY)):
            if ephemeral(os.path.abspath(c)):
                break
            return core.slash(os.path.abspath(c))
    raise SystemExit("cmem não está instalado de forma permanente. Rode primeiro:\n"
                     "  uv tool install coimbra-memory\n  cmem install")


# ---------------------------------------------------------------- arquivos
def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def save(path, text, dry):
    """Grava com backup. Em --simular, só diz o que faria."""
    if dry:
        return "(simulação: nada gravado)"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bak = ""
    if os.path.exists(path):
        bak = f"{path}.cmem-backup-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(path, bak)
    core.atomic_write(path, text)
    return f"backup: {core.slash(bak)}" if bak else "arquivo criado"


def strip_jsonc(text):
    """Remove comentários // e /* */ fora de strings, e vírgulas finais."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_json(path, jsonc=False):
    """(dados, tem_comentários). Arquivo ausente = {}."""
    text = read_text(path)
    if text is None or not text.strip():
        return {}, False
    if not jsonc:
        return json.loads(text), False
    clean = strip_jsonc(text)
    return json.loads(clean), clean.strip() != re.sub(r",(\s*[}\]])", r"\1", text).strip()


def dump(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- Claude Code
def claude_cli():
    return None if os.environ.get("CMEM_CLAUDE_CLI") == "none" else shutil.which("claude")


def install_claude(exe, dry):
    path = claude_json()
    data, _ = load_json(path)
    if KEY in data.get("mcpServers", {}):
        return "MCP já configurado"
    cli = claude_cli()
    if cli and not dry:
        # A CLI do Claude é o caminho oficial e lida com sessões abertas regravando o arquivo.
        r = subprocess.run([cli, "mcp", "add", "--scope", "user", KEY, "--", exe, "mcp"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return "MCP adicionado via `claude mcp add --scope user`"
    data.setdefault("mcpServers", {})[KEY] = {"type": "stdio", "command": exe, "args": ["mcp"], "env": {}}
    return f"MCP adicionado em {core.slash(path)} — {save(path, dump(data), dry)}"


def is_ours(hook):
    cmd = hook.get("command", "")
    return " hook claude" in cmd and ("cmem" in cmd or KEY in cmd)


def install_claude_hooks(exe, dry):
    path = claude_settings()
    data, _ = load_json(path)
    hooks = data.setdefault("hooks", {})
    added = []
    for event, matcher, timeout in HOOKS:
        groups = hooks.setdefault(event, [])
        if any(is_ours(h) for g in groups for h in g.get("hooks", [])):
            continue
        group = {"hooks": [{"type": "command", "command": f'"{exe}" hook claude', "timeout": timeout}]}
        if matcher:
            group = {"matcher": matcher, **group}
        groups.append(group)
        added.append(event)
    if not added:
        return "hooks já configurados"
    return f"hooks {', '.join(added)} em {core.slash(path)} — {save(path, dump(data), dry)}"


def uninstall_claude(dry):
    msgs = []
    path = claude_json()
    data, _ = load_json(path)
    if KEY in data.get("mcpServers", {}):
        cli = claude_cli()
        r = subprocess.run([cli, "mcp", "remove", "--scope", "user", KEY], capture_output=True) if cli and not dry else None
        if r is None or r.returncode != 0:
            del data["mcpServers"][KEY]
            save(path, dump(data), dry)
        msgs.append("MCP removido")
    path = claude_settings()
    data, _ = load_json(path)
    hooks, removed = data.get("hooks", {}), 0
    for event in list(hooks):
        for g in hooks[event]:
            before = len(g.get("hooks", []))
            g["hooks"] = [h for h in g.get("hooks", []) if not is_ours(h)]
            removed += before - len(g["hooks"])
        hooks[event] = [g for g in hooks[event] if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]
    if removed:
        save(path, dump(data), dry)
        msgs.append(f"{removed} hooks removidos")
    return "; ".join(msgs) or "nada a remover"


# ---------------------------------------------------------------- Codex
def codex_block(exe):
    return (f"{BEGIN}\n[mcp_servers.{KEY}]\ncommand = {json.dumps(exe)}\nargs = [\"mcp\"]\n"
            "# Libera salvar_estado e checkpoint sem pedir aprovação: só gravam em .git/coimbra-memory\n"
            f"default_tools_approval_mode = \"approve\"\n{END}\n")


def toml_ok(text):
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return True
    try:
        tomllib.loads(text)
        return True
    except tomllib.TOMLDecodeError:
        return False


def install_codex(exe, dry):
    path = codex_toml()
    text = read_text(path) or ""
    if BEGIN in text or f"[mcp_servers.{KEY}]" in text:
        return "MCP já configurado"
    new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + codex_block(exe)
    if not toml_ok(new):
        return "⚠ o config.toml atual não é TOML válido; nada alterado. Bloco para colar:\n" + codex_block(exe)
    return f"MCP adicionado em {core.slash(path)} — {save(path, new, dry)}"


def uninstall_codex(dry):
    path = codex_toml()
    text = read_text(path) or ""
    if BEGIN not in text:
        return "nada a remover"
    new = re.sub(r"\n*" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "\n", text, flags=re.S)
    save(path, new.rstrip("\n") + "\n", dry)
    return "MCP removido"


# ---------------------------------------------------------------- OpenCode
def opencode_entry(exe):
    return {"type": "local", "command": [exe, "mcp"], "enabled": True}


def install_opencode(exe, dry):
    path = opencode_config()
    data, comments = load_json(path, jsonc=True)
    if KEY in data.get("mcp", {}):
        return "MCP já configurado"
    if comments:
        return ("⚠ o arquivo tem comentários, que seriam perdidos; nada alterado. Acrescente em \"mcp\":\n"
                + json.dumps({KEY: opencode_entry(exe)}, indent=2))
    data.setdefault("$schema", "https://opencode.ai/config.json")
    data.setdefault("mcp", {})[KEY] = opencode_entry(exe)
    return f"MCP adicionado em {core.slash(path)} — {save(path, dump(data), dry)}"


def uninstall_opencode(dry):
    path = opencode_config()
    data, comments = load_json(path, jsonc=True)
    if KEY not in data.get("mcp", {}):
        return "nada a remover"
    if comments:
        return f"⚠ remova a chave \"{KEY}\" de \"mcp\" em {core.slash(path)} manualmente (o arquivo tem comentários)"
    del data["mcp"][KEY]
    save(path, dump(data), dry)
    return "MCP removido"


# ---------------------------------------------------------------- comandos
def run_install(argv):
    dry, hooks = "--simular" in argv, "--hooks" in argv
    exe = launcher()
    print(f"coimbra-memory {__version__} — comando dos harnesses: {exe}" + (" [SIMULAÇÃO]" if dry else ""))
    steps = [("Claude Code", lambda: install_claude(exe, dry)),
             ("Codex", lambda: install_codex(exe, dry)),
             ("OpenCode", lambda: install_opencode(exe, dry))]
    if hooks:
        steps.insert(1, ("Claude Code (hooks)", lambda: install_claude_hooks(exe, dry)))
    failed = report(steps)
    print("\nReinicie os programas abertos para carregar o servidor. Diagnóstico: cmem doctor")
    if not hooks:
        print("Retomada automática no Claude Code (sem o agente precisar chamar a ferramenta): cmem install --hooks")
    return 1 if failed else 0


def run_uninstall(argv):
    dry = "--simular" in argv
    failed = report([("Claude Code", lambda: uninstall_claude(dry)),
                     ("Codex", lambda: uninstall_codex(dry)),
                     ("OpenCode", lambda: uninstall_opencode(dry))])
    print("\nO estado salvo nos projetos (.git/coimbra-memory e refs/coimbra-memory/*) foi mantido.")
    return 1 if failed else 0


def report(steps):
    failed = False
    for name, fn in steps:
        try:
            msg = fn()
        except Exception as e:  # um harness com problema não impede os outros
            msg, failed = f"⚠ erro: {e}", True
        print(f"- {name}: {msg}")
    return failed


def handshake(exe):
    """Inicia o servidor MCP como um harness faria e confere a resposta."""
    try:
        r = subprocess.run([exe, "mcp"], input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
        return json.loads(r.stdout.splitlines()[0])["result"]["serverInfo"]["name"] == KEY
    except Exception:
        return False


def run_doctor(argv):
    ok = lambda b: "✓" if b else "✗"
    print(f"coimbra-memory {__version__} · Python {sys.version.split()[0]} · {sys.platform}")
    print(f"{ok(shutil.which('git'))} git")
    try:
        exe = launcher()
        print(f"{ok(True)} cmem permanente: {exe}")
        print(f"{ok(handshake(exe))} servidor MCP responde")
    except SystemExit as e:
        print(f"{ok(False)} {e}")
    claude, _ = load_json(claude_json())
    print(f"{ok(KEY in claude.get('mcpServers', {}))} Claude Code: MCP ({core.slash(claude_json())})")
    settings, _ = load_json(claude_settings())
    n = sum(is_ours(h) for gs in settings.get("hooks", {}).values() for g in gs for h in g.get("hooks", []))
    print(f"{'✓' if n else '·'} Claude Code: {n} hooks (opcionais)")
    print(f"{ok(BEGIN in (read_text(codex_toml()) or ''))} Codex: MCP ({core.slash(codex_toml())})")
    oc, _ = load_json(opencode_config(), jsonc=True)
    print(f"{ok(KEY in oc.get('mcp', {}))} OpenCode: MCP ({core.slash(opencode_config())})")
    root = core.repo_root(os.getcwd())
    if root:
        d = core.state_dir(root, create=False)  # diagnóstico não cria nada
        print(f"\nRepositório atual: {core.slash(root)}\nEstado: {core.slash(d)}"
              + ("" if os.path.isdir(d) else " (ainda não existe)"))
        found = core.find_transcripts(root)
        print(f"Conversas recentes encontradas: {len(found)} "
              f"({', '.join(sorted({h for _, h, _ in found})) or 'nenhuma'})")
    return 0
