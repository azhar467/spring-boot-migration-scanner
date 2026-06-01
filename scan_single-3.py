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
        rewrite.yml                OpenRewrite recipes per service (with --rewrite-yml)

 OPENREWRITE INTEGRATION: each rule maps to either an OpenRewrite recipe (auto-
    fixable) or a manual reason (no recipe). The report shows an automation split
    ("X% auto-fixable") and the recipe ID inline per finding. --rewrite-yml emits
    a ready-to-run rewrite.yml (one composite recipe per service); the scanner
    never runs OpenRewrite -- it only produces the input a human runs separately.

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


def _attach_remediation(finding, rule):
    """Copy the rule's OpenRewrite mapping onto the finding so the renderer and
    exporters can split automated vs manual without re-reading RULES."""
    finding["openrewrite_recipe"] = rule.get("openrewrite_recipe")
    finding["manual_reason"] = rule.get("manual_reason")
    finding["remediation"] = "automated" if rule.get("openrewrite_recipe") else "manual"
    return finding


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
    return _attach_remediation({
        "rule_id": rule["id"], "rule_name": rule["name"], "type": "file-pattern",
        "severity": severity, "status": status, "total_hits": total,
        "files": sorted(hit_files, key=lambda f: -f["count"]),
        "description": rule.get("description", ""),
    }, rule)


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
    return _attach_remediation({
        "rule_id": rule["id"], "rule_name": rule["name"], "type": "pom-dependency",
        "severity": severity, "status": status if found else "pass",
        "present": bool(found), "detail": detail,
        "description": rule.get("description", ""),
    }, rule)


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


def render_html(scan, ruleset_name, output_path, parent_row=None, meta=None):
    meta = meta or {}
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(scan)
    blockers = sum(1 for r in scan if _repo_status(r["findings"]) == "fail")
    reviews = sum(1 for r in scan if _repo_status(r["findings"]) == "warn")
    clears = total - blockers - reviews
    rows = [_repo_block(r) for r in sorted(
        scan, key=lambda x: SEV_ORDER.get(_worst_sev(x["findings"]), 9))]

    # Automated vs manual split across all FIRED findings in the services
    # (parent excluded from the headline count, like the service tallies).
    fired = [f for r in scan for f in r["findings"] if f["status"] != "pass"]
    auto = sum(1 for f in fired if f.get("openrewrite_recipe"))
    manual = len(fired) - auto
    pct = int(round(100.0 * auto / len(fired))) if fired else 0
    automation = _AUTOMATION.format(auto=auto, manual=manual, pct=pct,
                                    total_fired=len(fired))

    parent_html = ""
    if parent_row:
        parent_html = ("<div class='section-label'>Foundation &mdash; shared parent POM "
                       "(all services inherit from this)</div>\n"
                       + _repo_block(parent_row, is_parent=True))

    intro = _INTRO.format(
        service_count=meta.get("service_count", total),
        ref=html.escape(str(meta.get("ref", "master"))),
        mode=html.escape(str(meta.get("mode", ""))),
        ruleset=html.escape(ruleset_name),
        parent_note=("the shared parent POM plus " if parent_row else ""),
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(_TEMPLATE.format(
            ruleset=html.escape(ruleset_name), generated=generated,
            total=total, blockers=blockers, reviews=reviews, clears=clears,
            intro=intro, legend=_LEGEND, automation=automation, parent=parent_html,
            services_label=("<div class='section-label'>Services (%d)</div>" % total) if parent_row else "",
            rows="\n".join(rows)))
    return output_path


def _action_html(f):
    """Inline remediation hint built from the finding's own recipe/manual data.
    Only shown for fired (non-pass) findings."""
    if f["status"] == "pass":
        return ""
    if f.get("openrewrite_recipe"):
        return ("<div class='action auto'>&rarr; <span class='tag tag-auto'>OPENREWRITE</span> "
                "<code>%s</code></div>" % html.escape(f["openrewrite_recipe"]))
    reason = f.get("manual_reason") or "Manual change required."
    return ("<div class='action manual'>&rarr; <span class='tag tag-manual'>MANUAL</span> %s</div>"
            % html.escape(reason))


def _repo_block(repo, is_parent=False):
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
            "<td>{name}{action}</td><td class='st st-{st}'>{stlabel}</td>"
            "<td class='detail'>{detail}</td></tr>".format(
                st=f["status"], sev=html.escape(f["severity"]),
                name=html.escape(f["rule_name"]), action=_action_html(f),
                stlabel=STATUS_LABEL[f["status"]], detail=detail))
    overall = _PARENT_OVERALL if is_parent else _OVERALL.get(status, "")
    return _REPO_TEMPLATE.format(
        repo=html.escape(repo["repo"]), color=color, label=label,
        cls=" parent" if is_parent else "",
        overall=overall, findings="\n".join(finding_rows))


_OVERALL = {
    "fail": "Has at least one blocker &mdash; cannot upgrade as-is.",
    "warn": "No hard blockers, but items need review/manual work before upgrade.",
    "pass": "No findings against this ruleset. (If still on Boot 2.x, see the parent-version row.)",
}
_PARENT_OVERALL = ("This is the shared parent POM. Its versions are the baseline every "
                   "service inherits; a finding here affects the whole fleet.")


_REPO_TEMPLATE = """
<section class="repo{cls}">
  <header class="repo-head" style="border-left:6px solid {color}">
    <h2>{repo}</h2><span class="badge" style="background:{color}">{label}</span>
  </header>
  <div class="overall">{overall}</div>
  <table class="findings">
    <thead><tr><th>Sev</th><th>Rule &amp; suggested action</th><th>Status</th><th>Detail</th></tr></thead>
    <tbody>
{findings}
    </tbody>
  </table>
</section>
"""

_INTRO = """<div class="intro">
  <p><strong>What this is.</strong> An automated readiness assessment for the
  Spring Boot 2.7 &rarr; 3.x (Java 17) migration, covering {parent_note}{service_count}
  services. It was produced by scanning each repository's source and
  <code>pom.xml</code> on the <code>{ref}</code> branch via {mode}, against the
  versioned <code>{ruleset}</code> ruleset. Deterministic, no AI, nothing cloned.</p>
  <p><strong>How to read it.</strong> Each service is one panel. Its badge is its
  <em>overall</em> status &mdash; the worst of its individual rule results below.
  Each row is one rule: its severity, what it checks (and the suggested action if
  it fired), the pass/review/blocker status, and the supporting detail
  (hit counts, versions, parent-POM divergence).</p>
  <p><strong>Important.</strong> A <em>CLEAR</em> on a single rule means only that
  that one check passed &mdash; not that the service is migrated. A service can be
  CLEAR on jakarta rules yet still be a migration target because it is on Boot 2.x
  (see its <em>Spring Boot parent version</em> row). The OpenSearch / RestHighLevelClient
  finding is the highest-risk item: it is incompatible with Boot 3.x and is NOT
  auto-fixed by OpenRewrite, so it needs a deliberate client decision per service.</p>
</div>"""

_LEGEND = """<div class="legend">
  <div class="leg-group">
    <span class="leg-title">Status</span>
    <span class="chip" style="background:#c0392b">BLOCKER</span> must be resolved before upgrade
    <span class="chip" style="background:#d68910">REVIEW</span> needs manual work / a decision
    <span class="chip" style="background:#1e8449">CLEAR</span> this check passed
  </div>
  <div class="leg-group">
    <span class="leg-title">Severity</span>
    <span class="sev-key sev-critical">critical</span>
    <span class="sev-key sev-high">high</span>
    <span class="sev-key sev-medium">medium</span>
    <span class="sev-key sev-low">low</span>
    <span class="muted">&mdash; critical/high findings drive a BLOCKER; medium/low drive REVIEW</span>
  </div>
</div>"""

_AUTOMATION = """<div class="automation">
  <div class="auto-head">Automation split &mdash; what OpenRewrite can do for you</div>
  <div class="auto-bar"><div class="auto-fill" style="width:{pct}%"></div></div>
  <div class="auto-stats">
    <span><strong>{pct}%</strong> of the {total_fired} findings are auto-fixable</span>
    <span class="tag tag-auto">OPENREWRITE</span> <strong>{auto}</strong> findings &mdash; run the mapped recipes
    <span class="tag tag-manual">MANUAL</span> <strong>{manual}</strong> findings &mdash; engineering time (incl. OpenSearch 1.3)
  </div>
  <div class="auto-note">This tool does not run OpenRewrite. It identifies which findings each
    OpenRewrite recipe would resolve, and which it provably cannot &mdash; so the manual effort is
    visible before the upgrade starts. Recipe IDs are shown inline per finding below.</div>
</div>"""

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
  .intro {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:8px 22px; margin-bottom:22px; }}
  .intro p {{ font-size:.9rem; }}
  .intro strong {{ color:var(--accent); }}
  .intro code {{ background:#0c1117; padding:1px 6px; border-radius:4px;
    font-family:"IBM Plex Mono",monospace; font-size:.82rem; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:22px; background:#141b24;
    border:1px solid var(--line); border-radius:10px; padding:14px 20px;
    margin-bottom:30px; font-size:.8rem; align-items:center; }}
  .leg-group {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  .leg-title {{ color:var(--muted); text-transform:uppercase; letter-spacing:.1em;
    font-size:.68rem; font-weight:700; margin-right:4px; }}
  .chip {{ color:#fff; font-size:.62rem; font-weight:700; letter-spacing:.06em;
    padding:3px 8px; border-radius:20px; }}
  .sev-key {{ font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.05em; }}
  .section-label {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.12em;
    color:var(--muted); font-weight:700; margin:26px 0 12px; }}
  .overall {{ padding:10px 20px; font-size:.82rem; color:var(--ink);
    background:#11181f; border-bottom:1px solid var(--line); }}
  .repo.parent {{ border-color:var(--accent); }}
  .repo.parent .repo-head {{ background:#16202b; }}
  .action {{ font-size:.78rem; margin-top:4px; }}
  .action.auto {{ color:#3ddc84; }}
  .action.manual {{ color:#f0b429; }}
  .action code {{ background:#0c1117; padding:1px 6px; border-radius:4px;
    font-family:"IBM Plex Mono",monospace; font-size:.74rem; color:var(--ink); }}
  .tag {{ font-size:.6rem; font-weight:700; letter-spacing:.06em; padding:2px 6px;
    border-radius:4px; color:#0b0e12; }}
  .tag-auto {{ background:#3ddc84; }} .tag-manual {{ background:#f0b429; }}
  .automation {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:18px 22px; margin-bottom:30px; }}
  .auto-head {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.1em;
    color:var(--accent); font-weight:700; margin-bottom:12px; }}
  .auto-bar {{ height:10px; background:#0c1117; border-radius:6px; overflow:hidden;
    border:1px solid var(--line); }}
  .auto-fill {{ height:100%; background:linear-gradient(90deg,#3ddc84,#2bb673); }}
  .auto-stats {{ display:flex; flex-wrap:wrap; gap:18px; align-items:center;
    margin-top:12px; font-size:.84rem; }}
  .auto-note {{ color:var(--muted); font-size:.78rem; margin-top:10px; }}
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
  {intro}
  {legend}
  {automation}
  {parent}
  {services_label}
  {rows}
  <div class="foot">Rule-driven scan. Engine fixed; checks defined as versioned rules.
    Re-run after migration to verify remediation. &mdash; deterministic, no AI, runs in-VDI.
    Summary cards count the {total} services (the parent POM is shown separately as
    the inherited baseline).</div>
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
    cols = ["repo", "rule_id", "rule_name", "type", "severity", "status",
            "remediation", "openrewrite_recipe", "metric", "detail"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for repo in scan:
            for fnd in repo["findings"]:
                metric, detail = _flatten(fnd)
                w.writerow({"repo": repo["repo"], "rule_id": fnd["rule_id"],
                            "rule_name": fnd["rule_name"], "type": fnd["type"],
                            "severity": fnd["severity"], "status": fnd["status"],
                            "remediation": fnd.get("remediation", ""),
                            "openrewrite_recipe": fnd.get("openrewrite_recipe") or "",
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


def export_rewrite_yml(scan, output_path):
    """Emit a ready-to-run OpenRewrite rewrite.yml: one declarative recipe per
    service listing the recipes its fired findings imply. This is INPUT for a
    human to run with the rewrite-maven-plugin -- the scanner never executes it.
    Findings with no recipe (manual) are listed as comments so they aren't lost.
    """
    lines = [
        "# Generated by the Migration Readiness Scanner -- INPUT for OpenRewrite.",
        "# The scanner does NOT run these; a human runs the rewrite-maven-plugin.",
        "# One composite recipe per service. Manual items are noted as comments.",
        "#",
        "# Run per service, e.g.:",
        "#   mvn -U org.openrewrite.maven:rewrite-maven-plugin:run \\",
        "#     -Drewrite.activeRecipes=com.lfg.migration.<service> \\",
        "#     -Drewrite.recipeArtifactCoordinates=org.openrewrite.recipe:rewrite-spring:RELEASE,org.openrewrite.recipe:rewrite-migrate-java:RELEASE",
        "",
    ]
    for repo in scan:
        if repo.get("is_parent"):
            continue
        fired = [f for f in repo["findings"] if f["status"] != "pass"]
        recipes, manual = [], []
        for f in fired:
            if f.get("openrewrite_recipe"):
                if f["openrewrite_recipe"] not in recipes:
                    recipes.append(f["openrewrite_recipe"])
            else:
                manual.append("%s (%s)" % (f["rule_name"], f.get("manual_reason", "manual")))
        safe = re.sub(r"[^A-Za-z0-9]+", "-", str(repo["repo"])).strip("-")
        lines.append("---")
        lines.append("type: specs.openrewrite.org/v1beta/recipe")
        lines.append("name: com.lfg.migration.%s" % safe)
        lines.append("displayName: Boot 2.7->3.x migration for %s" % repo["repo"])
        lines.append("recipeList:")
        if recipes:
            for r in recipes:
                lines.append("  - %s" % r)
        else:
            lines.append("  []  # no auto-fixable findings")
        if manual:
            lines.append("# MANUAL (no recipe) -- handle by hand:")
            for m in manual:
                lines.append("#   - %s" % m)
        lines.append("")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
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

def run_scan(cfg, token, dry_run, csv_flag, json_flag, rewrite_flag, base_dir):
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
    parent_row = None
    parent_ref = cfg.get("default_ref", "master")
    parent_label = None
    if cfg.get("parent_pom_project"):
        parent_source = make_source(cfg["parent_pom_project"], parent_ref)
        parent_pom_view = parent_source.pom()
        # Also scan the parent itself so it appears as a visible row. Divergence
        # against itself is a no-op (it IS the reference), which is correct.
        parent_label = "parent-pom (%s)" % cfg["parent_pom_project"]
        pfindings = []
        for rule in RULES:
            if rule["type"] == "file-pattern":
                pfindings.append(run_file_pattern(rule, parent_source.files_for_glob))
            elif rule["type"] == "pom-dependency":
                pfindings.append(run_pom_dependency(rule, parent_pom_view, parent_pom_view))
        parent_row = {"repo": parent_label, "findings": pfindings, "is_parent": True}
        print("scanned: %s [parent]" % parent_label, file=sys.stderr)

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

    meta = {
        "ref": parent_ref,
        "service_count": len(scan),
        "parent_label": parent_label,
        "gitlab_url": cfg.get("gitlab_url", ""),
        "mode": "dry-run (bundled samples)" if dry_run else "GitLab REST API (no clone)",
    }

    out = os.path.join(base_dir, cfg.get("output", "readiness-report.html"))
    render_html(scan, ruleset_name, out, parent_row=parent_row, meta=meta)
    print("\nReport written to: %s" % out)

    # Exports include the parent row too (full picture for spreadsheets/diffing).
    export_scan = ([parent_row] if parent_row else []) + scan
    csv_target = csv_flag or cfg.get("export_csv")
    if csv_target:
        p = os.path.join(base_dir, csv_target if isinstance(csv_target, str) else "readiness-findings.csv")
        export_csv(export_scan, p)
        print("CSV written to:    %s" % p)
    json_target = json_flag or cfg.get("export_json")
    if json_target:
        p = os.path.join(base_dir, json_target if isinstance(json_target, str) else "readiness-findings.json")
        export_json(export_scan, ruleset_name, p)
        print("JSON written to:   %s" % p)
    rewrite_target = rewrite_flag or cfg.get("export_rewrite_yml")
    if rewrite_target:
        p = os.path.join(base_dir, rewrite_target if isinstance(rewrite_target, str) else "rewrite.yml")
        export_rewrite_yml(scan, p)
        print("rewrite.yml written to: %s" % p)


def main():
    ap = argparse.ArgumentParser(description="Migration readiness scanner (single-file)")
    ap.add_argument("--config", default=None, help="optional YAML/JSON config; else uses CONFIG dict")
    ap.add_argument("--token", default=None, help="GitLab PAT; overrides env and config")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--csv", nargs="?", const=True, default=None)
    ap.add_argument("--json", nargs="?", const=True, default=None)
    ap.add_argument("--rewrite-yml", nargs="?", const=True, default=None,
                    help="also emit an OpenRewrite rewrite.yml (input for the rewrite plugin)")
    args = ap.parse_args()

    if args.config:
        cfg = load_config_file(args.config)
        base_dir = os.path.dirname(os.path.abspath(args.config))
    else:
        cfg = CONFIG
        base_dir = os.getcwd()

    # Token precedence: --token  >  env GITLAB_TOKEN  >  CONFIG["token"].
    token = args.token or os.environ.get("GITLAB_TOKEN") or cfg.get("token") or ""
    run_scan(cfg, token, args.dry_run, args.csv, args.json, args.rewrite_yml, base_dir)


# ===========================================================================
# RULES -- the ruleset, as data. Add a dict to add a check; no engine change.
# Mirrors rulesets/spring-boot-3/*.yml from the multi-file version.
# ===========================================================================

RULES = [
    # Each rule carries either:
    #   "openrewrite_recipe": "<recipe id>"  -> OpenRewrite can auto-fix this
    #   "manual_reason": "<why>"             -> no recipe; needs human work
    # Recipe IDs verified against docs.openrewrite.org (rewrite-migrate-java /
    # rewrite-spring). The Boot 3.0 umbrella recipe
    # org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_0 chains the version
    # bump, deprecated-property migrations (redis, batch) and framework changes;
    # the individual jakarta recipes below are the targeted equivalents.

    # --- OpenSearch 1.3 / Elasticsearch client (highest value, all MANUAL) ---
    {"id": "opensearch-rhlc-usage", "type": "file-pattern",
     "name": "RestHighLevelClient usage (incompatible with Boot 3.x client)",
     "severity": "critical", "file_glob": "**/*.java",
     "pattern": r"RestHighLevelClient|org\.elasticsearch\.client\.RequestOptions",
     "description": "RHLC is removed/incompatible in the Boot 3.x Elasticsearch stack; "
                    "breaks against OpenSearch 1.3.",
     "manual_reason": "No OpenRewrite recipe covers the OpenSearch 1.3 client "
                      "incompatibility; client migration is a manual design decision."},
    {"id": "spring-data-es-dep", "type": "pom-dependency",
     "name": "spring-boot-starter-data-elasticsearch present", "severity": "high",
     "artifact_id": "spring-boot-starter-data-elasticsearch", "check_parent_divergence": True,
     "description": "Under Boot 3.x the client is incompatible with OpenSearch 1.3; "
                    "needs an explicit client decision.",
     "manual_reason": "Dependency stays, but the client behind it must be re-validated "
                      "against OpenSearch 1.3 by hand."},
    {"id": "legacy-es-rest-dep", "type": "pom-dependency",
     "name": "elasticsearch-rest-high-level-client dependency", "severity": "critical",
     "artifact_id": "elasticsearch-rest-high-level-client",
     "description": "Legacy high-level REST client; must be replaced before Boot 3.x.",
     "manual_reason": "No recipe; replacing the legacy REST client is manual."},

    # --- javax -> jakarta (all AUTOMATED) ---
    {"id": "javax-persistence", "type": "file-pattern",
     "name": "javax.persistence imports (-> jakarta.persistence)", "severity": "high",
     "file_glob": "**/*.java", "pattern": r"javax\.persistence\.",
     "description": "JPA annotations moved to jakarta.persistence.",
     "openrewrite_recipe": "org.openrewrite.java.migrate.jakarta.JavaxPersistenceToJakartaPersistence"},
    {"id": "javax-servlet", "type": "file-pattern",
     "name": "javax.servlet imports (-> jakarta.servlet)", "severity": "high",
     "file_glob": "**/*.java", "pattern": r"javax\.servlet\.",
     "description": "Servlet API moved to jakarta.servlet.",
     "openrewrite_recipe": "org.openrewrite.java.migrate.jakarta.JavaxServletToJakartaServlet"},
    {"id": "javax-validation", "type": "file-pattern",
     "name": "javax.validation imports (-> jakarta.validation)", "severity": "medium",
     "file_glob": "**/*.java", "pattern": r"javax\.validation\.",
     "description": "Bean Validation moved to jakarta.validation.",
     "openrewrite_recipe": "org.openrewrite.java.migrate.jakarta.JavaxValidationMigrationToJakartaValidation"},
    {"id": "javax-annotation", "type": "file-pattern",
     "name": "javax.annotation imports (-> jakarta.annotation)", "severity": "medium",
     "file_glob": "**/*.java", "pattern": r"javax\.annotation\.(PostConstruct|PreDestroy|Resource)",
     "description": "Common annotations moved to jakarta.annotation.",
     "openrewrite_recipe": "org.openrewrite.java.migrate.jakarta.JavaxAnnotationMigrationToJakartaAnnotation"},

    # --- config keys + parent POM ---
    {"id": "parent-boot-version", "type": "pom-dependency",
     "name": "Spring Boot parent version", "severity": "high",
     "artifact_id": "spring-boot-starter-parent", "bad_version_regex": r"^2\.",
     "check_parent_divergence": True,
     "description": "Reports inherited Boot version; flags 2.x and children diverging from parent.",
     "openrewrite_recipe": "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_0"},
    {"id": "redis-deprecated-keys", "type": "file-pattern",
     "name": "Deprecated spring.redis.* keys (-> spring.data.redis.*)", "severity": "medium",
     "file_glob": "**/*.yml", "pattern": r"spring\.redis\.|spring:\s*\n\s+redis:",
     "description": "Redis properties moved under spring.data.redis.",
     "openrewrite_recipe": "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_0"},
    {"id": "deprecated-security-adapter", "type": "file-pattern",
     "name": "WebSecurityConfigurerAdapter (removed in Spring Security 6)", "severity": "high",
     "file_glob": "**/*.java", "pattern": r"WebSecurityConfigurerAdapter",
     "description": "Removed in Spring Security 6; migrate to SecurityFilterChain.",
     "manual_reason": "OpenRewrite's security recipe is partial; SecurityFilterChain "
                      "rewrites usually need manual review/verification."},
    {"id": "deprecated-batch-properties", "type": "file-pattern",
     "name": "spring.batch.initialize-schema (renamed)", "severity": "low",
     "file_glob": "**/*.yml", "pattern": r"spring\.batch\.initialize-schema",
     "description": "Renamed to spring.batch.jdbc.initialize-schema.",
     "openrewrite_recipe": "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_0"},
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
