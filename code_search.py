"""Semantic code search for Claude Code, built on LEANN.

One central index per repo under ~/.leann/repos/<slug>, keyed by the main checkout, so every
worktree of a repo shares it. Ruby is chunked by Prism (chunk_ruby.rb); other code is chunked
into line windows with a path header; Markdown uses LEANN's stock splitter. `serve` is a stdio
MCP server; the other commands are for building and measuring indexes.
"""

import argparse
import collections
import contextlib
import fcntl
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
INDEX_ROOT = Path(os.environ.get("CODE_SEARCH_HOME", HOME / ".leann" / "repos"))
USAGE_LOG = Path(os.environ.get("CODE_SEARCH_USAGE_LOG", HOME / ".leann" / "usage.jsonl"))
TOOL_DIR = Path(__file__).resolve().parent
PYTHON = TOOL_DIR / ".venv" / "bin" / "python"
CONFIG_FILE = TOOL_DIR / "config.json"
DEFAULTS = {
    "model": "Qwen/Qwen3-Embedding-0.6B",
    "mode": "sentence-transformers",
    "query_prompt": "Instruct: Given a question about a codebase, retrieve the code that answers it\nQuery: ",
    "ruby": "prism",
    "other": "lines",  # non-Ruby code: "text" (stock splitter) or "lines" (windows with a header)
    "vector_weight": 1.0,
    "ruby_max_chars": 1500,  # max chunk size for Prism and line-window chunks
    "max_seq_length": 512,  # tokens embedded per chunk; caps padding for long-context models
}

RUBY_EXTS = (".rb", ".rake")
CODE_EXTS = {
    ".rb", ".rake", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".py", ".go", ".rs", ".java", ".kt",
    ".swift", ".cs", ".php", ".ex", ".exs", ".erb", ".vue", ".svelte", ".sql", ".sh",
}
DOC_EXTS = {".md"}
SKIP_DIRS = {"spec", "test", "tests", "__tests__", "__mocks__", "vendor", "node_modules", "dist", "fixtures", "coverage"}
SKIP_PREFIXES = ("db/migrate/",)
SKIP_NAME = re.compile(r"\.(test|spec|stories)\.[jt]sx?$|\.min\.js$")
MAX_BYTES = 256_000
STALE_HOURS = 24  # a search on an older index starts a background rebuild


def load_config(overrides=None):
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        cfg.update({k: v for k, v in json.loads(CONFIG_FILE.read_text()).items() if not k.startswith("_")})  # _ keys are docs
    cfg.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return cfg


# --- repos ---------------------------------------------------------------------------------


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout


def main_repo(path):
    """Main checkout of the repo containing path; worktrees resolve to it."""
    common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    return common.parent if common.name == ".git" else common


def slug(root):
    try:
        rel = root.relative_to(HOME / "Code" / "github.com")
    except ValueError:
        rel = Path(*root.parts[1:])
    return "__".join(rel.parts)


def index_dir(root):
    return INDEX_ROOT / slug(root)


def tracked_files(root):
    out = []
    for f in git(root, "ls-files", "-z").split("\0"):
        p = Path(f)
        if not f or p.suffix.lower() not in CODE_EXTS | DOC_EXTS:
            continue
        if SKIP_DIRS & set(p.parts[:-1]) or f.startswith(SKIP_PREFIXES) or SKIP_NAME.search(p.name):
            continue
        full = root / f
        try:
            if full.is_symlink() or full.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        out.append(f)
    return out


# --- chunking ------------------------------------------------------------------------------


def text_chunks(root, files):
    """Same splitter settings as `leann build` 0.3.8 without AST chunking (the stock MCP path)."""
    from llama_index.core.node_parser import SentenceSplitter

    code_p = SentenceSplitter(chunk_size=512, chunk_overlap=50, separator="\n", paragraph_separator="\n\n")
    doc_p = SentenceSplitter(chunk_size=256, chunk_overlap=128, separator=" ", paragraph_separator="\n\n")
    for f in files:
        text = (root / f).read_text(errors="replace")
        if not text.strip():
            continue
        is_code = Path(f).suffix.lower() in CODE_EXTS
        if is_code:
            lines = text.split("\n")
            width = len(str(len(lines)))
            text = "\n".join(f"{i + 1:>{width}}|{line}" for i, line in enumerate(lines))
        for chunk in (code_p if is_code else doc_p).split_text(text):
            first = chunk.split("\n", 1)[0]
            if is_code and "|" in first and not re.match(r"^\s*\d+\|", first) and "\n" in chunk:
                chunk = chunk.split("\n", 1)[1]
            yield chunk, {"file_path": f}


def line_chunks(root, files, max_chars):
    """Line windows with a path:range header, for code without a structural chunker."""
    for f in files:
        lines = (root / f).read_text(errors="replace").splitlines(keepends=True)
        i = 0
        while i < len(lines):
            j, size = i, 0
            while j < len(lines) and (j == i or size + len(lines[j]) <= max_chars):
                size += len(lines[j])
                j += 1
            body = "".join(lines[i:j])
            if body.strip():
                yield f"# {f}:{i + 1}-{j}\n{body}", {"file_path": f, "start_line": i + 1, "end_line": j}
            if j >= len(lines):
                break
            i = max(j - 3, i + 1)


def ruby_candidates():
    """Rubies to try, in order. Repos often pin a Ruby older than 3.3, which lacks Prism, so after the
    PATH one come the version managers' installs, newest first."""
    if os.environ.get("CODE_SEARCH_RUBY"):
        yield os.environ["CODE_SEARCH_RUBY"]
    if shutil.which("ruby"):
        yield shutil.which("ruby")
    for base in (
        Path(os.environ.get("ASDF_DATA_DIR", HOME / ".asdf")) / "installs" / "ruby",
        Path(os.environ.get("RBENV_ROOT", HOME / ".rbenv")) / "versions",
        Path(os.environ.get("MISE_DATA_DIR", HOME / ".local" / "share" / "mise")) / "installs" / "ruby",
    ):
        found = sorted(base.glob("*/bin/ruby"), key=lambda p: [int(n) for n in re.findall(r"\d+", p.parent.parent.name)])
        yield from (str(p) for p in reversed(found))
    yield "/opt/homebrew/opt/ruby/bin/ruby"
    yield "/usr/local/opt/ruby/bin/ruby"


def prism_ruby(ruby):
    """The real binary behind ruby if it can load Prism, else None. Resolving it means a version
    manager shim cannot switch to a repo's pinned older Ruby when the chunker runs there."""
    try:
        proc = subprocess.run([ruby, "-e", 'require "prism"; print RbConfig.ruby'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


@functools.cache
def ruby_bin():
    """First Ruby that can load Prism, or None."""
    return next(filter(None, map(prism_ruby, dict.fromkeys(ruby_candidates()))), None)


def prism_chunks(root, files, max_chars):
    proc = subprocess.run(
        [ruby_bin(), str(TOOL_DIR / "chunk_ruby.rb")],
        input="\0".join(files), cwd=root, capture_output=True, text=True,
        env={**os.environ, "CHUNK_MAX_CHARS": str(max_chars)},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"chunk_ruby.rb failed: {proc.stderr[-2000:]}")
    for line in proc.stdout.splitlines():
        row = json.loads(line)
        text = row.pop("text")
        row["file_path"] = row.pop("path")
        yield text, row


def treesitter_chunks(root, files):
    """LEANN's own AST path; needs the patched astchunk with Ruby support."""
    from leann.chunking_utils import create_ast_chunks
    from llama_index.core import Document

    docs = [Document(text=(root / f).read_text(errors="replace"), metadata={"file_path": f, "language": "ruby"}) for f in files]
    for c in create_ast_chunks([d for d in docs if d.text.strip()], max_chunk_size=300, chunk_overlap=64):
        yield c["text"], {"file_path": c["metadata"].get("file_path", "")}


def all_chunks(root, cfg):
    files = tracked_files(root)
    ruby = [f for f in files if f.endswith(RUBY_EXTS)] if cfg["ruby"] != "text" else []
    ruby_set = set(ruby)
    other = [f for f in files if f not in ruby_set]
    if cfg["other"] == "lines":
        code = [f for f in other if Path(f).suffix.lower() in CODE_EXTS]
        yield from line_chunks(root, code, cfg["ruby_max_chars"])
        other = [f for f in other if Path(f).suffix.lower() not in CODE_EXTS]
    yield from text_chunks(root, other)
    if cfg["ruby"] == "prism" and ruby and ruby_bin():
        yield from prism_chunks(root, ruby, cfg["ruby_max_chars"])
    elif cfg["ruby"] == "prism" and ruby:
        print("code_search: no Ruby with Prism (3.3+) found, so Ruby files get line-window chunks. "
              "Set CODE_SEARCH_RUBY to a Ruby 3.3+ binary to use Prism.", file=sys.stderr)
        yield from line_chunks(root, ruby, cfg["ruby_max_chars"])
    elif cfg["ruby"] == "treesitter":
        yield from treesitter_chunks(root, ruby)


# --- build ---------------------------------------------------------------------------------


def cap_seq_length(cfg):
    """LEANN never sets a token limit, so long-context models pad the first (longest-sorted) batch
    of a build to the longest chunk. Load the model into LEANN's cache and cap it there."""
    from leann import embedding_compute

    embedding_compute.compute_embeddings(["warm up"], cfg["model"], cfg["mode"])
    for key, model in embedding_compute._model_cache.items():
        if key.startswith(f"sentence_transformers_{cfg['model']}_"):
            model.max_seq_length = cfg["max_seq_length"]


def build(root, cfg, dest):
    from leann import LeannBuilder

    started = time.time()
    chunks = list(all_chunks(root, cfg))
    options = {"query_prompt_template": cfg["query_prompt"]} if cfg.get("query_prompt") else None
    if cfg["mode"] == "sentence-transformers":
        cap_seq_length(cfg)
    builder = LeannBuilder("hnsw", embedding_model=cfg["model"], embedding_mode=cfg["mode"], embedding_options=options, is_recompute=False)
    for text, meta in chunks:
        builder.add_text(text, meta)
    dest.mkdir(parents=True, exist_ok=True)
    builder.build_index(str(dest / "documents.leann"))
    info = {
        "root": str(root), "head": git(root, "rev-parse", "HEAD").strip(), "branch": git(root, "branch", "--show-current").strip(),
        "built_at": int(time.time()), "seconds": round(time.time() - started), "chunks": len(chunks),
        "files": len({m["file_path"] for _, m in chunks}), "cfg": cfg,
    }
    (dest / "build.json").write_text(json.dumps(info, indent=2))
    return info


def try_lock(lock):
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def index_repo(root, overrides=None, lock_fd=None):
    """Build into a fresh dir and swap the `current` symlink, so searches never see a half-built index.
    lock_fd is a build.lock handle the parent already holds (see spawn_index)."""
    idx = index_dir(root)
    idx.mkdir(parents=True, exist_ok=True)
    with os.fdopen(lock_fd, "w") if lock_fd is not None else open(idx / "build.lock", "w") as lock:
        if lock_fd is None and not try_lock(lock):
            print("build already running")
            return None
        dest = idx / "builds" / str(int(time.time()))
        info = build(root, load_config(overrides), dest)
        tmp = idx / "current.tmp"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(dest)
        os.replace(tmp, idx / "current")
        for old in sorted((idx / "builds").iterdir())[:-2]:
            shutil.rmtree(old, ignore_errors=True)
        return info


# --- search --------------------------------------------------------------------------------

MAX_SEARCHERS = 4  # loaded indexes per process; the daemon serves several repos
_searchers = collections.OrderedDict()  # index path as passed -> (resolved build dir, LeannSearcher)


def searcher(path):
    """LeannSearcher for an index path such as <repo>/current. A path whose target changed (a swapped
    build) loads again, and past MAX_SEARCHERS the least recently used index is dropped."""
    from leann import LeannSearcher

    target = path.resolve()
    if path in _searchers and _searchers[path][0] != target:
        del _searchers[path]  # drop the swapped-out build before loading the new one
    if path not in _searchers:
        with contextlib.redirect_stdout(sys.stderr):
            _searchers[path] = (target, LeannSearcher(str(target / "documents.leann"), recompute_embeddings=False, use_daemon=False))
    _searchers.move_to_end(path)
    while len(_searchers) > MAX_SEARCHERS:
        _searchers.popitem(last=False)
    return _searchers[path][1]


def ranked_files(build_dir, query, k, weight):
    with contextlib.redirect_stdout(sys.stderr):
        results = searcher(build_dir).search(query, top_k=k, vector_weight=weight)
    return results


_cold = True  # no search has run in this process yet


def log_usage(root, out, started, top_k):
    """One line per MCP search, for tracking use over time. Never the query text, and never fatal."""
    global _cold
    try:
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "repo": slug(root),
            "latency_ms": round((time.time() - started) * 1000), "cold": _cold, "top_k": top_k, "n_results": out["n_results"],
            "index_age_h": round((time.time() - out["built_at"]) / 3600, 2), "model": out["model"],
        }
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"code_search usage log failed: {e}", file=sys.stderr)
    _cold = False


def render(path, query, top_k=8, path_prefix=None):
    """Search the repo's current index and format the results: the part of a search that needs the
    model, so a shared server can run it. The caller checks that the index exists."""
    root = main_repo(path)
    current = index_dir(root) / "current"
    info = json.loads((current / "build.json").read_text())
    results = ranked_files(current, query, top_k * 4 if path_prefix else top_k, info["cfg"]["vector_weight"])
    if path_prefix:
        results = [r for r in results if r.metadata.get("file_path", "").startswith(path_prefix)][:top_k]
    try:
        behind = git(path, "rev-list", "--count", f"{info['head']}..HEAD").strip()
    except subprocess.CalledProcessError:
        behind = "?"
    age_h = (time.time() - info["built_at"]) / 3600
    head = f"Index of {root.name} built {age_h:.1f} h ago at {info['head'][:9]} (this checkout has {behind} commits the index lacks). Paths are repo-relative."
    blocks = []
    for i, r in enumerate(results, 1):
        m = r.metadata
        where = m.get("file_path", "?") + (f":{m['start_line']}-{m['end_line']}" if "start_line" in m else "")
        text = r.text.strip()
        if text.startswith(f"# {m.get('file_path')}:"):  # chunk header repeats the line above
            text = text.split("\n", 1)[-1]
        blocks.append(f"### {i}. {where} {m.get('name', '')} (score {r.score:.3f})\n```\n{text}\n```")
    body = "\n\n".join(blocks) if blocks else "No results."
    return {"head": head, "body": body, "n_results": len(results), "built_at": info["built_at"], "model": info["cfg"]["model"]}


def search(path, query, top_k=8, path_prefix=None, log=False):
    started = time.time()
    root = main_repo(path)
    if not (index_dir(root) / "current").exists():
        return f"No code-search index for {root}. Call code_index to build one."
    out = render(path, query, top_k, path_prefix)
    head = out["head"]
    age_h = (time.time() - out["built_at"]) / 3600
    if age_h >= STALE_HOURS:
        head += "\n" + refresh_stale(root, age_h)
    if log:
        log_usage(root, out, started, top_k)
    return head + "\n\n" + out["body"]


# --- eval ----------------------------------------------------------------------------------


def evaluate(build_dir, questions_file, weight, k=10):
    qs = json.loads(Path(questions_file).read_text())["questions"]
    rows = []
    for q in qs:
        files = []
        for r in ranked_files(build_dir, q["q"], k * 4, weight):
            f = r.metadata.get("file_path", "")
            if f not in files:
                files.append(f)
        rank = next((i + 1 for i, f in enumerate(files[:k]) if f in q["expect"]), None)
        rows.append({"lang": q.get("lang", "-"), "q": q["q"], "rank": rank, "top": files[:3]})

    def summary(sub):
        n = len(sub)
        if not n:
            return {"n": 0}
        hit = lambda c: sum(1 for r in sub if r["rank"] and r["rank"] <= c)
        mrr = sum(1 / r["rank"] for r in sub if r["rank"]) / n
        return {"n": n, "hit@1": hit(1), "hit@5": hit(5), f"hit@{k}": hit(k), "mrr": round(mrr, 3)}

    by_lang = {lang: summary([r for r in rows if r["lang"] == lang]) for lang in sorted({r["lang"] for r in rows})}
    return {"all": summary(rows), **by_lang, "rows": rows}


# --- MCP server ----------------------------------------------------------------------------

TOOLS = [
    {
        "name": "code_search",
        "description": (
            "Semantic search over the current repo's code (all worktrees share one index of the main checkout). "
            "Use it to find code by meaning when you do not know the symbol or file name. Use LSP or rg for exact symbols. "
            "Results are repo-relative paths with line ranges; open them in your own checkout."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What the code does, in plain words."},
                "top_k": {"type": "integer", "default": 8},
                "path_prefix": {"type": "string", "description": "Only return files under this repo-relative path, e.g. app/models/"},
                "repo_path": {"type": "string", "description": "Any path inside the repo. Defaults to the server's working directory."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "code_index",
        "description": "Build or refresh the code_search index for the current repo in the background. Search keeps using the old index until the new one is ready.",
        "inputSchema": {"type": "object", "properties": {"repo_path": {"type": "string"}}},
    },
]


def spawn_index(root):
    """Start a detached `index` build. It inherits the build.lock this takes, so no search can start
    a second build before the child is up. False when a build already holds the lock."""
    idx = index_dir(root)
    idx.mkdir(parents=True, exist_ok=True)
    with open(idx / "build.lock", "w") as lock:
        if not try_lock(lock):
            return False
        with open(idx / "build.log", "w") as log:
            subprocess.Popen(
                [str(PYTHON), str(Path(__file__).resolve()), "index", "--repo", str(root), "--lock-fd", str(lock.fileno())],
                pass_fds=(lock.fileno(),), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
    return True


def start_index(path):
    root = main_repo(path)
    log = index_dir(root) / "build.log"
    if not spawn_index(root):
        return f"A build for {root} is already running. Log: {log}"
    return f"Started an index build for {root} from its current checkout. Log: {log}"


def refresh_stale(root, age_h):
    """Rebuild a stale index in the background, at most once per STALE_HOURS so a failing build does not
    restart on every search. code_index still retries on demand."""
    idx = index_dir(root)
    log = idx / "build.log"
    with open(idx / "build.lock", "w") as lock:
        running = not try_lock(lock)  # closing the file drops a lock taken here
    if not running and log.exists() and time.time() - log.stat().st_mtime < STALE_HOURS * 3600:
        return f"Index is {age_h:.0f} h old; the last refresh did not finish (log: {log}). Call code_index to retry."
    if running or not spawn_index(root):
        return f"Index is {age_h:.0f} h old; a background refresh is already running."
    return f"Index is {age_h:.0f} h old; a background refresh started."


def serve():
    import logging

    logging.disable(logging.INFO)  # LEANN logs whole passages at INFO
    out = sys.stdout
    sys.stdout = sys.stderr  # library prints must never reach the JSON-RPC stream

    def reply(msg_id, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": msg_id, **({"error": error} if error else {"result": result})}
        out.write(json.dumps(msg) + "\n")
        out.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        msg_id, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "code-search", "version": "0.1.0"},
            })
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            args = params.get("arguments") or {}
            path = args.get("repo_path") or os.getcwd()
            try:
                if params.get("name") == "code_search":
                    text = search(path, args["query"], int(args.get("top_k", 8)), args.get("path_prefix"), log=True)
                elif params.get("name") == "code_index":
                    text = start_index(path)
                else:
                    raise ValueError(f"unknown tool {params.get('name')}")
                reply(msg_id, {"content": [{"type": "text", "text": text}]})
            except Exception as e:  # report to the agent instead of killing the server
                reply(msg_id, {"content": [{"type": "text", "text": f"code_search error: {e}"}], "isError": True})
        elif method == "ping":
            reply(msg_id, {})
        elif msg_id is not None:
            reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


# --- cli -----------------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index", help="build the production index for a repo and swap it in")
    p.add_argument("--repo", default=".")
    p.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)  # build.lock held by spawn_index
    p = sub.add_parser("variant", help="build a named comparison index (not used by search)")
    p.add_argument("name")
    p.add_argument("--repo", default=".")
    p.add_argument("--ruby", choices=["text", "prism", "treesitter"])
    p.add_argument("--model")
    p.add_argument("--mode")
    p.add_argument("--ruby-max-chars", type=int)
    p.add_argument("--other", choices=["text", "lines"])
    p.add_argument("--query-prompt", help="prefix applied to queries only (instruction-tuned embedders)")
    p = sub.add_parser("eval", help="score a comparison index against a frozen question file")
    p.add_argument("name")
    p.add_argument("questions")
    p.add_argument("--repo", default=".")
    p.add_argument("--weight", type=float, default=1.0)
    p = sub.add_parser("search", help="search a repo's index from the shell")
    q = p.add_mutually_exclusive_group(required=True)
    q.add_argument("query", nargs="?", help="what the code does, in plain words")
    q.add_argument("--query-file", help="read the query from a file instead")
    p.add_argument("--repo", default=".")
    p.add_argument("-k", type=int, default=8)
    sub.add_parser("serve")
    a = ap.parse_args()

    if a.cmd == "serve":
        return serve()
    root = main_repo(Path(a.repo).resolve())
    if a.cmd == "index":
        print(json.dumps(index_repo(root, lock_fd=a.lock_fd), indent=2))
    elif a.cmd == "variant":
        cfg = load_config({"ruby": a.ruby, "model": a.model, "mode": a.mode, "ruby_max_chars": a.ruby_max_chars, "other": a.other, "query_prompt": a.query_prompt})
        print(json.dumps(build(root, cfg, index_dir(root) / "variants" / a.name), indent=2))
    elif a.cmd == "eval":
        res = evaluate(index_dir(root) / "variants" / a.name, a.questions, a.weight)
        for r in res.pop("rows"):
            print(f"{str(r['rank']):>4}  {r['lang']}  {r['q'][:70]:<70}  {r['top'][0] if r['top'] else ''}")
        print(json.dumps(res))
    elif a.cmd == "search":
        query = Path(a.query_file).read_text().strip() if a.query_file else a.query
        print(search(Path(a.repo).resolve(), query, a.k))


if __name__ == "__main__":
    main()
