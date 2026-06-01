#!/usr/bin/env python3
"""
Migration Readiness Scanner -- SINGLE-FILE EDITION.

Same engine as the multi-file project, collapsed into one file for environments
where you can only paste/recreate a single file (locked VDI, no zip download, no
pip install). Rules are defined as Python dicts at the bottom instead of YAML, so
there is zero dependency beyond the Python 3 standard library.

Scans a fleet of GitLab repos over the REST API WITHOUT cloning, and writes a
self-contained HTML readiness dashboard (plus optional CSV/JSON). Nothing is
ever git-cloned; only the files a rule needs are fetched over HTTPS and held in
memory. The only thing written to disk is the report.

The multi-file version on GitHub is the reference/pitch copy; this is the VDI
runtime. To add a check: append a dict to RULES. Engine code never changes.

===========================================================================
 HOW TO RUN
===========================================================================

 0) Requirements: Python 3 only. No pip install. No network for --dry-run.

 1) SMOKE TEST FIRST (no token, no network) -- proves the file runs in VDI:

        python3 scan_single.py --dry-run

    Then open readiness-report.html in a browser. If you see the dashboard,
    the paste/recreate worked and Python is happy.

 2) CONFIGURE: edit the CONFIG dict directly below this docstring.
      - gitlab_url:        https://gitlab.lfg.com   (NO /api/v4 -- code adds it)
      - token:             paste your GitLab PAT (read_api scope) here
      - parent_pom_project: 2765                     (numeric project ID)
      - default_ref:       "master"  (your repos have master + develop;
                                       change to "develop" to scan that instead,
                                       or set "ref" per-project below)
      - projects:          list of {"id": <numeric>, "name": "<label>"}
                           e.g. {"id": 11681, "name": "kbmg"}

 3) REAL RUN against the fleet:

        python3 scan_single.py
        python3 scan_single.py --csv --json        # also write raw exports

    Outputs (next to this script, or next to --config if you pass one):
        readiness-report.html      the dashboard for Brian
        readiness-findings.csv     one row per repo+rule (with --csv)
        readiness-findings.json    full structured scan (with --json)

 TOKEN PRECEDENCE (first one found wins):
        --token <PAT>   >   env GITLAB_TOKEN   >   CONFIG["token"]
    So the in-config token is the default; env/flag override it when set.
    To keep the token OUT of the file, leave CONFIG["token"] = "" and run:
        export GITLAB_TOKEN=glpat-xxxx ; python3 scan_single.py

 BRANCH NOTE: there is no local clone to fall back on, so default_ref must
    match a branch that actually exists, or the tree call 404s. Your repos
    have master/develop; default is master.

 ADD A CHECK: append one dict to the RULES list near the bottom. Two types:
    file-pattern (regex over files) and pom-dependency (version / parent-POM
    divergence). No engine change -- that is the reusability story.

 ALTERNATIVELY: pass --config a small YAML or JSON file instead of editing the
    CONFIG dict. JSON needs no extra library; YAML uses a tiny built-in parser.

===========================================================================
"""

import argparse
import csv
import datetime
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


# ===========================================================================
# CONFIG -- edit here, or override with --config <file> (YAML or JSON)
# ===========================================================================

CONFIG = {
    # GitLab host only -- the code appends /api/v4 itself.
    # Your API is gitlab.lfg.com/api/v4/projects/<id>, so this is:
    "gitlab_url": "https://gitlab.lfg.com",

    # GitLab personal access token (read_api scope). Paste it here, OR leave
    # blank and use env GITLAB_TOKEN / --token. Precedence: --token > env > here.
    "token": "",

    # Your repos have master + develop. Default to master; change to "develop"
    # to scan that branch instead (or set "ref" per project below).
    "default_ref": "master",

    # Shared parent POM repo -- numeric project ID.
    "parent_pom_project": 2765,

    "output": "readiness-report.html",
    "export_csv": False,                          # True or "path.csv"
    "export_json": False,                         # True or "path.json"

    # The fleet. Numeric project IDs. name is just the display label; optional
    # per-project "ref" overrides default_ref (e.g. "ref": "develop").
    "projects": [
        {"id": 11681, "name": "kbmg"},
        # {"id": 11682, "name": "call-routing"},
        # {"id": 11683, "name": "rte"},
        # ... add the remaining services
    ],
}


# ===========================================================================
# GitLab client (urllib only, no clone)
# ===========================================================================

class GitLabClient:
    def __init__(self, base_url, token, per_page=100, retries=3, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.api = self.base_url + "/api/v4"
        self.token = token
        self.per_page = per_page
        self.retries = retries
        self.timeout = timeout

    def _request(self, url):
        req = urllib.request.Request(url)
        req.add_header("PRIVATE-TOKEN", self.token)
        last_err = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read(), dict(resp.headers)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503):
                    time.sleep(1.5 * (attempt + 1))
                    last_err = e
                    continue
                raise
            except urllib.error.URLError as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("GitLab request failed for %s: %s" % (url, last_err))

    def _pid(self, project_id):
        return urllib.parse.quote(str(project_id), safe="")

    def list_tree(self, project_id, ref="main"):
        pid = self._pid(project_id)
        results, page = [], 1
        while True:
            q = urllib.parse.urlencode({
                "ref": ref, "recursive": "true",
                "per_page": self.per_page, "page": page,
            })
            url = "%s/projects/%s/repository/tree?%s" % (self.api, pid, q)
            body, headers = self._request(url)
            for entry in json.loads(body.decode("utf-8")):
                if entry.get("type") == "blob":
                    results.append(entry["path"])
            nxt = headers.get("X-Next-Page") or headers.get("x-next-page")
            if not nxt:
                break
            page = int(nxt)
        return results

    def get_raw_file(self, project_id, file_path, ref="main"):
        pid = self._pid(project_id)
        fp = urllib.parse.quote(file_path, safe="")
        q = urllib.parse.urlencode({"ref": ref})
        url = "%s/projects/%s/repository/files/%s/raw?%s" % (self.api, pid, fp, q)
        try:
            body, _ = self._request(url)
            return body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def resolve_default_branch(self, project_id):
        url = "%s/projects/%s" % (self.api, self._pid(project_id))
        body, _ = self._request(url)
        return json.loads(body.decode("utf-8")).get("default_branch") or "main"


# ===========================================================================
# POM parsing + matchers
# ===========================================================================

def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _text(node):
    return (node.text or "").strip() if node is not None and node.text else None


def parse_pom(text):
    if not text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    def find(node, name):
        for child in node:
            if _strip_ns(child.tag) == name:
                return child
        return None

    result = {"parent": None, "properties": {}, "dependencies": []}
    parent = find(root, "parent")
    if parent is not None:
        result["parent"] = {
            "groupId": _text(find(parent, "groupId")),
            "artifactId": _text(find(parent, "artifactId")),
            "version": _text(find(parent, "version")),
        }
    props = find(root, "properties")
    if props is not None:
        for p in props:
            result["properties"][_strip_ns(p.tag)] = (p.text or "").strip()
    for container in ("dependencies", "dependencyManagement"):
        cnode = find(root, container)
        if cnode is None:
            continue
        if container == "dependencyManagement":
            cnode = find(cnode, "dependencies")
            if cnode is None:
                continue
        for dep in cnode:
            if _strip_ns(dep.tag) != "dependency":
                continue
            result["dependencies"].append({
                "groupId": _text(find(dep, "groupId")),
                "artifactId": _text(find(dep, "artifactId")),
                "version": _text(find(dep, "version")),
            })
    return result


def resolve_version(version, properties):
    if not version:
        return version
    m = re.fullmatch(r"\$\{([^}]+)\}", version.strip())
    return properties.get(m.group(1), version) if m else version


def _version_lt(a, b):
    def norm(v):
        nums = re.findall(r"\d+", v)
        return [int(n) for n in nums] or [0]
    aa, bb = norm(a), norm(b)
    n = max(len(aa), len(bb))
    aa += [0] * (n - len(aa))
    bb += [0] * (n - len(bb))
    return aa < bb


def run_file_pattern(rule, files_provider):
    pattern = re.compile(rule["pattern"])
    total, hit_files = 0, []
    for path, text in files_provider(rule.get("file_glob", "**/*")):
        if text is None:
            continue
        matches = pattern.findall(text)
        if matches:
            total += len(matches)
            hit_files.append({"path": path, "count": len(matches)})
    severity = rule.get("severity", "info")
    status = "fail" if total > 0 and severity in ("critical", "high") else (
        "warn" if total > 0 else "pass")
    return {
        "rule_id": rule["id"], "rule_name": rule["name"], "type": "file-pattern",
        "severity": severity, "status": status, "total_hits": total,
        "files": sorted(hit_files, key=lambda f: -f["count"]),
        "description": rule.get("description", ""),
    }


def run_pom_dependency(rule, pom_view, parent_pom_view=None):
    severity = rule.get("severity", "info")
    gid, aid = rule.get("group_id"), rule["artifact_id"]
    found = None
    props = (pom_view or {}).get("properties", {}) if pom_view else {}
    if pom_view:
        if pom_view.get("parent") and pom_view["parent"].get("artifactId") == aid:
            found = dict(pom_view["parent"])
        for dep in pom_view.get("dependencies", []):
            if dep.get("artifactId") == aid and (not gid or dep.get("groupId") == gid):
                found = dict(dep)
                break
    detail, status = {}, "pass"
    if found:
        ver = resolve_version(found.get("version"), props)
        detail["version"] = ver
        if rule.get("bad_version_regex") and ver and re.search(rule["bad_version_regex"], ver):
            status = "fail" if severity in ("critical", "high") else "warn"
            detail["reason"] = "matches incompatible version pattern"
        if rule.get("min_version") and ver and _version_lt(ver, rule["min_version"]):
            status = "fail" if severity in ("critical", "high") else "warn"
            detail["reason"] = "below required minimum %s" % rule["min_version"]
    if rule.get("check_parent_divergence") and parent_pom_view and found:
        pprops = parent_pom_view.get("properties", {})
        pver = None
        if parent_pom_view.get("parent") and parent_pom_view["parent"].get("artifactId") == aid:
            pver = resolve_version(parent_pom_view["parent"].get("version"), pprops)
        for dep in parent_pom_view.get("dependencies", []):
            if dep.get("artifactId") == aid:
                pver = resolve_version(dep.get("version"), pprops)
                break
        cver = detail.get("version")
        if pver and cver and pver != cver:
            detail["diverges_from_parent"] = True
            detail["parent_version"] = pver
            if status == "pass":
                status = "warn"
            detail["reason"] = (detail.get("reason", "") +
                                " | overrides parent (%s vs %s)" % (cver, pver)).strip(" |")
    return {
        "rule_id": rule["id"], "rule_name": rule["name"], "type": "pom-dependency",
        "severity": severity, "status": status if found else "pass",
        "present": bool(found), "detail": detail,
        "description": rule.get("description", ""),
    }


# ===========================================================================
# Repo source (API or dry-run local)
# ===========================================================================

SOURCE_GLOB_EXTS = {
    "**/*.java": (".java",), "**/*.yml": (".yml", ".yaml"),
    "**/*.yaml": (".yml", ".yaml"), "**/*.properties": (".properties",),
    "**/*.xml": (".xml",),
}


class RepoSource:
    def __init__(self, client, project_id, ref, dry_run_files=None):
        self.client = client
        self.project_id = project_id
        self.ref = ref
        self.dry_run_files = dry_run_files  # dict path->text, or None
        self._tree = None
        self._cache = {}

    def tree(self):
        if self._tree is None:
            if self.dry_run_files is not None:
                self._tree = list(self.dry_run_files.keys())
            else:
                self._tree = self.client.list_tree(self.project_id, self.ref)
        return self._tree

    def get(self, path):
        if path in self._cache:
            return self._cache[path]
        if self.dry_run_files is not None:
            text = self.dry_run_files.get(path)
        else:
            text = self.client.get_raw_file(self.project_id, path, self.ref)
        self._cache[path] = text
        return text

    def files_for_glob(self, glob):
        exts = SOURCE_GLOB_EXTS.get(glob)
        for path in self.tree():
            if exts and not path.endswith(exts):
                continue
            yield path, self.get(path)

    def pom(self):
        return parse_pom(self.get("pom.xml"))


# ===========================================================================
# Renderer (self-contained HTML)
# ===========================================================================

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
STATUS_COLOR = {"fail": "#c0392b", "warn": "#d68910", "pass": "#1e8449"}
STATUS_LABEL = {"fail": "BLOCKER", "warn": "REVIEW", "pass": "CLEAR"}


def _repo_status(findings):
    if any(f["status"] == "fail" for f in findings):
        return "fail"
    if any(f["status"] == "warn" for f in findings):
        return "warn"
    return "pass"


def _worst_sev(findings):
    active = [f for f in findings if f["status"] != "pass"]
    if not active:
        return "info"
    return sorted(active, key=lambda f: SEV_ORDER.get(f["severity"], 9))[0]["severity"]


def render_html(scan, ruleset_name, output_path):
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(scan)
    blockers = sum(1 for r in scan if _repo_status(r["findings"]) == "fail")
    reviews = sum(1 for r in scan if _repo_status(r["findings"]) == "warn")
    clears = total - blockers - reviews
    rows = [_repo_block(r) for r in sorted(
        scan, key=lambda x: SEV_ORDER.get(_worst_sev(x["findings"]), 9))]
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(_TEMPLATE.format(
            ruleset=html.escape(ruleset_name), generated=generated,
            total=total, blockers=blockers, reviews=reviews, clears=clears,
            rows="\n".join(rows)))
    return output_path


def _repo_block(repo):
    status = _repo_status(repo["findings"])
    color, label = STATUS_COLOR[status], STATUS_LABEL[status]
    finding_rows = []
    for f in sorted(repo["findings"], key=lambda x: SEV_ORDER.get(x["severity"], 9)):
        if f["status"] == "pass" and f["type"] == "file-pattern" and f.get("total_hits", 0) == 0:
            detail = "<span class='muted'>no matches</span>"
        elif f["type"] == "file-pattern":
            top = ", ".join("%s (%d)" % (html.escape(x["path"].split("/")[-1]), x["count"])
                            for x in f["files"][:3])
            detail = "<strong>%d</strong> hits &nbsp;<span class='muted'>%s</span>" % (
                f["total_hits"], top)
        else:
            d = f.get("detail", {})
            if not f.get("present"):
                detail = "<span class='muted'>not present</span>"
            else:
                bits = []
                if d.get("version"):
                    bits.append("version <code>%s</code>" % html.escape(str(d["version"])))
                if d.get("diverges_from_parent"):
                    bits.append("<span class='diverge'>diverges from parent (%s)</span>"
                                % html.escape(str(d.get("parent_version", "?"))))
                if d.get("reason"):
                    bits.append("<span class='muted'>%s</span>" % html.escape(d["reason"]))
                detail = " &middot; ".join(bits) if bits else "present"
        finding_rows.append(
            "<tr class='f-{st}'><td class='sev sev-{sev}'>{sev}</td>"
            "<td>{name}</td><td class='st st-{st}'>{stlabel}</td>"
            "<td class='detail'>{detail}</td></tr>".format(
                st=f["status"], sev=html.escape(f["severity"]),
                name=html.escape(f["rule_name"]),
                stlabel=STATUS_LABEL[f["status"]], detail=detail))
    return _REPO_TEMPLATE.format(repo=html.escape(repo["repo"]), color=color,
                                 label=label, findings="\n".join(finding_rows))


_REPO_TEMPLATE = """
<section class="repo">
  <header class="repo-head" style="border-left:6px solid {color}">
    <h2>{repo}</h2><span class="badge" style="background:{color}">{label}</span>
  </header>
  <table class="findings">
    <thead><tr><th>Sev</th><th>Rule</th><th>Status</th><th>Detail</th></tr></thead>
    <tbody>
{findings}
    </tbody>
  </table>
</section>
"""

_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Migration Readiness &mdash; {ruleset}</title>
<style>
  :root {{ --bg:#0f1419; --panel:#1a222c; --ink:#e6edf3; --muted:#8b98a5;
    --line:#2a3441; --accent:#4a9eff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:"IBM Plex Sans","Segoe UI",system-ui,sans-serif; line-height:1.5; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:48px 24px 80px; }}
  .top {{ border-bottom:1px solid var(--line); padding-bottom:24px; margin-bottom:32px; }}
  .eyebrow {{ letter-spacing:.18em; text-transform:uppercase; font-size:.7rem;
    color:var(--accent); font-weight:600; }}
  h1 {{ margin:.2em 0 .1em; font-size:1.9rem; font-weight:650;
    font-family:"IBM Plex Mono",ui-monospace,monospace; }}
  .meta {{ color:var(--muted); font-size:.85rem; }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:28px 0 40px; }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:18px 20px; }}
  .card .n {{ font-size:2rem; font-weight:700; font-family:"IBM Plex Mono",monospace; }}
  .card .l {{ color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.1em; }}
  .card.fail .n {{ color:#ff6b5e; }} .card.warn .n {{ color:#f0b429; }} .card.pass .n {{ color:#3ddc84; }}
  .repo {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
    margin-bottom:18px; overflow:hidden; }}
  .repo-head {{ display:flex; align-items:center; justify-content:space-between;
    padding:14px 20px; background:#141b24; }}
  .repo-head h2 {{ margin:0; font-size:1.05rem; font-family:"IBM Plex Mono",monospace; font-weight:550; }}
  .badge {{ color:#fff; font-size:.68rem; font-weight:700; letter-spacing:.08em;
    padding:4px 10px; border-radius:20px; }}
  table.findings {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
  table.findings th {{ text-align:left; color:var(--muted); font-weight:500; font-size:.7rem;
    text-transform:uppercase; letter-spacing:.08em; padding:10px 20px; border-bottom:1px solid var(--line); }}
  table.findings td {{ padding:11px 20px; border-bottom:1px solid var(--line); vertical-align:top; }}
  table.findings tr:last-child td {{ border-bottom:none; }}
  .sev {{ font-size:.68rem; text-transform:uppercase; letter-spacing:.06em; font-weight:700; white-space:nowrap; }}
  .sev-critical {{ color:#ff6b5e; }} .sev-high {{ color:#ff8a5e; }} .sev-medium {{ color:#f0b429; }}
  .sev-low {{ color:#8b98a5; }} .sev-info {{ color:#6b7886; }}
  .st {{ font-size:.68rem; font-weight:700; letter-spacing:.05em; white-space:nowrap; }}
  .st-fail {{ color:#ff6b5e; }} .st-warn {{ color:#f0b429; }} .st-pass {{ color:#3ddc84; }}
  .f-pass td {{ opacity:.55; }}
  .detail code {{ background:#0c1117; padding:1px 6px; border-radius:4px;
    font-family:"IBM Plex Mono",monospace; font-size:.8rem; }}
  .muted {{ color:var(--muted); }} .diverge {{ color:#f0b429; font-weight:600; }}
  .foot {{ margin-top:40px; color:var(--muted); font-size:.78rem;
    border-top:1px solid var(--line); padding-top:18px; }}
  @media (max-width:680px) {{ .cards {{ grid-template-columns:repeat(2,1fr); }} }}
</style></head><body><div class="wrap">
  <div class="top"><div class="eyebrow">Migration Readiness Report</div>
    <h1>{ruleset}</h1>
    <div class="meta">Generated {generated} &middot; scanned via GitLab API (no clone)</div></div>
  <div class="cards">
    <div class="card"><div class="n">{total}</div><div class="l">Services</div></div>
    <div class="card fail"><div class="n">{blockers}</div><div class="l">Blockers</div></div>
    <div class="card warn"><div class="n">{reviews}</div><div class="l">Needs review</div></div>
    <div class="card pass"><div class="n">{clears}</div><div class="l">Clear</div></div></div>
  {rows}
  <div class="foot">Rule-driven scan. Engine fixed; checks defined as versioned rules.
    Re-run after migration to verify remediation. &mdash; deterministic, no AI, runs in-VDI.</div>
</div></body></html>
"""


# ===========================================================================
# CSV / JSON exporters
# ===========================================================================

def _flatten(fnd):
    if fnd["type"] == "file-pattern":
        top = "; ".join("%s=%d" % (x["path"], x["count"]) for x in fnd.get("files", [])[:5])
        return fnd.get("total_hits", 0), top
    d = fnd.get("detail", {})
    if not fnd.get("present"):
        return "", "not present"
    bits = []
    if d.get("version"):
        bits.append("version=%s" % d["version"])
    if d.get("diverges_from_parent"):
        bits.append("diverges_from_parent=%s" % d.get("parent_version", "?"))
    if d.get("reason"):
        bits.append(d["reason"])
    return d.get("version", "present"), " | ".join(bits)


def export_csv(scan, output_path):
    cols = ["repo", "rule_id", "rule_name", "type", "severity", "status", "metric", "detail"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for repo in scan:
            for fnd in repo["findings"]:
                metric, detail = _flatten(fnd)
                w.writerow({"repo": repo["repo"], "rule_id": fnd["rule_id"],
                            "rule_name": fnd["rule_name"], "type": fnd["type"],
                            "severity": fnd["severity"], "status": fnd["status"],
                            "metric": metric, "detail": detail})
    return output_path


def export_json(scan, ruleset_name, output_path):
    statuses = [_repo_status(r["findings"]) for r in scan]
    payload = {
        "ruleset": ruleset_name,
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "summary": {"services": len(scan), "blockers": statuses.count("fail"),
                    "needs_review": statuses.count("warn"), "clear": statuses.count("pass")},
        "repos": scan,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return output_path


# ===========================================================================
# Optional tiny config loader (YAML subset or JSON). Only used if --config given.
# ===========================================================================

def load_config_file(path):
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
    # Minimal YAML for the small config shape used here. For complex configs,
    # prefer JSON or installing PyYAML. Handles scalars, nested maps, and the
    # projects list of {id,name,ref} maps.
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
            q = None
            out = []
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
            i += 1
            continue
        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        if rest:
            cfg[key] = coerce(rest)
            i += 1
            continue
        # block: list or map nested
        i += 1
        if i < len(lines) and lines[i].lstrip().startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith(" ") and lines[i].lstrip().startswith("- "):
                content = lines[i].lstrip()[2:].strip()
                indent = len(lines[i]) - len(lines[i].lstrip())
                item = {}
                if ":" in content:
                    k, _, v = content.partition(":")
                    item[k.strip()] = coerce(v.strip())
                    i += 1
                    while i < len(lines):
                        ln = lines[i]
                        ind = len(ln) - len(ln.lstrip())
                        if ind <= indent or ln.lstrip().startswith("- "):
                            break
                        k2, _, v2 = ln.strip().partition(":")
                        item[k2.strip()] = coerce(v2.strip())
                        i += 1
                    items.append(item)
                else:
                    items.append(coerce(content))
                    i += 1
            cfg[key] = items
        else:
            sub = {}
            while i < len(lines) and lines[i].startswith(" "):
                k2, _, v2 = lines[i].strip().partition(":")
                sub[k2.strip()] = coerce(v2.strip())
                i += 1
            cfg[key] = sub
    return cfg


# ===========================================================================
# Orchestrator
# ===========================================================================

def run_scan(cfg, token, dry_run, csv_flag, json_flag, base_dir):
    ruleset_name = "spring-boot-3"
    client = None
    if not dry_run:
        if not token:
            print("ERROR: no token. Put it in CONFIG['token'], set env GITLAB_TOKEN, "
                  "pass --token, or use --dry-run.", file=sys.stderr)
            sys.exit(2)
        client = GitLabClient(cfg["gitlab_url"], token)

    def make_source(pid, ref):
        if dry_run:
            return RepoSource(None, pid, ref, dry_run_files=DRY_RUN_SAMPLES.get(str(pid), {}))
        return RepoSource(client, pid, ref)

    # Under --dry-run, scan the bundled sample fleet instead of the configured
    # (numeric, real) projects, so the smoke test works no matter what CONFIG
    # holds. Real runs use cfg as provided.
    if dry_run:
        cfg = {
            "default_ref": "master",
            "parent_pom_project": "platform/parent-pom",
            "output": cfg.get("output", "readiness-report.html"),
            "projects": [
                {"id": "lfg/services/kbmg", "name": "kbmg"},
                {"id": "lfg/services/call-routing", "name": "call-routing"},
                {"id": "lfg/services/rte", "name": "rte"},
            ],
        }

    parent_pom_view = None
    if cfg.get("parent_pom_project"):
        ref = cfg.get("default_ref", "master")
        parent_pom_view = make_source(cfg["parent_pom_project"], ref).pom()

    scan = []
    for repo in cfg["projects"]:
        if isinstance(repo, dict):
            pid = repo["id"]
            label = repo.get("name", str(pid))
            ref = repo.get("ref") or cfg.get("default_ref", "master")
        else:
            pid = label = str(repo)
            ref = cfg.get("default_ref", "master")
        source = make_source(pid, ref)
        pom_view = source.pom()
        findings = []
        for rule in RULES:
            if rule["type"] == "file-pattern":
                findings.append(run_file_pattern(rule, source.files_for_glob))
            elif rule["type"] == "pom-dependency":
                findings.append(run_pom_dependency(rule, pom_view, parent_pom_view))
        scan.append({"repo": label, "findings": findings})
        print("scanned: %s" % label, file=sys.stderr)

    out = os.path.join(base_dir, cfg.get("output", "readiness-report.html"))
    render_html(scan, ruleset_name, out)
    print("\nReport written to: %s" % out)

    csv_target = csv_flag or cfg.get("export_csv")
    if csv_target:
        p = os.path.join(base_dir, csv_target if isinstance(csv_target, str) else "readiness-findings.csv")
        export_csv(scan, p)
        print("CSV written to:    %s" % p)
    json_target = json_flag or cfg.get("export_json")
    if json_target:
        p = os.path.join(base_dir, json_target if isinstance(json_target, str) else "readiness-findings.json")
        export_json(scan, ruleset_name, p)
        print("JSON written to:   %s" % p)


def main():
    ap = argparse.ArgumentParser(description="Migration readiness scanner (single-file)")
    ap.add_argument("--config", default=None, help="optional YAML/JSON config; else uses CONFIG dict")
    ap.add_argument("--token", default=None, help="GitLab PAT; overrides env and config")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--csv", nargs="?", const=True, default=None)
    ap.add_argument("--json", nargs="?", const=True, default=None)
    args = ap.parse_args()

    if args.config:
        cfg = load_config_file(args.config)
        base_dir = os.path.dirname(os.path.abspath(args.config))
    else:
        cfg = CONFIG
        base_dir = os.getcwd()

    # Token precedence: --token  >  env GITLAB_TOKEN  >  CONFIG["token"].
    token = args.token or os.environ.get("GITLAB_TOKEN") or cfg.get("token") or ""
    run_scan(cfg, token, args.dry_run, args.csv, args.json, base_dir)


# ===========================================================================
# RULES -- the ruleset, as data. Add a dict to add a check; no engine change.
# Mirrors rulesets/spring-boot-3/*.yml from the multi-file version.
# ===========================================================================

RULES = [
    # --- OpenSearch 1.3 / Elasticsearch client (highest value) ---
    {"id": "opensearch-rhlc-usage", "type": "file-pattern",
     "name": "RestHighLevelClient usage (incompatible with Boot 3.x client)",
     "severity": "critical", "file_glob": "**/*.java",
     "pattern": r"RestHighLevelClient|org\.elasticsearch\.client\.RequestOptions",
     "description": "RHLC is removed/incompatible in the Boot 3.x Elasticsearch stack; "
                    "breaks against OpenSearch 1.3."},
    {"id": "spring-data-es-dep", "type": "pom-dependency",
     "name": "spring-boot-starter-data-elasticsearch present", "severity": "high",
     "artifact_id": "spring-boot-starter-data-elasticsearch", "check_parent_divergence": True,
     "description": "Under Boot 3.x the client is incompatible with OpenSearch 1.3; "
                    "needs an explicit client decision."},
    {"id": "legacy-es-rest-dep", "type": "pom-dependency",
     "name": "elasticsearch-rest-high-level-client dependency", "severity": "critical",
     "artifact_id": "elasticsearch-rest-high-level-client",
     "description": "Legacy high-level REST client; must be replaced before Boot 3.x."},

    # --- javax -> jakarta ---
    {"id": "javax-persistence", "type": "file-pattern",
     "name": "javax.persistence imports (-> jakarta.persistence)", "severity": "high",
     "file_glob": "**/*.java", "pattern": r"javax\.persistence\.",
     "description": "JPA annotations moved to jakarta.persistence."},
    {"id": "javax-servlet", "type": "file-pattern",
     "name": "javax.servlet imports (-> jakarta.servlet)", "severity": "high",
     "file_glob": "**/*.java", "pattern": r"javax\.servlet\.",
     "description": "Servlet API moved to jakarta.servlet."},
    {"id": "javax-validation", "type": "file-pattern",
     "name": "javax.validation imports (-> jakarta.validation)", "severity": "medium",
     "file_glob": "**/*.java", "pattern": r"javax\.validation\.",
     "description": "Bean Validation moved to jakarta.validation."},
    {"id": "javax-annotation", "type": "file-pattern",
     "name": "javax.annotation imports (-> jakarta.annotation)", "severity": "medium",
     "file_glob": "**/*.java", "pattern": r"javax\.annotation\.(PostConstruct|PreDestroy|Resource)",
     "description": "Common annotations moved to jakarta.annotation."},

    # --- config keys + parent POM ---
    {"id": "parent-boot-version", "type": "pom-dependency",
     "name": "Spring Boot parent version", "severity": "high",
     "artifact_id": "spring-boot-starter-parent", "bad_version_regex": r"^2\.",
     "check_parent_divergence": True,
     "description": "Reports inherited Boot version; flags 2.x and children diverging from parent."},
    {"id": "redis-deprecated-keys", "type": "file-pattern",
     "name": "Deprecated spring.redis.* keys (-> spring.data.redis.*)", "severity": "medium",
     "file_glob": "**/*.yml", "pattern": r"spring\.redis\.|spring:\s*\n\s+redis:",
     "description": "Redis properties moved under spring.data.redis."},
    {"id": "deprecated-security-adapter", "type": "file-pattern",
     "name": "WebSecurityConfigurerAdapter (removed in Spring Security 6)", "severity": "high",
     "file_glob": "**/*.java", "pattern": r"WebSecurityConfigurerAdapter",
     "description": "Removed in Spring Security 6; migrate to SecurityFilterChain."},
    {"id": "deprecated-batch-properties", "type": "file-pattern",
     "name": "spring.batch.initialize-schema (renamed)", "severity": "low",
     "file_glob": "**/*.yml", "pattern": r"spring\.batch\.initialize-schema",
     "description": "Renamed to spring.batch.jdbc.initialize-schema."},
]


# ===========================================================================
# DRY_RUN_SAMPLES -- tiny fixtures so --dry-run works with no GitLab/token.
# Delete this block if you want a leaner file; --dry-run just won't work then.
# ===========================================================================

DRY_RUN_SAMPLES = {
    "platform/parent-pom": {
        "pom.xml": """<project><parent>
<groupId>org.springframework.boot</groupId>
<artifactId>spring-boot-starter-parent</artifactId>
<version>2.7.12</version></parent>
<groupId>com.lfg.platform</groupId><artifactId>parent-pom</artifactId>
<dependencyManagement><dependencies><dependency>
<groupId>org.springframework.boot</groupId>
<artifactId>spring-boot-starter-data-elasticsearch</artifactId>
</dependency></dependencies></dependencyManagement></project>""",
    },
    "lfg/services/kbmg": {
        "pom.xml": """<project><parent>
<groupId>org.springframework.boot</groupId>
<artifactId>spring-boot-starter-parent</artifactId>
<version>2.7.12</version></parent><artifactId>kbmg</artifactId>
<dependencies><dependency>
<groupId>org.springframework.boot</groupId>
<artifactId>spring-boot-starter-data-elasticsearch</artifactId>
</dependency></dependencies></project>""",
        "src/main/java/com/lfg/Repo.java": "import javax.persistence.Entity;\n"
            "import javax.persistence.Id;\nimport javax.servlet.http.HttpServletRequest;\n"
            "import org.elasticsearch.client.RestHighLevelClient;\n"
            "import org.elasticsearch.client.RequestOptions;\n"
            "@Entity public class Repo { @Id Long id; RestHighLevelClient client; }",
        "src/main/java/com/lfg/Security.java": "import javax.servlet.Filter;\n"
            "public class Security extends WebSecurityConfigurerAdapter { javax.validation.Valid v; }",
    },
    "lfg/services/call-routing": {
        "pom.xml": """<project><parent>
<groupId>org.springframework.boot</groupId>
<artifactId>spring-boot-starter-parent</artifactId>
<version>3.0.5</version></parent><artifactId>call-routing</artifactId></project>""",
        "src/main/resources/application.yml": "spring:\n  redis:\n    host: localhost\n"
            "  batch:\n    initialize-schema: always\n",
        "src/main/java/Svc.java": "import javax.annotation.PostConstruct;\n"
            "public class Svc { @PostConstruct void init(){} }",
    },
    "lfg/services/rte": {
        "pom.xml": """<project><parent>
<groupId>org.springframework.boot</groupId>
<artifactId>spring-boot-starter-parent</artifactId>
<version>2.7.12</version></parent><artifactId>rte</artifactId></project>""",
        "src/main/java/Clean.java": "import org.springframework.stereotype.Service;\n"
            "@Service public class Clean {}",
    },
}


if __name__ == "__main__":
    main()
