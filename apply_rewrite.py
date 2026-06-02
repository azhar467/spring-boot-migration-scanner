#!/usr/bin/env python3
"""
apply_rewrite.py  --  Phase 2 of the migration tooling (LOCAL, code-mutating).

This is the companion to scan_single.py. The scanner (Phase 1) is read-only and
never clones. THIS script is the opposite: it clones repos locally and runs
OpenRewrite to actually modify code. Keep them separate on purpose.

PER REPO it will:
  1. resolve the git clone URL from the numeric project id (one API call)
  2. clone into a workspace dir (or fetch if already cloned)
  3. checkout the base branch, then create a fresh migration branch
  4. place the scanner's rewrite.yml and run `mvn ... rewrite:run` with that
     repo's composite recipe (com.lfg.migration.<repo>)
  5. STOP -- it does NOT build, commit, or push.

You then, manually, per repo:
     cd <workspace>/<repo>
     git diff                 # review the changes
     mvn clean install        # validate the build
     git add -A && git commit -m "Boot 3.x migration via OpenRewrite"
     git push -u origin <branch>

=========================  PREREQUISITES  =========================
  * git can clone from your GitLab over HTTPS/SSH (test by hand first).
  * Maven + JDK 17 installed; `mvn -version` works.
  * Network access to Maven Central / your Nexus so OpenRewrite can pull
    rewrite-spring and rewrite-migrate-java recipe artifacts.
  * A rewrite.yml produced by scan_single.py --rewrite-yml.

=========================  HOW TO RUN  ============================
  # ALWAYS start with ONE repo and --dry-run to see the exact commands:
      python3 apply_rewrite.py --config config.yaml --repos kbmg --dry-run

  # Then run it for real on that one repo:
      python3 apply_rewrite.py --config config.yaml --repos kbmg

  # Review + build + push that repo by hand, then do the next:
      python3 apply_rewrite.py --config config.yaml --repos call-routing

  # Only once you trust it, you may batch (still reviews each by hand after):
      python3 apply_rewrite.py --config config.yaml --all

SAFETY: refuses to run unless you pass either --repos <list> or --all, so it
can never accidentally rewrite the whole fleet. --all still stops before any
build/commit/push for every repo.

CONFIG: reuses the same file/dict as the scanner, plus optional keys:
  workspace           dir for clones        (default ./migration-workspace)
  migration_branch    branch name           (default migration/boot3)
  recipe_artifacts    OpenRewrite coords     (default rewrite-spring + rewrite-migrate-java RELEASE)
  maven_cmd           maven executable       (default "mvn")
  clone_protocol      "https" or "ssh"       (default "https")
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_RECIPE_ARTIFACTS = (
    "org.openrewrite.recipe:rewrite-spring:RELEASE,"
    "org.openrewrite.recipe:rewrite-migrate-java:RELEASE"
)
REWRITE_PLUGIN = "org.openrewrite.maven:rewrite-maven-plugin:RELEASE:run"


# --- recipe naming: MUST match scan_single.export_rewrite_yml exactly ---
def recipe_name_for(repo_label):
    safe = re.sub(r"[^A-Za-z0-9]+", "-", str(repo_label)).strip("-")
    return "com.lfg.migration.%s" % safe


# --- minimal config loader (JSON, or YAML via PyYAML, or tiny fallback) ---
def load_config(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json"):
        return json.loads(text)
    try:
        import yaml  # noqa
        return yaml.safe_load(text)
    except ImportError:
        return _mini_yaml(text)


def _mini_yaml(text):
    # Same subset parser shape as the scanner; enough for this config.
    def coerce(v):
        v = v.strip()
        if v in ("", "~", "null"):
            return None
        if v.lower() == "true":
            return True
        if v.lower() == "false":
            return False
        if (v[:1], v[-1:]) in (('"', '"'), ("'", "'")):
            return v[1:-1]
        if re.fullmatch(r"-?\d+", v):
            return int(v)
        return v
    lines = []
    for raw in text.splitlines():
        if "#" in raw:
            q = None; out = []
            for ch in raw:
                if ch in "'\"":
                    q = None if q == ch else (q or ch)
                if ch == "#" and not q:
                    break
                out.append(ch)
            raw = "".join(out)
        if raw.strip():
            lines.append(raw.rstrip())
    cfg, i = {}, 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(" "):
            i += 1; continue
        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        if rest:
            cfg[key] = coerce(rest); i += 1; continue
        i += 1
        if i < len(lines) and lines[i].lstrip().startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith(" ") and lines[i].lstrip().startswith("- "):
                content = lines[i].lstrip()[2:].strip()
                indent = len(lines[i]) - len(lines[i].lstrip())
                if ":" in content:
                    k, _, v = content.partition(":")
                    item = {k.strip(): coerce(v.strip())}
                    i += 1
                    while i < len(lines):
                        ln = lines[i]; ind = len(ln) - len(ln.lstrip())
                        if ind <= indent or ln.lstrip().startswith("- "):
                            break
                        k2, _, v2 = ln.strip().partition(":")
                        item[k2.strip()] = coerce(v2.strip()); i += 1
                    items.append(item)
                else:
                    items.append(coerce(content)); i += 1
            cfg[key] = items
        else:
            sub = {}
            while i < len(lines) and lines[i].startswith(" "):
                k2, _, v2 = lines[i].strip().partition(":")
                sub[k2.strip()] = coerce(v2.strip()); i += 1
            cfg[key] = sub
    return cfg


# --- GitLab: resolve numeric id -> clone URL ---
def resolve_clone_url(gitlab_url, token, project_id, protocol):
    api = gitlab_url.rstrip("/") + "/api/v4"
    pid = urllib.parse.quote(str(project_id), safe="")
    req = urllib.request.Request("%s/projects/%s" % (api, pid))
    req.add_header("PRIVATE-TOKEN", token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    if protocol == "ssh":
        return info.get("ssh_url_to_repo") or info.get("http_url_to_repo")
    return info.get("http_url_to_repo") or info.get("ssh_url_to_repo")


# --- shell helper: log every command, honor dry-run ---
def run(cmd, cwd=None, dry=False, check=True):
    printable = " ".join(cmd)
    print("    $ %s%s" % (("(%s) " % cwd if cwd else ""), printable))
    if dry:
        return 0, "", ""
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    if out.strip():
        print(_indent(out))
    if err.strip():
        print(_indent(err))
    if check and p.returncode != 0:
        raise RuntimeError("command failed (%d): %s" % (p.returncode, printable))
    return p.returncode, out, err


def _indent(s):
    return "\n".join("      " + ln for ln in s.rstrip().splitlines())


def process_repo(repo, cfg, token, rewrite_yml_path, opts):
    pid = repo["id"] if isinstance(repo, dict) else repo
    label = (repo.get("name", str(pid)) if isinstance(repo, dict) else str(pid))
    base_ref = (repo.get("ref") if isinstance(repo, dict) else None) or cfg.get("default_ref", "master")
    recipe = recipe_name_for(label)
    workspace = opts["workspace"]
    repo_dir = os.path.join(workspace, re.sub(r"[^A-Za-z0-9._-]+", "-", str(label)))
    branch = opts["branch"]

    print("\n=== %s  (project %s, base %s) ===" % (label, pid, base_ref))
    print("    recipe: %s" % recipe)

    clone_url = resolve_clone_url(cfg["gitlab_url"], token, pid, opts["protocol"]) \
        if not opts["dry"] else "<resolved-at-runtime>"
    if opts["dry"]:
        print("    (dry-run) would resolve clone URL for project %s" % pid)

    # 1. clone or fetch
    if os.path.isdir(os.path.join(repo_dir, ".git")):
        run(["git", "fetch", "--all", "--prune"], cwd=repo_dir, dry=opts["dry"])
    else:
        os.makedirs(workspace, exist_ok=True)
        run(["git", "clone", clone_url, repo_dir], dry=opts["dry"])

    # 2. base branch, then fresh migration branch
    run(["git", "checkout", base_ref], cwd=repo_dir, dry=opts["dry"])
    run(["git", "pull", "--ff-only"], cwd=repo_dir, dry=opts["dry"], check=False)
    # delete pre-existing migration branch to keep runs idempotent
    run(["git", "branch", "-D", branch], cwd=repo_dir, dry=opts["dry"], check=False)
    run(["git", "checkout", "-b", branch], cwd=repo_dir, dry=opts["dry"])

    # 3. place rewrite.yml (back up any existing one)
    if not opts["dry"]:
        dest = os.path.join(repo_dir, "rewrite.yml")
        if os.path.isfile(dest):
            shutil.copy2(dest, dest + ".bak")
            print("    backed up existing rewrite.yml -> rewrite.yml.bak")
        shutil.copy2(rewrite_yml_path, dest)
        print("    placed rewrite.yml")
    else:
        print("    (dry-run) would copy %s -> %s/rewrite.yml" % (rewrite_yml_path, repo_dir))

    # 4. run OpenRewrite (does NOT build/commit/push)
    mvn = opts["maven"]
    cmd = [mvn, "-U", REWRITE_PLUGIN,
           "-Drewrite.activeRecipes=%s" % recipe,
           "-Drewrite.recipeArtifactCoordinates=%s" % opts["recipe_artifacts"]]
    run(cmd, cwd=repo_dir, dry=opts["dry"])

    # 5. report what changed; stop here
    if not opts["dry"]:
        _, out, _ = run(["git", "status", "--short"], cwd=repo_dir, check=False)
        changed = bool(out.strip())
        print("    RESULT: %s" % ("files changed -- review with `git diff`"
                                  if changed else "NO changes produced"))
    print("    NEXT (manual): cd %s && git diff && mvn clean install && git push -u origin %s"
          % (repo_dir, branch))
    return label


def main():
    ap = argparse.ArgumentParser(description="Phase 2: run OpenRewrite locally per repo")
    ap.add_argument("--config", default=None, help="same config as the scanner (YAML/JSON)")
    ap.add_argument("--token", default=None, help="GitLab PAT; else env GITLAB_TOKEN or config token")
    ap.add_argument("--rewrite-yml", default="rewrite.yml",
                    help="path to the rewrite.yml produced by the scanner")
    ap.add_argument("--repos", default=None,
                    help="comma-separated repo names/ids to process (safer than --all)")
    ap.add_argument("--all", action="store_true", help="process every repo in config")
    ap.add_argument("--dry-run", action="store_true",
                    help="print every git/mvn command without executing")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--branch", default=None)
    ap.add_argument("--maven", default=None)
    ap.add_argument("--recipe-artifacts", default=None)
    ap.add_argument("--protocol", default=None, choices=["https", "ssh"])
    args = ap.parse_args()

    # Config can come from file; fall back to importing the scanner's CONFIG.
    cfg = load_config(args.config)
    if cfg is None:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from scan_single import CONFIG as cfg  # type: ignore
        except Exception:
            print("ERROR: no --config and could not import CONFIG from scan_single.py",
                  file=sys.stderr)
            sys.exit(2)

    token = args.token or os.environ.get("GITLAB_TOKEN") or cfg.get("token") or ""
    if not token and not args.dry_run:
        print("ERROR: no token (needed to resolve clone URLs). Use --token, env, or config, "
              "or --dry-run.", file=sys.stderr)
        sys.exit(2)

    if not args.all and not args.repos:
        print("REFUSING to run with no selection. Pass --repos <names> (recommended, start "
              "with one) or --all.", file=sys.stderr)
        sys.exit(2)

    if not args.dry_run and not os.path.isfile(args.rewrite_yml):
        print("ERROR: rewrite.yml not found at %s. Generate it first:\n"
              "  python3 scan_single.py --config <cfg> --rewrite-yml" % args.rewrite_yml,
              file=sys.stderr)
        sys.exit(2)

    opts = {
        "workspace": args.workspace or cfg.get("workspace", "migration-workspace"),
        "branch": args.branch or cfg.get("migration_branch", "migration/boot3"),
        "maven": args.maven or cfg.get("maven_cmd", "mvn"),
        "recipe_artifacts": args.recipe_artifacts or cfg.get("recipe_artifacts", DEFAULT_RECIPE_ARTIFACTS),
        "protocol": args.protocol or cfg.get("clone_protocol", "https"),
        "dry": args.dry_run,
    }

    # Select repos
    all_repos = cfg["projects"]
    if args.all:
        selected = all_repos
    else:
        wanted = {w.strip() for w in args.repos.split(",")}
        selected = []
        for r in all_repos:
            name = str(r.get("name", r.get("id"))) if isinstance(r, dict) else str(r)
            rid = str(r.get("id")) if isinstance(r, dict) else str(r)
            if name in wanted or rid in wanted:
                selected.append(r)
        if not selected:
            print("ERROR: none of --repos matched config projects.", file=sys.stderr)
            sys.exit(2)

    print("Phase 2 / OpenRewrite apply%s" % ("  [DRY-RUN]" if args.dry_run else ""))
    print("workspace=%s  branch=%s  protocol=%s" % (opts["workspace"], opts["branch"], opts["protocol"]))
    print("recipe artifacts: %s" % opts["recipe_artifacts"])
    print("repos: %s" % ", ".join(
        str(r.get("name", r.get("id")) if isinstance(r, dict) else r) for r in selected))

    done = []
    for repo in selected:
        try:
            done.append(process_repo(repo, cfg, token, args.rewrite_yml, opts))
        except Exception as e:
            print("    ERROR on this repo: %s" % e, file=sys.stderr)
            print("    continuing to next repo.")

    print("\nProcessed %d repo(s). NOTHING was built, committed, or pushed -- that is your "
          "manual step per repo." % len(done))


if __name__ == "__main__":
    main()
