from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


GENERATOR = "tracepress-qa/0.1.0"
GENERATED_FILES = (
    "qa-report.json",
    "claim-traceability.csv",
    "source-register.csv",
    "sample-publication.html",
    "release-manifest.json",
    "CHECKSUMS.sha256",
)


class BuildFailed(RuntimeError):
    """Raised when a blocking quality gate prevents release creation."""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        count = report.get("summary", {}).get("errors", 0)
        super().__init__(f"Build blocked by {count} quality-gate error(s)")


@dataclass(frozen=True)
class Inspection:
    report: dict[str, Any]
    manuscript: str
    sources: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    release: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(_json_bytes(value))


def _check(
    checks: list[dict[str, Any]],
    code: str,
    passed: bool,
    message: str,
    *,
    severity: str = "error",
    context: dict[str, Any] | None = None,
) -> None:
    status = "pass" if passed else ("warn" if severity == "warning" else "fail")
    item: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "status": status,
        "message": message,
    }
    if context:
        item["context"] = context
    checks.append(item)


def _load_json(path: Path, checks: list[dict[str, Any]], code: str) -> dict[str, Any]:
    if not path.is_file():
        _check(checks, code, False, f"Required file is missing: {path.name}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _check(checks, code, False, f"Could not parse {path.name}: {exc}")
        return {}
    valid = isinstance(data, dict)
    _check(checks, code, valid, f"{path.name} is valid JSON object data.")
    return data if valid else {}


def _contained_path(base: Path, relative: str) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        return None
    candidate = (base / relative).resolve()
    root = base.resolve()
    return candidate if candidate == root or root in candidate.parents else None


def _required_strings(record: dict[str, Any], fields: Iterable[str]) -> list[str]:
    return [field for field in fields if not isinstance(record.get(field), str) or not record[field].strip()]


def _verify_claim_rule(
    claim: dict[str, Any], source_paths: dict[str, Path]
) -> tuple[bool, str]:
    rule = claim.get("verification")
    if rule is None:
        return True, "Source-linked editorial claim; no executable rule declared."
    if not isinstance(rule, dict):
        return False, "Verification must be an object."
    rule_type = rule.get("type")
    source_id = rule.get("source_id")
    path = source_paths.get(source_id)
    if path is None:
        return False, f"Verification source {source_id!r} is unavailable."
    if path.suffix.lower() != ".csv":
        return False, "Executable sample rules require a CSV source."
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        return False, f"Could not read verification CSV: {exc}"

    if rule_type == "csv_row_count":
        expected = rule.get("expected")
        actual = len(rows)
        return actual == expected, f"Expected {expected} data row(s); found {actual}."

    column = rule.get("column")
    if not isinstance(column, str) or (rows and column not in rows[0]):
        return False, f"CSV column {column!r} is missing."
    if rule_type == "csv_nonempty_column":
        missing = sum(not (row.get(column) or "").strip() for row in rows)
        return missing == 0, f"Column {column!r} has {missing} empty value(s)."
    if rule_type == "csv_value_count":
        value = str(rule.get("value", ""))
        expected = rule.get("expected")
        actual = sum((row.get(column) or "") == value for row in rows)
        return actual == expected, f"Expected {expected} row(s) where {column}={value!r}; found {actual}."
    return False, f"Unsupported verification rule: {rule_type!r}."


def _inspect(workspace: str | Path) -> Inspection:
    workspace_path = Path(workspace).resolve()
    input_dir = workspace_path / "input"
    checks: list[dict[str, Any]] = []

    _check(checks, "WORKSPACE.INPUT_DIR", input_dir.is_dir(), "Input directory exists.")
    sources_doc = _load_json(input_dir / "sources.json", checks, "SCHEMA.SOURCES_JSON")
    claims_doc = _load_json(input_dir / "claims.json", checks, "SCHEMA.CLAIMS_JSON")
    links_doc = _load_json(input_dir / "links.json", checks, "SCHEMA.LINKS_JSON")
    release = _load_json(input_dir / "release.json", checks, "SCHEMA.RELEASE_JSON")

    manuscript_path = input_dir / "manuscript.md"
    if manuscript_path.is_file():
        try:
            manuscript = manuscript_path.read_text(encoding="utf-8")
            _check(checks, "MANUSCRIPT.READABLE", True, "Manuscript is readable UTF-8 text.")
        except (OSError, UnicodeError) as exc:
            manuscript = ""
            _check(checks, "MANUSCRIPT.READABLE", False, f"Could not read manuscript: {exc}")
    else:
        manuscript = ""
        _check(checks, "MANUSCRIPT.READABLE", False, "Required manuscript.md is missing.")

    source_records = sources_doc.get("records", [])
    valid_source_container = sources_doc.get("schema_version") == "1.0" and isinstance(source_records, list)
    _check(
        checks,
        "SCHEMA.SOURCES_V1",
        valid_source_container,
        "Source register uses schema version 1.0 and contains a records array.",
    )
    if not isinstance(source_records, list):
        source_records = []

    normalized_sources: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    source_paths: dict[str, Path] = {}
    required_source_fields = (
        "id",
        "title",
        "kind",
        "origin",
        "rights",
        "confidentiality",
        "status",
        "path",
    )
    for index, raw in enumerate(source_records):
        if not isinstance(raw, dict):
            _check(checks, "SOURCE.RECORD", False, f"Source record {index + 1} must be an object.")
            continue
        missing = _required_strings(raw, required_source_fields)
        source_id = str(raw.get("id", f"record-{index + 1}"))
        _check(
            checks,
            "SOURCE.REQUIRED_FIELDS",
            not missing,
            f"{source_id} includes all required fields." if not missing else f"{source_id} is missing: {', '.join(missing)}.",
            context={"source_id": source_id},
        )
        id_valid = bool(re.fullmatch(r"SRC-\d{3}", source_id))
        _check(checks, "SOURCE.ID_FORMAT", id_valid, f"{source_id} uses the SRC-000 identifier pattern.")
        unique = source_id not in source_ids
        _check(checks, "SOURCE.ID_UNIQUE", unique, f"{source_id} is unique.")
        source_ids.add(source_id)

        path = _contained_path(input_dir, str(raw.get("path", "")))
        contained = path is not None
        _check(checks, "SOURCE.PATH_CONTAINED", contained, f"{source_id} path stays inside the input directory.")
        exists = bool(path and path.is_file())
        _check(checks, "SOURCE.FILE_EXISTS", exists, f"{source_id} resolves to a local evidence file.")
        public_demo = raw.get("confidentiality") == "public-demo" and raw.get("rights") in {
            "synthetic",
            "public-domain",
        }
        _check(
            checks,
            "SOURCE.PUBLIC_HANDLING",
            public_demo,
            f"{source_id} is classified for public demonstration use.",
        )
        item = dict(raw)
        if exists and path:
            item["bytes"] = path.stat().st_size
            item["computed_sha256"] = _sha256(path)
            source_paths[source_id] = path
        else:
            item["bytes"] = None
            item["computed_sha256"] = ""
        normalized_sources.append(item)

    claim_records = claims_doc.get("claims", [])
    valid_claim_container = claims_doc.get("schema_version") == "1.0" and isinstance(claim_records, list)
    _check(
        checks,
        "SCHEMA.CLAIMS_V1",
        valid_claim_container,
        "Claim register uses schema version 1.0 and contains a claims array.",
    )
    if not isinstance(claim_records, list):
        claim_records = []

    normalized_claims: list[dict[str, Any]] = []
    claim_ids: set[str] = set()
    for index, raw in enumerate(claim_records):
        if not isinstance(raw, dict):
            _check(checks, "CLAIM.RECORD", False, f"Claim record {index + 1} must be an object.")
            continue
        missing = _required_strings(raw, ("id", "text", "status"))
        claim_id = str(raw.get("id", f"claim-{index + 1}"))
        evidence = raw.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            missing.append("evidence")
        _check(
            checks,
            "CLAIM.REQUIRED_FIELDS",
            not missing,
            f"{claim_id} includes all required fields." if not missing else f"{claim_id} is missing: {', '.join(missing)}.",
            context={"claim_id": claim_id},
        )
        _check(
            checks,
            "CLAIM.ID_FORMAT",
            bool(re.fullmatch(r"C-\d{3}", claim_id)),
            f"{claim_id} uses the C-000 identifier pattern.",
        )
        unique = claim_id not in claim_ids
        _check(checks, "CLAIM.ID_UNIQUE", unique, f"{claim_id} is unique.")
        claim_ids.add(claim_id)
        allowed_status = raw.get("status") in {"verified-fixture", "editorial-context"}
        _check(checks, "CLAIM.STATUS", allowed_status, f"{claim_id} has an allowed disclosure status.")

        evidence_items = evidence if isinstance(evidence, list) else []
        evidence_ok = True
        for link in evidence_items:
            if not isinstance(link, dict) or not isinstance(link.get("locator"), str):
                evidence_ok = False
                continue
            if link.get("source_id") not in source_ids:
                evidence_ok = False
        _check(checks, "CLAIM.EVIDENCE", evidence_ok and bool(evidence_items), f"{claim_id} resolves to registered source evidence.")
        verified, result = _verify_claim_rule(raw, source_paths)
        _check(checks, "CLAIM.REPRODUCIBLE", verified, f"{claim_id}: {result}")
        item = dict(raw)
        item["verification_passed"] = verified
        item["verification_result"] = result
        normalized_claims.append(item)

    markers = re.findall(r"\[claim:(C-\d{3})\]", manuscript)
    unknown_markers = sorted(set(markers) - claim_ids)
    unused_claims = sorted(claim_ids - set(markers))
    _check(
        checks,
        "MANUSCRIPT.CLAIM_MARKERS_KNOWN",
        not unknown_markers,
        "Every manuscript claim marker resolves to the claim register."
        if not unknown_markers
        else f"Unknown claim marker(s): {', '.join(unknown_markers)}.",
    )
    _check(
        checks,
        "MANUSCRIPT.CLAIMS_USED",
        not unused_claims,
        "Every registered claim appears in the manuscript."
        if not unused_claims
        else f"Unused claim record(s): {', '.join(unused_claims)}.",
    )
    _check(checks, "MANUSCRIPT.CLAIMS_PRESENT", bool(markers), "Manuscript includes traceable claim markers.")

    h1_count = len(re.findall(r"(?m)^# (?!#).+$", manuscript))
    h2_titles = [title.strip().lower() for title in re.findall(r"(?m)^## (.+)$", manuscript)]
    _check(checks, "MANUSCRIPT.SINGLE_H1", h1_count == 1, f"Manuscript contains {h1_count} level-one heading(s); exactly one is required.")
    required_sections = {"demonstration status", "method", "findings", "limits and disclosure"}
    missing_sections = sorted(required_sections - set(h2_titles))
    _check(
        checks,
        "MANUSCRIPT.REQUIRED_SECTIONS",
        not missing_sections,
        "All required status, method, findings, and disclosure sections are present."
        if not missing_sections
        else f"Missing section(s): {', '.join(missing_sections)}.",
    )
    unresolved = sorted(set(re.findall(r"(?i)\b(?:TODO|FIXME|TKTK|TK)\b", manuscript)))
    _check(
        checks,
        "MANUSCRIPT.NO_EDITORIAL_TOKENS",
        not unresolved,
        "No unresolved editorial tokens remain."
        if not unresolved
        else f"Unresolved editorial token(s): {', '.join(unresolved)}.",
    )
    disclosure_ok = "ai-assisted" in manuscript.lower() and "synthetic" in manuscript.lower()
    _check(
        checks,
        "MANUSCRIPT.DISCLOSURE",
        disclosure_ok,
        "Manuscript explicitly discloses AI assistance and synthetic data.",
    )

    fixtures_raw = links_doc.get("fixtures", [])
    valid_links_container = links_doc.get("schema_version") == "1.0" and isinstance(fixtures_raw, list)
    _check(checks, "SCHEMA.LINKS_V1", valid_links_container, "Link registry uses schema version 1.0 and contains a fixtures array.")
    fixtures: dict[str, dict[str, Any]] = {}
    if isinstance(fixtures_raw, list):
        for fixture in fixtures_raw:
            if isinstance(fixture, dict) and isinstance(fixture.get("url"), str):
                fixtures[fixture["url"]] = fixture
    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", manuscript)
    for target in links:
        parsed = urlparse(target)
        if parsed.scheme in {"https", "fixture"}:
            fixture = fixtures.get(target)
            registered = fixture is not None
            _check(checks, "LINK.OFFLINE_REGISTERED", registered, f"{target} has a declared offline fixture.", context={"url": target})
            status = fixture.get("status") if fixture else None
            healthy = isinstance(status, int) and 200 <= status < 400
            _check(checks, "LINK.STATUS", healthy, f"{target} fixture has a successful status ({status}).", context={"url": target})
        elif parsed.scheme == "mailto":
            _check(checks, "LINK.MAILTO", bool(parsed.path), f"{target} contains a mail address.")
        elif parsed.scheme:
            _check(checks, "LINK.SCHEME", False, f"Unsupported or insecure link scheme in {target}.", context={"url": target})
        elif target.startswith("#"):
            _check(checks, "LINK.FRAGMENT", True, f"{target} is an internal fragment link.")
        else:
            local = _contained_path(input_dir, target)
            _check(checks, "LINK.LOCAL_CONTAINED", local is not None, f"Local link {target} stays inside the input directory.")
            _check(checks, "LINK.LOCAL_EXISTS", bool(local and local.is_file()), f"Local link {target} resolves to a file.")

    release_missing = _required_strings(
        release,
        ("release_id", "version", "release_date", "status", "classification", "ai_disclosure", "confidentiality_disclosure"),
    )
    _check(
        checks,
        "RELEASE.REQUIRED_FIELDS",
        not release_missing,
        "Release policy includes all required fields."
        if not release_missing
        else f"Release policy is missing: {', '.join(release_missing)}.",
    )
    _check(
        checks,
        "RELEASE.PUBLIC_SYNTHETIC",
        release.get("status") == "demonstration" and release.get("classification") == "public-synthetic",
        "Release is explicitly classified as a public synthetic demonstration.",
    )
    _check(
        checks,
        "RELEASE.DATE_FORMAT",
        bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(release.get("release_date", "")))),
        "Release date uses ISO YYYY-MM-DD format.",
    )

    errors = sum(item["status"] == "fail" for item in checks)
    warnings = sum(item["status"] == "warn" for item in checks)
    passed = sum(item["status"] == "pass" for item in checks)
    report = {
        "schema_version": "1.0",
        "generator": GENERATOR,
        "release_id": release.get("release_id", "unknown"),
        "summary": {
            "status": "pass" if errors == 0 else "fail",
            "total": len(checks),
            "passed": passed,
            "warnings": warnings,
            "errors": errors,
        },
        "checks": checks,
    }
    return Inspection(report, manuscript, normalized_sources, normalized_claims, release)


def check_workspace(workspace: str | Path) -> dict[str, Any]:
    """Run quality gates without writing release artifacts."""

    return _inspect(workspace).report


def _csv_text(fieldnames: list[str], rows: list[dict[str, Any]]) -> str:
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "section"


def _inline(value: str) -> str:
    token = re.compile(r"\[claim:(C-\d{3})\]|\[([^\]]+)\]\(([^)]+)\)")
    parts: list[str] = []
    cursor = 0
    for match in token.finditer(value):
        parts.append(html.escape(value[cursor : match.start()]))
        if match.group(1):
            claim_id = match.group(1)
            parts.append(
                f'<sup class="claim-ref"><a href="#trace-{claim_id}" '
                f'aria-label="View evidence for claim {claim_id}">{claim_id}</a></sup>'
            )
        else:
            label = html.escape(match.group(2))
            target = html.escape(match.group(3), quote=True)
            parts.append(f'<a href="{target}">{label}</a>')
        cursor = match.end()
    parts.append(html.escape(value[cursor:]))
    return "".join(parts)


def _render_blocks(manuscript: str) -> tuple[str, str, list[tuple[str, str]]]:
    lines = manuscript.splitlines()
    output: list[str] = []
    title = "Untitled publication"
    navigation: list[tuple[str, str]] = []
    paragraph: list[str] = []
    list_items: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            output.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    for line in lines + [""]:
        stripped = line.strip()
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            text = heading.group(2).strip()
            anchor = _slug(text)
            if level == 1:
                title = text
            if level == 2:
                navigation.append((anchor, text))
            output.append(f'<h{level} id="{anchor}">{_inline(text)}</h{level}>')
        elif stripped.startswith("- "):
            flush_paragraph()
            list_items.append(stripped[2:].strip())
        elif stripped.startswith("> "):
            flush_paragraph()
            flush_list()
            output.append(f"<blockquote><p>{_inline(stripped[2:].strip())}</p></blockquote>")
        elif not stripped:
            flush_paragraph()
            flush_list()
        else:
            flush_list()
            paragraph.append(stripped)
    return title, "\n".join(output), navigation


def _render_html(inspection: Inspection) -> str:
    title, body, navigation = _render_blocks(inspection.manuscript)
    nav = "".join(f'<a href="#{anchor}">{html.escape(label)}</a>' for anchor, label in navigation)
    source_by_id = {source["id"]: source for source in inspection.sources}
    trace_rows: list[str] = []
    for claim in inspection.claims:
        evidence = claim.get("evidence", [])
        source_labels = []
        locators = []
        for link in evidence:
            source_id = str(link.get("source_id", ""))
            source = source_by_id.get(source_id, {})
            source_labels.append(f"{source_id}: {source.get('title', 'Unknown source')}")
            locators.append(str(link.get("locator", "")))
        result = "Passed" if claim.get("verification_passed") else "Failed"
        trace_rows.append(
            f'<tr id="trace-{html.escape(claim["id"])}"><th scope="row">{html.escape(claim["id"])}</th>'
            f'<td>{html.escape(claim["text"])}</td><td>{html.escape("; ".join(source_labels))}</td>'
            f'<td>{html.escape("; ".join(locators))}</td><td>{result}</td></tr>'
        )
    release = inspection.release
    description = "A synthetic, traceable publishing QA demonstration with offline evidence checks."
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{html.escape(description, quote=True)}">
  <title>{html.escape(title)} — Tracepress QA demonstration</title>
  <style>
    :root {{ color-scheme: light; --ink:#17211b; --muted:#53645a; --paper:#fbfaf5; --panel:#eef2e9; --line:#b9c4ba; --accent:#075f4b; --focus:#b54708; }}
    * {{ box-sizing:border-box; }}
    html {{ scroll-behavior:smooth; }}
    body {{ margin:0; background:var(--paper); color:var(--ink); font:1rem/1.65 system-ui,-apple-system,"Segoe UI",sans-serif; }}
    a {{ color:var(--accent); text-underline-offset:.18em; }}
    a:focus-visible {{ outline:3px solid var(--focus); outline-offset:4px; border-radius:2px; }}
    .skip {{ position:absolute; left:.75rem; top:-5rem; background:var(--ink); color:white; padding:.65rem 1rem; z-index:3; }}
    .skip:focus {{ top:.75rem; }}
    header, main, footer {{ width:min(72rem, calc(100% - 2rem)); margin-inline:auto; }}
    header {{ padding:2.5rem 0 1.25rem; border-bottom:1px solid var(--line); }}
    .eyebrow {{ margin:0 0 .4rem; color:var(--accent); font-size:.78rem; font-weight:750; letter-spacing:.12em; text-transform:uppercase; }}
    nav {{ display:flex; flex-wrap:wrap; gap:.5rem 1rem; margin-top:1rem; }}
    main {{ display:grid; grid-template-columns:minmax(0, 1fr) minmax(15rem, .34fr); gap:clamp(2rem,5vw,5rem); padding-block:3rem; }}
    article {{ min-width:0; }}
    article > h1 {{ font:700 clamp(2.45rem,7vw,5.4rem)/.98 Georgia,serif; letter-spacing:-.045em; max-width:12ch; margin:.25rem 0 2.5rem; }}
    h2 {{ font:700 clamp(1.5rem,3vw,2.2rem)/1.15 Georgia,serif; margin:3rem 0 .8rem; }}
    h3 {{ margin-top:2rem; }}
    p, li {{ max-width:67ch; }}
    blockquote {{ margin:0 0 2.25rem; padding:.2rem 0 .2rem 1.2rem; border-left:4px solid var(--accent); color:var(--muted); font-size:1.1rem; }}
    .claim-ref {{ margin-left:.2rem; font-size:.68em; font-weight:750; }}
    aside {{ align-self:start; position:sticky; top:1rem; background:var(--panel); border:1px solid var(--line); border-radius:.35rem; padding:1.25rem; }}
    aside h2 {{ margin:0 0 .7rem; font:700 1.05rem/1.3 system-ui,sans-serif; }}
    aside p {{ margin:.45rem 0; font-size:.9rem; }}
    .trace {{ grid-column:1 / -1; border-top:1px solid var(--line); padding-top:2rem; }}
    .table-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:.35rem; }}
    table {{ border-collapse:collapse; width:100%; min-width:52rem; background:white; }}
    caption {{ text-align:left; font-weight:700; padding:1rem; background:var(--panel); }}
    th, td {{ padding:.8rem; border-top:1px solid var(--line); text-align:left; vertical-align:top; }}
    thead th {{ border-top:0; background:#f5f6f1; }}
    footer {{ border-top:1px solid var(--line); padding:1.5rem 0 3rem; color:var(--muted); font-size:.9rem; }}
    @media (max-width: 48rem) {{ main {{ grid-template-columns:1fr; padding-block:2rem; }} aside {{ position:static; }} .trace {{ grid-column:1; }} }}
    @media (prefers-reduced-motion: reduce) {{ html {{ scroll-behavior:auto; }} }}
  </style>
</head>
<body>
  <a class="skip" href="#content">Skip to content</a>
  <header>
    <p class="eyebrow">Tracepress QA · synthetic portfolio demonstration</p>
    <nav aria-label="Publication sections">{nav}</nav>
  </header>
  <main id="content">
    <article aria-labelledby="{_slug(title)}">{body}</article>
    <aside aria-labelledby="release-note-title">
      <h2 id="release-note-title">Release note</h2>
      <p><strong>Status:</strong> {html.escape(str(release.get('status', 'unknown')).title())}</p>
      <p><strong>Classification:</strong> {html.escape(str(release.get('classification', 'unknown')))}</p>
      <p><strong>Release:</strong> {html.escape(str(release.get('version', 'unknown')))} · {html.escape(str(release.get('release_date', 'unknown')))}</p>
      <p>{html.escape(str(release.get('confidentiality_disclosure', '')))}</p>
    </aside>
    <section class="trace" aria-labelledby="traceability-title">
      <h2 id="traceability-title">Claim traceability</h2>
      <p>Each public claim links to local synthetic evidence. “Passed” means the declared fixture rule reproduced the expected result.</p>
      <div class="table-wrap" tabindex="0" aria-label="Scrollable claim traceability table">
        <table>
          <caption>Claim-to-source register</caption>
          <thead><tr><th scope="col">ID</th><th scope="col">Claim</th><th scope="col">Source</th><th scope="col">Locator</th><th scope="col">Check</th></tr></thead>
          <tbody>{''.join(trace_rows)}</tbody>
        </table>
      </div>
    </section>
  </main>
  <footer>
    <p>{html.escape(str(release.get('ai_disclosure', '')))}</p>
    <p>Generated by {GENERATOR}. Offline fixtures only; no live URLs were requested.</p>
  </footer>
</body>
</html>
"""


def build_release(workspace: str | Path, output: str | Path) -> dict[str, Any]:
    """Validate inputs and create deterministic release artifacts."""

    inspection = _inspect(workspace)
    if inspection.report["summary"]["status"] != "pass":
        raise BuildFailed(inspection.report)

    output_dir = Path(output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in GENERATED_FILES:
        path = output_dir / name
        if path.is_file():
            path.unlink()

    _write_json(output_dir / "qa-report.json", inspection.report)

    source_rows = [
        {
            "id": source.get("id", ""),
            "title": source.get("title", ""),
            "kind": source.get("kind", ""),
            "origin": source.get("origin", ""),
            "rights": source.get("rights", ""),
            "confidentiality": source.get("confidentiality", ""),
            "status": source.get("status", ""),
            "path": source.get("path", ""),
            "bytes": source.get("bytes", ""),
            "sha256": source.get("computed_sha256", ""),
        }
        for source in inspection.sources
    ]
    source_fields = ["id", "title", "kind", "origin", "rights", "confidentiality", "status", "path", "bytes", "sha256"]
    (output_dir / "source-register.csv").write_text(_csv_text(source_fields, source_rows), encoding="utf-8", newline="")

    claim_rows: list[dict[str, Any]] = []
    for claim in inspection.claims:
        evidence = claim.get("evidence", [])
        claim_rows.append(
            {
                "id": claim.get("id", ""),
                "claim": claim.get("text", ""),
                "status": claim.get("status", ""),
                "source_ids": ";".join(str(item.get("source_id", "")) for item in evidence),
                "locators": ";".join(str(item.get("locator", "")) for item in evidence),
                "verification_passed": str(bool(claim.get("verification_passed"))).lower(),
                "verification_result": claim.get("verification_result", ""),
            }
        )
    claim_fields = ["id", "claim", "status", "source_ids", "locators", "verification_passed", "verification_result"]
    (output_dir / "claim-traceability.csv").write_text(_csv_text(claim_fields, claim_rows), encoding="utf-8", newline="")
    (output_dir / "sample-publication.html").write_text(_render_html(inspection), encoding="utf-8", newline="\n")

    artifact_names = ["qa-report.json", "claim-traceability.csv", "source-register.csv", "sample-publication.html"]
    manifest = {
        "schema_version": "1.0",
        "generator": GENERATOR,
        "release": {
            key: inspection.release.get(key)
            for key in (
                "release_id",
                "version",
                "release_date",
                "status",
                "classification",
                "ai_disclosure",
                "confidentiality_disclosure",
            )
        },
        "quality_gate": inspection.report["summary"],
        "artifacts": [
            {"path": name, "bytes": (output_dir / name).stat().st_size, "sha256": _sha256(output_dir / name)}
            for name in artifact_names
        ],
    }
    _write_json(output_dir / "release-manifest.json", manifest)
    checksum_names = sorted(artifact_names + ["release-manifest.json"])
    checksum_text = "".join(f"{_sha256(output_dir / name)}  {name}\n" for name in checksum_names)
    (output_dir / "CHECKSUMS.sha256").write_text(checksum_text, encoding="ascii", newline="\n")
    return manifest


def verify_release(output: str | Path) -> dict[str, Any]:
    """Verify every path and digest declared by CHECKSUMS.sha256."""

    output_dir = Path(output).resolve()
    checksum_path = output_dir / "CHECKSUMS.sha256"
    checks: list[dict[str, Any]] = []
    if not checksum_path.is_file():
        _check(checks, "VERIFY.CHECKSUM_FILE", False, "CHECKSUMS.sha256 is missing.")
    else:
        _check(checks, "VERIFY.CHECKSUM_FILE", True, "CHECKSUMS.sha256 is present.")
        try:
            lines = checksum_path.read_text(encoding="ascii").splitlines()
        except (OSError, UnicodeError) as exc:
            lines = []
            _check(checks, "VERIFY.CHECKSUM_READ", False, f"Could not read checksum file: {exc}")
        declared_names: list[str] = []
        for line_number, line in enumerate(lines, start=1):
            match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if not match:
                _check(checks, "VERIFY.LINE_FORMAT", False, f"Malformed checksum line {line_number}.")
                continue
            expected, name = match.groups()
            declared_names.append(name)
            path = output_dir / name
            exists = path.is_file()
            _check(checks, "VERIFY.FILE_EXISTS", exists, f"{name} is present.", context={"path": name})
            if exists:
                actual = _sha256(path)
                _check(
                    checks,
                    "VERIFY.DIGEST",
                    actual == expected,
                    f"{name} matches its SHA-256 digest." if actual == expected else f"{name} digest does not match.",
                    context={"path": name},
                )
        required_names = set(GENERATED_FILES) - {"CHECKSUMS.sha256"}
        declared_set = set(declared_names)
        complete = declared_set == required_names and len(declared_names) == len(declared_set)
        missing = sorted(required_names - declared_set)
        unexpected = sorted(declared_set - required_names)
        detail = []
        if missing:
            detail.append(f"missing: {', '.join(missing)}")
        if unexpected:
            detail.append(f"unexpected: {', '.join(unexpected)}")
        if len(declared_names) != len(declared_set):
            detail.append("duplicate entries")
        _check(
            checks,
            "VERIFY.COMPLETE_SET",
            complete,
            "Checksum file declares the complete release artifact set."
            if complete
            else "Checksum artifact set is incomplete or invalid (" + "; ".join(detail) + ").",
        )
    errors = sum(item["status"] == "fail" for item in checks)
    return {
        "schema_version": "1.0",
        "generator": GENERATOR,
        "summary": {
            "status": "pass" if errors == 0 else "fail",
            "total": len(checks),
            "passed": sum(item["status"] == "pass" for item in checks),
            "errors": errors,
        },
        "checks": checks,
    }
