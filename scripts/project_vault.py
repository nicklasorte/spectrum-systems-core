#!/usr/bin/env python3
"""Project data-lake meeting baselines into an Obsidian vault.

VIEW-ONLY OUTPUT PROJECTION. This is output-side tooling, not part of the
extraction pipeline: it READS the committed reference baselines from the
data-lake and WRITES a regenerable Obsidian markdown vault OUTSIDE the
data-lake. It NEVER mutates the data-lake — it opens nothing under the
data-lake for writing, and the output directory must not live inside the
data-lake (asserted at startup). The vault is a disposable view: delete
it and re-run to rebuild.

Reads:  <data-lake>/store/processed/meetings/<sid>/reference_baselines/
            opus_reference_minutes.jsonl  (+ codex_*.jsonl for divergence)
        <data-lake>/store/raw/meetings/<sid>/source.txt  (transcript view)
Writes: <vault>/{meetings,transcripts}/*.md + <vault>/index.md

Defaults match the operator's local layout (~/data-lake, ~/Documents/
spectrum-vault); override with --data-lake / --vault. No network, no LLM.
"""
import argparse
import collections
import json
import pathlib
import re

# Order entity sections sensibly
SECTION_ORDER = ["decisions","action_items","commitments","open_questions","risks",
    "dissent_or_objection","position_statement","issue_registry_entry","claims",
    "technical_parameters","regulatory_references","precedent_reference",
    "external_stakeholder_input","cross_references","named_artifacts","glossary_definition",
    "procedural_ruling","agenda_item","topics","meeting_phases","scheduled_events",
    "sentiment_indicators","attendees"]


def load(jsonl):
    if not jsonl.exists(): return []
    return [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]

def date_from_id(sid):
    # try to find a date pattern in the source_id
    m = re.search(r'(\d{1,2})[-/](\d{1,2})[-/](20\d{2})', sid)
    if m: return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.search(r'(20\d{2})(\d{2})(\d{2})', sid)
    if m: return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r'(\d{1,2})([a-z]{3})(20\d{2})', sid.lower())
    if m:
        mon = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}.get(m.group(2))
        if mon: return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
    return "unknown"

def esc(s):
    return str(s).replace("|","\\|").replace("\n"," ").strip() if s else ""

def render_item(etype, item):
    d = item.get("item_data") or {}
    gt = esc(item.get("ground_truth_text"))
    # pick the most useful secondary fields per type
    bits = []
    for k in ("owner","raised_by","speaker","asked_by","objector","agency","ruled_by","defined_by","mentioned_by","relayed_by","presenter"):
        if d.get(k): bits.append(f"*{d[k]}*"); break
    for k in ("decision_subtype","severity","status","position_type","issue_type","sentiment","priority","input_type","ruling_type"):
        if d.get(k): bits.append(f"`{d[k]}`")
    if d.get("value") and d.get("unit"): bits.append(f"**{esc(d['value'])} {esc(d['unit'])}**")
    elif d.get("value"): bits.append(f"**{esc(d['value'])}**")
    if d.get("due"): bits.append(f"due: {esc(d['due'])}")
    if d.get("date"): bits.append(f"date: {esc(d['date'])}")
    meta = " · ".join(bits)
    line = f"- {gt}"
    if meta: line += f"  \n  {meta}"
    sq = d.get("source_quote")
    if sq and esc(sq) != gt:
        line += f'  \n  > "{esc(sq)}"'
    return line

def build_meeting(mdir, vault, raw):
    sid = mdir.name
    opus = load(mdir / "reference_baselines" / "opus_reference_minutes.jsonl")
    codex = load(mdir / "reference_baselines" / "codex_reference_minutes.jsonl")
    if not opus: return None
    date = date_from_id(sid)
    opus_by = collections.defaultdict(list)
    for r in opus: opus_by[r.get("extraction_type")].append(r)
    codex_counts = collections.Counter(r.get("extraction_type") for r in codex)
    opus_counts = collections.Counter(r.get("extraction_type") for r in opus)

    # transcript companion
    src = raw / sid / "source.txt"
    tnote = None
    if src.exists():
        tnote = f"{sid}-transcript"
        (vault / "transcripts" / f"{tnote}.md").write_text(
            f"---\nsource_id: {sid}\ntype: transcript\n---\n# Transcript: {sid}\n\n> [!note] Read-only view. Source: data-lake/store/raw/meetings/{sid}/source.txt\n\n```\n"
            + src.read_text(encoding="utf-8") + "\n```\n", encoding="utf-8")

    # Derive the model + schema from the baseline rows themselves (every
    # row stamps model_id / schema_version) rather than hardcoding a
    # model string in this view-only tool.
    model = opus[0].get("model_id", "")
    schema_version = opus[0].get("schema_version", "")
    out = [f"---", f"source_id: {sid}", f"meeting_date: {date}",
           f"model: {model}", f"schema_version: {schema_version}",
           f"total_items: {len(opus)}", f"type: meeting", f"---", ""]
    out.append(f"# {sid}")
    out.append(f"\n**Date:** {date} · **Opus items:** {len(opus)} · **Codex items:** {len(codex)}")
    if tnote: out.append(f"\n📄 Transcript: [[{tnote}]]")
    out.append("")

    for etype in SECTION_ORDER:
        items = opus_by.get(etype)
        if not items: continue
        title = etype.replace("_"," ").title()
        out.append(f"\n## {title} ({len(items)})\n")
        for it in items:
            out.append(render_item(etype, it))

    # divergence panel
    out.append("\n## Codex Divergence\n")
    out.append("> [!info]- Where Codex and Opus differ by count")
    diffs = []
    for et in SECTION_ORDER:
        o, c = opus_counts.get(et,0), codex_counts.get(et,0)
        if o != c: diffs.append(f"> - **{et}**: Opus {o} / Codex {c}")
    out.append("\n".join(diffs) if diffs else "> Counts match across all entity types.")
    out.append("")

    (vault / "meetings" / f"{sid}.md").write_text("\n".join(out), encoding="utf-8")
    return {"sid": sid, "date": date, "opus": len(opus), "codex": len(codex)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-lake", default=str(pathlib.Path.home() / "data-lake"),
                    help="Data-lake root (read-only). Default: ~/data-lake")
    ap.add_argument("--vault", default=str(pathlib.Path.home() / "Documents" / "spectrum-vault"),
                    help="Obsidian vault output dir (must be OUTSIDE the data-lake). "
                         "Default: ~/Documents/spectrum-vault")
    args = ap.parse_args()

    lake = pathlib.Path(args.data_lake).resolve()
    vault = pathlib.Path(args.vault).resolve()
    meetings = lake / "store" / "processed" / "meetings"
    raw = lake / "store" / "raw" / "meetings"

    # Read-only contract: the vault must never live inside the data-lake,
    # so a projection run can never write into the data-lake tree.
    if vault == lake or lake in vault.parents:
        raise SystemExit(
            f"refusing to project: --vault {vault} is inside --data-lake "
            f"{lake}; the vault must be a separate, disposable directory"
        )
    if not meetings.is_dir():
        raise SystemExit(f"no processed meetings dir at {meetings}")

    vault.mkdir(parents=True, exist_ok=True)
    (vault / "meetings").mkdir(exist_ok=True)
    (vault / "transcripts").mkdir(exist_ok=True)

    rows = []
    for mdir in sorted(meetings.iterdir()):
        if mdir.is_dir():
            r = build_meeting(mdir, vault, raw)
            if r: rows.append(r)

    rows.sort(key=lambda x: x["date"])
    idx = ["---", "type: index", "---", "# Spectrum Systems — Meeting Index", "",
           f"**{len(rows)} meetings** · Opus reference baselines", "",
           "| Date | Meeting | Opus | Codex |", "|---|---|---|---|"]
    for r in rows:
        idx.append(f"| {r['date']} | [[{r['sid']}]] | {r['opus']} | {r['codex']} |")
    (vault / "index.md").write_text("\n".join(idx), encoding="utf-8")

    print(f"Projected {len(rows)} meetings into {vault}")
    print(f"  meetings/  ({len(rows)} notes)")
    print(f"  transcripts/  (linked companions)")
    print(f"  index.md  (front door)")


if __name__ == "__main__":
    main()
