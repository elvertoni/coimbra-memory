"""Servidor MCP (stdio) do coimbra-memory, sem dependências.

Uma mensagem JSON-RPC 2.0 por linha em stdin/stdout. Stdout é exclusivo do
protocolo: qualquer diagnóstico vai para stderr.

O servidor grava o estado por conta própria, fora do sandbox do harness: o
agente não precisa de permissão de escrita em .git/.
"""
import json
import os
import sys

from . import __version__, core

INSTRUCTIONS = """coimbra-memory retoma a tarefa entre Claude Code, Codex e OpenCode quando a sessão cai (limite de uso, contexto esgotado, interrupção).
1. No início da sessão, se o contexto ainda não contém "## Retomada — branch", chame `retomar` com `diretorio` = raiz do projeto, e reconcilie antes de alterar código.
2. A sessão pode acabar sem aviso. ANTES de etapa longa ou operação com efeito colateral (migração, script de dados, deploy, refatoração em vários arquivos), chame `salvar_estado` com "Etapa atual", "Operação em curso" (ação; como verificar se terminou; se é seguro repetir) e "Próximo passo". DEPOIS de cada etapa relevante, envie só as seções que mudaram. Tentativa que falhou vai em "Descartado", na hora.
3. Nunca registre segredos, tokens ou dados pessoais no estado."""

DIR = {"type": "string", "description": "Raiz do projeto (repositório git). Padrão: diretório de trabalho do servidor."}

TOOLS = [
    {
        "name": "retomar",
        "annotations": {"title": "Retomar tarefa", "readOnlyHint": True, "openWorldHint": False},
        "description": "Pacote de retomada da branch atual: estado salvo, operações iniciadas sem término, "
                       "atividade posterior, fim da conversa anterior (Claude Code, Codex ou OpenCode) e git status. "
                       "Chame no início da sessão.",
        "inputSchema": {"type": "object", "properties": {
            "diretorio": DIR,
            "sessao_atual": {"type": "string", "description": "Id da sessão em andamento, para a ponte mostrar a anterior."},
        }},
    },
    {
        "name": "salvar_estado",
        # Só escreve em .git/coimbra-memory/: não altera arquivos do projeto.
        "annotations": {"title": "Salvar estado da tarefa", "readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
        "description": "Atualiza o estado da tarefa (state.md) substituindo só as seções informadas; seção com texto "
                       "vazio é removida. Seções obrigatórias: Objetivo, Etapa atual, Operação em curso, Próximo passo. "
                       "Outras usuais: Plano, Descartado, Hipóteses abertas. Grava de forma atômica e valida; "
                       "se inválido, nada é gravado e os problemas voltam na resposta.",
        "inputSchema": {"type": "object", "required": ["secoes"], "properties": {
            "diretorio": DIR,
            "secoes": {"type": "object", "additionalProperties": {"type": "string"},
                       "description": "Ex.: {\"Etapa atual\": \"...\", \"Próximo passo\": \"...\"}"},
            "autor": {"type": "string", "description": "Harness/modelo que está gravando, ex.: \"codex/gpt-5\"."},
        }},
    },
    {
        "name": "conversa",
        "annotations": {"title": "Conversa anterior", "readOnlyHint": True, "openWorldHint": False},
        "description": "Trecho maior do fim da última conversa deste repositório, de qualquer harness. "
                       "Use quando o pacote de retomada não bastar.",
        "inputSchema": {"type": "object", "properties": {
            "diretorio": DIR,
            "caracteres": {"type": "integer", "description": "Orçamento de texto. Padrão: 30000."},
        }},
    },
    {
        "name": "checkpoint",
        "annotations": {"title": "Checkpoint git", "readOnlyHint": False, "destructiveHint": False,
                        "openWorldHint": False},
        "description": "Grava agora o código (inclusive arquivos novos) e o estado em refs git fora das branches, "
                       "sem tocar no índice nem na branch. Use antes de operações arriscadas.",
        "inputSchema": {"type": "object", "properties": {
            "diretorio": DIR,
            "motivo": {"type": "string"},
        }},
    },
]


def project(args):
    d = args.get("diretorio") or os.getcwd()
    root = core.repo_root(d)
    if not root:
        raise ValueError(f"não é um repositório git: {d}. Informe `diretorio` com a raiz do projeto.")
    return root


def call(name, args):
    root = project(args)
    if name == "retomar":
        sid = args.get("sessao_atual")
        return core.resume_pack(root, exclude=(sid,) if sid else ())
    if name == "salvar_estado":
        secoes = args.get("secoes")
        if not isinstance(secoes, dict) or not secoes:
            raise ValueError("`secoes` deve ser um objeto {nome da seção: texto}")
        problems = core.update_state(root, secoes, args.get("autor"))
        if problems:
            raise ValueError("estado NÃO gravado: " + "; ".join(problems))
        return f"estado gravado em {core.slash(os.path.join(core.state_dir(root), 'state.md'))}"
    if name == "conversa":
        lines = core.bridge_pack(root, budget=int(args.get("caracteres") or 30000))
        return "\n".join(lines).strip() or "nenhuma conversa recente deste repositório"
    if name == "checkpoint":
        c = core.checkpoint(root, args.get("motivo") or "manual (mcp)", force=True)
        if not c:
            raise ValueError("checkpoint falhou")
        return f"checkpoint {c[:10]} em {core.WIP_REF}{core.current_branch(root)}"
    raise ValueError(f"ferramenta desconhecida: {name}")


def handle(msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": core.NAME, "version": __version__},
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            text, error = call(params.get("name"), params.get("arguments") or {}), False
        except Exception as e:  # erro da ferramenta volta ao modelo, não derruba o servidor
            text, error = str(e), True
        return {"content": [{"type": "text", "text": text}], "isError": error}
    raise LookupError(method)


def serve():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON inválido"}}
        else:
            if "id" not in msg:  # notificação (ex.: notifications/initialized): sem resposta
                continue
            try:
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
            except LookupError:
                reply = {"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32601, "message": f"método não suportado: {msg.get('method')}"}}
            except Exception as e:
                reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0
