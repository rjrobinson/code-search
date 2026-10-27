"""End-to-end benchmark: does code_search make headless Claude Code faster or cheaper at finding code?

Each run is one `claude -p` session that answers one frozen question with up to 3 file paths. All conditions
share the prompt, model and built-in tools:

  baseline        Read, Grep, Glob, LSP, Bash(rg/git ls-files/find/ls); hooks off
  code_search     the same plus mcp__code-search__code_search; hooks off
  baseline_hooks  baseline with your user hooks on, to price them

`--allowedTools` only pre-approves tools, and your user settings may already allow code_search and load other MCP
servers, so availability is pinned with --tools (built-ins) and --strict-mcp-config (MCP). If your hooks keep
state per directory, run baseline_hooks at --concurrency 1 with no other session in the repo.

Every run is a real Claude Code session billed to your account. --budget caps the total spend of a run dir.

  run_bench.py run --repo ~/src/app --questions eval/app.json [--limit N] [--concurrency 3] [--budget 40]
  run_bench.py run --run-dir bench/runs/<ts> --conditions baseline_hooks --concurrency 1 --budget 60
  run_bench.py summarize bench/runs/<ts>
"""

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
BENCH_DIR = TOOL_DIR / "bench"
sys.path.insert(0, str(TOOL_DIR))
import code_search  # noqa: E402

PROMPT = (
    "Find where this is implemented in this repository: {q}\n"
    "Reply with up to 3 repo-relative file paths, best first, one per line, and nothing else. Do not edit any files."
)
BASE_ALLOWED = ["Read", "Grep", "Glob", "LSP", "Bash(rg:*)", "Bash(git ls-files:*)", "Bash(find:*)", "Bash(ls:*)"]
BASE_DISALLOWED = ["Edit", "Write", "NotebookEdit", "Agent", "mcp__code-search__code_index"]
BUILTIN_TOOLS = "Read,Grep,Glob,LSP,Bash"
CONDITION_SETTINGS = {
    "baseline": {"autoMemoryEnabled": False, "disableAllHooks": True},
    "code_search": {"autoMemoryEnabled": False, "disableAllHooks": True},
    "baseline_hooks": {"autoMemoryEnabled": False},
}
CONDITIONS = tuple(CONDITION_SETTINGS)
DEFAULT_CONDITIONS = "baseline,code_search"
# (control, treatment): deltas are control minus treatment, so positive means the treatment saved it.
COMPARISONS = (("baseline", "code_search"), ("baseline", "baseline_hooks"))
PER_RUN_BUDGET = 1
HOOK_BLOCK = re.compile(r"PreToolUse|hook error|blocked by", re.I)  # add your own hooks' block messages here


# --- setup ---------------------------------------------------------------------------------


def load_questions(paths):
    """Each question file is one set, named after the file. See eval/example.json for the schema."""
    out = []
    for path in map(Path, paths):
        for i, q in enumerate(json.loads(path.read_text())["questions"], 1):
            out.append({"set": path.stem, "id": f"{path.stem}-{i:02d}", "lang": q.get("lang", "-"), "kind": q.get("kind"),
                        "q": q["q"], "expect": q["expect"]})
    return out


def index_current(repo):
    return code_search.index_dir(code_search.main_repo(repo)) / "current"


def mcp_config(usage_log):
    server = {
        "type": "stdio",
        "command": str(TOOL_DIR / ".venv" / "bin" / "python"),
        "args": [str(TOOL_DIR / "code_search.py"), "serve"],
        "env": {"CODE_SEARCH_USAGE_LOG": str(usage_log)},
    }
    return json.dumps({"mcpServers": {"code-search": server}})


def command(question, condition, usage_log):
    cmd = [
        "claude", "-p", PROMPT.format(q=question["q"]),
        "--output-format", "stream-json", "--verbose", "--no-session-persistence",
        "--max-budget-usd", str(PER_RUN_BUDGET), "--settings", json.dumps(CONDITION_SETTINGS[condition]),
        "--strict-mcp-config", "--tools", BUILTIN_TOOLS,
    ]
    allowed, disallowed = list(BASE_ALLOWED), list(BASE_DISALLOWED)
    if condition == "code_search":
        cmd += ["--mcp-config", mcp_config(usage_log)]
        allowed.append("mcp__code-search__code_search")
    else:
        disallowed.append("mcp__code-search__code_search")
    return cmd + ["--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(disallowed)]


def child_env():
    # Drop the parent session's identity (messaging socket, effort, session id) so each run is a plain new session.
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


def environment_snapshot(repo, question_files):
    return {
        "git_head": subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
        "git_status": subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout,
        "index_current": os.path.realpath(index_current(repo)),
        "code_search_sha256": hashlib.sha256((TOOL_DIR / "code_search.py").read_bytes()).hexdigest(),
        "questions_sha256": {Path(p).name: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in question_files},
        "claude_version": subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip(),
    }


# --- one run -------------------------------------------------------------------------------

PATH_LINE = re.compile(r"[\w@.\-]+(?:/[\w@.\-\[\]]+)+\.\w+")


def answer_paths(text, repo):
    """Repo-relative paths from the reply, tolerating bullets, backticks, ./, absolute paths and :line suffixes."""
    paths = []
    for line in (text or "").splitlines():
        line = line.strip().strip("`*\"' ")
        line = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line).strip("`*\"' ")
        line = line.replace(str(repo) + "/", "")
        line = re.sub(r"^\./", "", line)
        m = PATH_LINE.search(line)
        if not m:
            continue
        p = re.sub(r"(?::\d+(?:-\d+)?|#L\d+.*)$", "", m.group(0))
        if p not in paths:
            paths.append(p)
    return paths[:3]


def result_text(block):
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "")


def parse_stream(stream_lines):
    """Init, result, tool calls by name, tool time by name and hook blocks by tool, from stream-json lines with arrival times."""
    init, result = {}, {}
    tool_uses = {}  # id -> (name, arrived_at)
    tool_ms, hook_blocks = {}, {}
    for arrived, raw in stream_lines:
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            init = ev
        elif kind == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use" and block.get("id") not in tool_uses:
                    tool_uses[block["id"]] = (block.get("name"), arrived)
        elif kind == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in tool_uses:
                    name, started = tool_uses[block["tool_use_id"]]
                    tool_ms[name] = tool_ms.get(name, 0) + round((arrived - started) * 1000)
                    if block.get("is_error") and HOOK_BLOCK.search(result_text(block)):
                        hook_blocks[name] = hook_blocks.get(name, 0) + 1
        elif kind == "result":
            result = ev
    calls = {}
    for name, _ in tool_uses.values():
        calls[name] = calls.get(name, 0) + 1
    return init, result, calls, tool_ms, hook_blocks


def run_one(job, run_dir, repo):
    q, condition, rep = job["question"], job["condition"], job["rep"]
    run_id = f"{q['id']}.{condition}.r{rep}"
    usage_log = run_dir / "usage" / f"{run_id}.jsonl"
    stream_lines = []
    started = time.time()
    proc = subprocess.Popen(
        command(q, condition, usage_log), cwd=repo, env=child_env(),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stderr_chunks = []
    reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    reader.start()
    for line in proc.stdout:
        stream_lines.append((time.time(), line))
    proc.wait()
    reader.join(timeout=5)
    wall = time.time() - started

    (run_dir / "streams" / f"{run_id}.jsonl").write_text("".join(l for _, l in stream_lines))
    if stderr_chunks and stderr_chunks[0]:
        (run_dir / "logs" / f"{run_id}.stderr").write_text(stderr_chunks[0])

    init, result, calls, tool_ms, hook_blocks = parse_stream(stream_lines)
    usage = result.get("usage") or {}
    answers = answer_paths(result.get("result"), repo)
    expect = set(q["expect"])
    cs_log = [json.loads(l) for l in usage_log.read_text().splitlines()] if usage_log.exists() else []
    tokens = {k: usage.get(k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}
    return {
        "run_id": run_id, "set": q["set"], "question_id": q["id"], "lang": q["lang"], "kind": q["kind"], "condition": condition, "rep": rep,
        "answer_paths": answers, "correct_at_1": bool(answers) and answers[0] in expect,
        "correct_at_3": any(a in expect for a in answers),
        "wall_s": round(wall, 2), "duration_ms": result.get("duration_ms"), "duration_api_ms": result.get("duration_api_ms"),
        "num_turns": result.get("num_turns"), "tool_calls": calls, "tool_calls_total": sum(calls.values()), "tool_ms": tool_ms,
        "hook_blocks": sum(hook_blocks.values()), "hook_blocks_by_tool": hook_blocks,
        "permission_denials": len(result.get("permission_denials") or []),
        **tokens, "total_tokens": sum(tokens.values()), "total_cost_usd": result.get("total_cost_usd") or 0.0,
        "model": init.get("model"), "model_usage": result.get("modelUsage"),
        "is_error": bool(result.get("is_error")) or not result or proc.returncode != 0,
        "result_subtype": result.get("subtype"), "exit_code": proc.returncode,
        "init_tools": init.get("tools"), "init_mcp_servers": init.get("mcp_servers"),
        "cs_server_calls": [{k: r[k] for k in ("latency_ms", "cold", "top_k", "n_results")} for r in cs_log],
        "reply": (result.get("result") or "")[:500],
    }


# --- run plan ------------------------------------------------------------------------------


def phases(meta):
    """Phases of a run dir. The first harness version wrote one phase's fields at the top level."""
    if "phases" in meta:
        return meta["phases"]
    return [{
        "conditions": ["baseline", "code_search"], "concurrency": meta["args"]["concurrency"], "args": meta["args"],
        "planned_runs": meta["planned_runs"], "env_before": meta["env_before"], "env_after": meta.get("env_after"),
        "budget_stop": meta.get("budget_stop"), "spent_usd": meta.get("spent_usd"),
    }]


def run(args):
    conditions = args.conditions.split(",")
    if unknown := set(conditions) - set(CONDITIONS):
        sys.exit(f"unknown conditions: {sorted(unknown)}")
    repo = Path(args.repo).resolve() if args.repo else None
    question_files = [str(Path(p).resolve()) for p in args.questions or []]

    if args.run_dir:
        run_dir = Path(args.run_dir)
        meta = json.loads((run_dir / "meta.json").read_text())
        meta["phases"] = phases(meta)
        # A run dir keeps the repo and questions it started with, so its phases stay comparable.
        if (repo and str(repo) != meta.get("repo")) or (question_files and question_files != meta.get("questions")):
            sys.exit("--repo and --questions must match the run dir's meta.json; leave them out to reuse its values")
        repo, question_files = Path(meta["repo"]), meta["questions"]
    else:
        if not question_files:
            sys.exit("--questions is required for a new run")
        repo = repo or Path.cwd()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = BENCH_DIR / "runs" / (stamp + (f"-{args.tag}" if args.tag else ""))
        meta = {"started_at": stamp, "prompt": PROMPT, "repo": str(repo), "questions": question_files, "phases": []}
    if "code_search" in conditions and not index_current(repo).exists():
        sys.exit(f"no code-search index for {repo}. Build one first: code_search.py index --repo {repo}")

    questions = load_questions(question_files)
    if args.only:
        questions = [q for q in questions if q["id"] in set(args.only.split(","))]
    if args.limit:
        questions = questions[: args.limit]
    jobs = [{"question": q, "condition": c, "rep": r} for r in range(1, args.reps + 1) for q in questions for c in conditions]
    if args.run_dir:
        done_ids = {json.loads(l)["run_id"] for l in (run_dir / "results.jsonl").read_text().splitlines() if l.strip()}
        jobs = [j for j in jobs if f"{j['question']['id']}.{j['condition']}.r{j['rep']}" not in done_ids]
    for sub in ("streams", "logs", "usage"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    results_file = run_dir / "results.jsonl"
    prior = [json.loads(l) for l in results_file.read_text().splitlines() if l.strip()] if results_file.exists() else []
    spent = sum(r["total_cost_usd"] for r in prior)  # the budget covers every phase of the run dir

    phase = {"conditions": conditions, "concurrency": args.concurrency, "args": vars(args), "planned_runs": len(jobs),
             "env_before": environment_snapshot(repo, question_files)}
    meta["phases"].append(phase)
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"run dir: {run_dir}  conditions: {conditions}  planned runs: {len(jobs)}  already spent: ${spent:.2f}", flush=True)

    done, stopped_at, phase_spent = 0, None, 0.0
    pending = list(jobs)
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool, open(results_file, "a") as out:
        running = set()
        while pending or running:
            while pending and len(running) < args.concurrency and stopped_at is None:
                running.add(pool.submit(run_one, pending.pop(0), run_dir, repo))
            if not running:
                break
            finished, running = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished:
                row = fut.result()
                out.write(json.dumps(row) + "\n")
                out.flush()
                spent += row["total_cost_usd"]
                phase_spent += row["total_cost_usd"]
                done += 1
                print(f"[{done}/{len(jobs)}] {row['run_id']:<34} ok@3={row['correct_at_3']!s:<5} turns={row['num_turns']} "
                      f"tools={row['tool_calls_total']} blocks={row['hook_blocks']} cost=${row['total_cost_usd']:.3f} "
                      f"wall={row['wall_s']}s err={row['is_error']} spent=${spent:.2f}", flush=True)
                if spent >= args.budget and stopped_at is None:
                    stopped_at = {"after_runs": done, "spent_usd": round(spent, 4), "not_started": len(pending)}
                    print(f"budget ${args.budget} reached; not starting {len(pending)} queued runs", flush=True)
            if stopped_at is not None:
                pending = []

    phase.update(env_after=environment_snapshot(repo, question_files), budget_stop=stopped_at, spent_usd=round(phase_spent, 4))
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    summarize(run_dir, write_history=not args.no_history)


# --- summary -------------------------------------------------------------------------------

METRICS = ("wall_s", "num_turns", "tool_calls_total", "total_tokens", "total_cost_usd")


def stats(rows):
    n = len(rows)
    out = {"n": n}
    if not n:
        return out
    out["acc_at_1"] = round(sum(r["correct_at_1"] for r in rows) / n, 3)
    out["acc_at_3"] = round(sum(r["correct_at_3"] for r in rows) / n, 3)
    for m in METRICS + ("hook_blocks",):
        vals = [r.get(m) for r in rows if r.get(m) is not None]
        out[f"median_{m}"] = round(statistics.median(vals), 4) if vals else None
        out[f"mean_{m}"] = round(statistics.mean(vals), 4) if vals else None
    out["hook_blocks_total"] = sum(r.get("hook_blocks", 0) for r in rows)
    out["share_called_code_search"] = round(sum("mcp__code-search__code_search" in r["tool_calls"] for r in rows) / n, 3)
    out["errors"] = sum(r["is_error"] for r in rows)
    return out


def score(r):
    return 2 if r["correct_at_1"] else 1 if r["correct_at_3"] else 0


def paired(rows, control, treatment):
    """Per question, control minus treatment (positive = the treatment saved it). Reps are averaged first."""
    by_q = {}
    for r in rows:
        if r["num_turns"] is None or r["condition"] not in (control, treatment):  # no result event, nothing to compare
            continue
        by_q.setdefault(r["question_id"], {}).setdefault(r["condition"], []).append({**r, "score": score(r)})
    out = []
    for qid, conds in sorted(by_q.items()):
        if control not in conds or treatment not in conds:
            continue
        avg = {c: {m: statistics.mean(x[m] for x in conds[c]) for m in METRICS + ("score",)} for c in (control, treatment)}
        b, s = avg[control], avg[treatment]
        out.append({
            "question_id": qid, "set": conds[control][0]["set"], "lang": conds[control][0]["lang"], "kind": conds[control][0].get("kind"),
            "tokens_saved": round(b["total_tokens"] - s["total_tokens"]), "time_saved_s": round(b["wall_s"] - s["wall_s"], 2),
            "cost_saved_usd": round(b["total_cost_usd"] - s["total_cost_usd"], 4),
            "turns_saved": round(b["num_turns"] - s["num_turns"], 2), "tool_calls_saved": round(b["tool_calls_total"] - s["tool_calls_total"], 2),
            "outcome": "win" if s["score"] > b["score"] else "loss" if s["score"] < b["score"] else "tie",
            "control_score": b["score"], "treatment_score": s["score"],
        })
    return out


def paired_stats(pairs):
    if not pairs:
        return {"n": 0}
    out = {"n": len(pairs)}
    for k in ("tokens_saved", "time_saved_s", "cost_saved_usd", "turns_saved", "tool_calls_saved"):
        vals = [p[k] for p in pairs]
        out[f"median_{k}"] = round(statistics.median(vals), 4)
        out[f"mean_{k}"] = round(statistics.mean(vals), 4)
        out[f"share_positive_{k}"] = round(sum(v > 0 for v in vals) / len(vals), 3)
    for o in ("win", "tie", "loss"):
        out[o] = sum(p["outcome"] == o for p in pairs)
    return out


def user_effort():
    try:
        return json.loads((Path.home() / ".claude" / "settings.json").read_text()).get("effortLevel", "default")
    except (OSError, json.JSONDecodeError):
        return "unknown"


def setup(meta, rows, conditions):
    concurrency = {c: ph["concurrency"] for ph in phases(meta) for c in ph["conditions"]}
    placeholder = {"q": "<question>"}
    first = {c: next((r for r in rows if r["condition"] == c), {}) for c in conditions}
    return {
        "models": sorted({r["model"] for r in rows if r["model"]}),
        "claude_version": phases(meta)[0]["env_before"]["claude_version"],
        "prompt": PROMPT, "per_run_budget_usd": PER_RUN_BUDGET,
        "mcp": "--strict-mcp-config in every condition: no user, project or plugin MCP servers load",
        "child_env": "parent CLAUDE* env vars dropped; cwd is the repo; user and project settings and CLAUDE.md load as usual",
        "effort": f"not set by the harness; user setting effortLevel={user_effort()}",
        "conditions": {c: {
            "hooks": "on (your user and plugin hooks)" if "disableAllHooks" not in CONDITION_SETTINGS[c] else "off (disableAllHooks)",
            "settings": CONDITION_SETTINGS[c], "concurrency": concurrency.get(c),
            "tools_seen_at_init": first[c].get("init_tools"), "mcp_servers_seen_at_init": first[c].get("init_mcp_servers"),
            "flags": command(placeholder, c, "<run_dir>/usage/<run_id>.jsonl")[3:],
        } for c in conditions},
    }


def env_changes(meta):
    """Fields that differ between any phase's start and end, or between the first start and the last end."""
    snaps = []
    for ph in phases(meta):
        snaps += [ph["env_before"], ph.get("env_after")]
    snaps = [s for s in snaps if s]
    changed = {}
    for k in snaps[0]:
        if k == "claude_version":
            continue
        vals = [s.get(k) for s in snaps]
        if len(set(map(str, vals))) > 1:
            changed[k] = vals
    return changed


def summarize(run_dir, write_history=False):
    run_dir = Path(run_dir).resolve()
    rows = [json.loads(l) for l in (run_dir / "results.jsonl").read_text().splitlines() if l.strip()]
    meta = json.loads((run_dir / "meta.json").read_text())
    conditions = [c for c in CONDITIONS if any(r["condition"] == c for r in rows)]
    groups = {"all": rows}
    for key in ("set", "lang", "kind"):
        for val in sorted({r.get(key) for r in rows} - {None}):
            groups[f"{key}={val}"] = [r for r in rows if r.get(key) == val]
    by_condition = {g: {c: stats([r for r in sub if r["condition"] == c]) for c in conditions} for g, sub in groups.items()}
    comparisons = {}
    for control, treatment in COMPARISONS:
        if control not in conditions or treatment not in conditions:
            continue
        pairs = paired(rows, control, treatment)
        pair_groups = {"all": pairs}
        for key in ("set", "lang", "kind"):
            for val in sorted({p.get(key) for p in pairs} - {None}):
                pair_groups[f"{key}={val}"] = [p for p in pairs if p.get(key) == val]
        comparisons[f"{treatment}_vs_{control}"] = {
            "sign": f"{control} minus {treatment}; positive = {treatment} saved it",
            "stats": {g: paired_stats(p) for g, p in pair_groups.items()}, "per_question": pairs,
        }
    cold = [c["latency_ms"] for r in rows for c in r["cs_server_calls"] if c["cold"]]
    warm = [c["latency_ms"] for r in rows for c in r["cs_server_calls"] if not c["cold"]]
    summary = {
        "run_dir": str(run_dir), "setup": setup(meta, rows, conditions), "runs": len(rows),
        "spent_usd": round(sum(r["total_cost_usd"] for r in rows), 4),
        "budget_stops": [ph.get("budget_stop") for ph in phases(meta)],
        "errored_runs": [r["run_id"] for r in rows if r["is_error"]],
        "env_changed": env_changes(meta),
        "by_condition": by_condition, "comparisons": comparisons,
        "code_search_server": {
            "cold_calls": len(cold), "cold_median_ms": statistics.median(cold) if cold else None,
            "warm_calls": len(warm), "warm_median_ms": statistics.median(warm) if warm else None,
            "calls_per_run_median": statistics.median([len(r["cs_server_calls"]) for r in rows if r["condition"] == "code_search"] or [0]),
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    if write_history:
        write_history_row(run_dir, meta, summary)
    print_tables(summary)
    return summary


def write_history_row(run_dir, meta, summary):
    """One row per run dir: a re-summarized run replaces its earlier row."""
    first = phases(meta)[0]["env_before"]
    build = json.loads((index_current(Path(meta["repo"])) / "build.json").read_text())
    row = {
        "timestamp": meta["started_at"], "git_sha": first["git_head"], "models": summary["setup"]["models"],
        "config": {"index": build["cfg"], "index_head": build["head"], "code_search_sha256": first["code_search_sha256"],
                   "claude_version": first["claude_version"]},
        "n": summary["runs"], "spent_usd": summary["spent_usd"],
        "conditions": summary["by_condition"]["all"],
        "paired": {k: v["stats"]["all"] for k, v in summary["comparisons"].items()}, "run_dir": str(run_dir),
    }
    history = BENCH_DIR / "history.jsonl"
    kept = [l for l in history.read_text().splitlines() if l.strip() and json.loads(l).get("run_dir") != str(run_dir)] if history.exists() else []
    history.write_text("".join(l + "\n" for l in kept + [json.dumps(row)]))


def print_tables(summary):
    cols = ("n", "acc_at_1", "acc_at_3", "median_wall_s", "mean_wall_s", "median_num_turns", "mean_num_turns",
            "median_tool_calls_total", "mean_tool_calls_total", "median_total_tokens", "mean_total_tokens",
            "median_total_cost_usd", "mean_total_cost_usd", "share_called_code_search", "hook_blocks_total", "median_hook_blocks", "errors")
    print(f"\nruns={summary['runs']} spent=${summary['spent_usd']} models={summary['setup']['models']} errored={summary['errored_runs']}")
    for group, conds in summary["by_condition"].items():
        print(f"\n== {group}")
        for c, s in conds.items():
            print(f"  {c:<15} " + " ".join(f"{k.replace('median_', 'med_').replace('_total', '')}={s.get(k)}" for k in cols))
        for name, comp in summary["comparisons"].items():
            p = comp["stats"].get(group)
            if p:
                print(f"  {name}: " + " ".join(f"{k}={v}" for k, v in p.items()))
    print("\ncode_search server:", summary["code_search_server"])
    print("env changed:", summary["env_changed"] or "nothing")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--repo", help="checkout to run Claude Code in (default: current directory)")
    p.add_argument("--questions", nargs="+", metavar="FILE", help="question files, one set each (schema: eval/example.json)")
    p.add_argument("--conditions", default=DEFAULT_CONDITIONS, help=f"comma-separated, from {','.join(CONDITIONS)}")
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--budget", type=float, default=40.0, help="stop starting runs once the run dir's total spend reaches this")
    p.add_argument("--limit", type=int, help="first N questions only (smoke runs)")
    p.add_argument("--only", help="comma-separated question ids")
    p.add_argument("--run-dir", help="add a phase to an existing run dir; runs already recorded there are skipped")
    p.add_argument("--tag", help="suffix for a new run dir name")
    p.add_argument("--no-history", action="store_true", help="do not write history.jsonl (smoke runs)")
    p = sub.add_parser("summarize")
    p.add_argument("run_dir")
    p.add_argument("--history", action="store_true", help="also write this run's row to history.jsonl")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a)
    else:
        summarize(a.run_dir, write_history=a.history)


if __name__ == "__main__":
    sys.exit(main())
