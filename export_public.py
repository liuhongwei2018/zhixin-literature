"""Export a bounded public reading mirror; never copy the backend's runtime files."""
import argparse
import html
import json
import math
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

TRACKS = {
    "CHIP_mCA": "CHIP / mCA",
    "AI_medical_imaging": "AI 医学影像",
    "omics_methods_applications": "组学方法与应用",
    "clinical_observational": "病案与临床数据研究",
}
LANES = {
    "pubmed_new": "PubMed · 新收录", "pubmed_updates": "PubMed · 题录更新",
    "epmc_new": "Europe PMC · 新收录", "epmc_updates": "Europe PMC · 题录更新",
    "arxiv": "arXiv", "biorxiv": "bioRxiv", "medrxiv": "medRxiv",
}
SCORE_KEYS = ("topic_relevance", "publication_recency", "article_type_cues",
              "metadata_availability", "application_priority", "abstract_review_adjustment")
PRIVATE = re.compile(r"/Users/|/Volumes/|/mnt/|192\.168\.|127\.0\.0\.1|"
                     r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{15,}|"
                     r"-----BEGIN .*PRIVATE KEY-----", re.I)


def clean_text(value, limit=700):
    text = html.unescape(html.unescape(str(value or "")))
    text = re.sub(r"</?(?:sup|sub|i|b|em|strong|scp)(?:\s[^>]*)?>", "", text, flags=re.I)
    return " ".join(text.split())[:limit]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid numeric public field")
    return value


def canonical(p):
    pmid = str(p.get("pmid") or "")
    if re.fullmatch(r"[1-9][0-9]{0,11}", pmid):
        return "pmid:" + pmid, "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/"
    doi = str(p.get("doi") or "").strip().lower()
    if re.fullmatch(r"10\.[0-9]{4,9}/[^\s]+", doi):
        return "doi:" + doi, "https://doi.org/" + quote(doi, safe="/()")
    arxiv = str(p.get("arxiv") or "")
    if re.fullmatch(r"(?:[0-9]{4}\.[0-9]{4,5}|[a-z-]+/[0-9]{7})(?:v[0-9]+)?", arxiv):
        return "arxiv:" + arxiv, "https://arxiv.org/abs/" + arxiv
    # A source-only record is usable only with a validated Europe PMC identifier.
    source_id = str(p.get("id") or "")
    if p.get("source") == "epmc" and re.fullmatch(r"(?:MED|PMC|PPR):[A-Za-z0-9]+", source_id):
        source, ident = source_id.split(":")
        return "epmc:" + source_id, "https://europepmc.org/article/" + source + "/" + ident
    raise ValueError("no validated canonical publication identifier")


def date_value(value):
    value = str(value or "")[:10]
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    datetime.strptime(value, "%Y-%m-%d")
    return value


def public_paper(p):
    r = p.get("ranking") or {}
    if not r.get("eligible") or p.get("retracted") or p.get("identity_status") != "active":
        return None
    key, url = canonical(p)
    kind = "preprint" if p.get("kind") == "preprint" else "journal"
    return {
        "key": key, "url": url, "title": clean_text(p.get("title")),
        "journal": clean_text(p.get("journal"), 200), "kind": kind,
        "published": date_value(r.get("published")),
        "version_updated": date_value(p.get("updated") or p.get("version_date")) if kind == "preprint" else None,
        "first_seen": date_value(p.get("first_seen")),
        "source": p.get("source") if p.get("source") in ("pubmed", "epmc", "arxiv", "biorxiv", "medrxiv") else "other",
        "tracks": [k for k in TRACKS if k in p.get("tracks", [])],
    }


def public_rank(p, position):
    r = p["ranking"]
    return {"position": position, "score": number(r["score"]),
            "article_type": clean_text(r.get("article_type"), 100),
            "labels": [clean_text(v, 80) for v in r.get("labels", []) if isinstance(v, str)],
            "method_cue": bool(r.get("method_cue")),
            "score_components": {k: number(v) for k, v in r.get("score_components", {}).items() if k in SCORE_KEYS},
            "abstract_classification_checked": bool(r.get("review")),
            "full_text_quality_reviewed": False}


def export(report, notes=None, now=None, overrides=None):
    if set(report.get("tracks", {})) != set(TRACKS):
        raise ValueError("expected exactly the four confirmed research directions")
    if report.get("formal_hot_enabled") or report.get("push_enabled"):
        raise ValueError("this mirror requires pushes and formal HOT to remain disabled")
    now = now or datetime.now(timezone.utc)
    jobs = report.get("jobs", [])
    public_jobs = []
    seen_jobs = set()
    for j in jobs:
        if j.get("profile") not in (*TRACKS, "ALL") or j.get("lane") not in LANES:
            raise ValueError("unexpected source job")
        ident = (j["profile"], j["lane"], j.get("slot"))
        if ident in seen_jobs:
            raise ValueError("duplicate source coverage entry")
        seen_jobs.add(ident)
        if j.get("status") not in ("complete", "pending", "running", "failed"):
            raise ValueError("unexpected source status")
        public_jobs.append({"track": j["profile"], "source": j["lane"],
                            "name": LANES[j["lane"]], "status": j["status"],
                            "start": date_value(j.get("start")), "through": date_value(j.get("through"))})
    complete = bool(public_jobs) and all(j["status"] == "complete" for j in public_jobs)
    overrides = overrides or {}
    aliases = {alias: group["canonical"] for group in overrides.get("version_groups", []) for alias in group["aliases"]}
    exclusions = {(p["track"], p["key"]) for p in overrides.get("exclusions", [])}
    representatives = {}
    for t in report["tracks"].values():
        for p in t.get("reading_list", [])[:30]:
            item = public_paper(p)
            if item:
                representatives[item["key"]] = p
    papers, tracks = {}, {}
    excluded_entries, merged_entries = 0, 0
    for key, name in TRACKS.items():
        source = report["tracks"][key]
        reading, rankings = [], {}
        for p in source.get("reading_list", [])[:30]:
            item = public_paper(p)
            if item and (key, item["key"]) in exclusions:
                excluded_entries += 1
                continue
            if item and item["key"] in aliases and aliases[item["key"]] in representatives:
                p = representatives[aliases[item["key"]]]
                item = public_paper(p)
                merged_entries += 1
            if not item or item["key"] in reading:
                continue
            pid = item["key"]
            if pid in papers and papers[pid]["title"] != item["title"]:
                raise ValueError("conflicting public publication identity")
            papers[pid] = item
            reading.append(pid)
            rankings[pid] = public_rank(p, len(reading))
        source_jobs = [j for j in public_jobs if j["track"] in (key, "ALL")]
        tracks[key] = {"name": name, "candidate_count": number(source["candidate_count"]),
                       "readable_count": number(source["readable_count"]), "reading": reading,
                       "rankings": rankings, "all_due_sources_complete": bool(source_jobs) and all(j["status"] == "complete" for j in source_jobs)}
    highlights = []
    for p in (notes or {}).get("papers", []):
        if p.get("track") not in TRACKS or not p.get("abstract_classification_checked"):
            continue
        key, url = canonical(p)
        highlights.append({"key": key, "url": url, "track": p["track"],
                           "title": clean_text(p.get("original_title")),
                           "short_title_zh": clean_text(p.get("short_title_zh"), 120),
                           "summary_zh": clean_text(p.get("summary_zh"), 500),
                           "published": date_value(p.get("source_publication_date")),
                           "article_type": clean_text(p.get("article_type"), 100),
                           "abstract_classification_checked": True,
                           "full_text_quality_reviewed": False})
    result = {"schema_version": 1, "title": "知新 · 文献阅读清单",
              "mode": "public_reading_mirror", "generated_at": now.isoformat(),
              "source_as_of": report["as_of"], "timezone": "Asia/Shanghai",
              "daily_scan_local": "08:00", "weekly_summary": "周一",
              "scanner_location": "independent_local_backend", "cloud_scanner_enabled": False,
              "formal_hot_enabled": False, "push_enabled": False,
              "all_due_sources_complete": complete,
              "coverage": {"complete": sum(j["status"] == "complete" for j in public_jobs), "total": len(public_jobs), "jobs": public_jobs},
              "displayed_unique_papers": len(papers), "papers": papers, "tracks": tracks,
              "public_screening": {"excluded_reading_entries": excluded_entries, "version_entries_merged": merged_entries,
                                   "description": "公开清单另排除已核实的词义误命中，并按已核实的来源版本关系合并重复展示。后台候选统计保留原口径。"},
              "highlights_as_of": (notes or {}).get("as_of"), "highlights": highlights,
              "ranking_description": "方向内阅读优先级由主题相关性、发表日期、文章类型线索、元数据完整度和应用场景共同生成。组学清单兼顾不同技术。分数不代表研究质量，也不用于跨方向比较。",
              "update_description": "页面展示独立后台生成的公开阅读清单快照。自动检索仍在独立后台运行，需要运行环境在线；网页以显示的实际数据时间为准。未完成来源不视为零结果。"}
    validate(result)
    return result


def validate(data):
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
    if PRIVATE.search(encoded):
        raise ValueError("private path, network identifier or credential detected; export stopped")
    if any(k in encoded for k in ('"abstract_excerpt"', '"abstract"', '"runtime"', '"config"', '"result_path"', '"receipts"')):
        raise ValueError("non-public backend fields detected")
    if data.get("formal_hot_enabled") or data.get("push_enabled"):
        raise ValueError("unexpected delivery or HOT state")
    for p in [*data["papers"].values(), *data["highlights"]]:
        url = urlparse(p["url"])
        if url.scheme != "https" or url.hostname not in ("pubmed.ncbi.nlm.nih.gov", "doi.org", "arxiv.org", "europepmc.org") or url.username or url.password:
            raise ValueError("unsafe publication link")
    if set(data["tracks"]) != set(TRACKS):
        raise ValueError("unexpected directions")
    for t in data["tracks"].values():
        if len(t["reading"]) != len(set(t["reading"])) or not set(t["reading"]) <= data["papers"].keys():
            raise ValueError("duplicate or missing publication")


def fetch_snapshot(url):
    p = urlparse(url)
    if p.scheme != "http" or p.hostname not in ("127.0.0.1", "localhost") or p.username or p.password:
        raise ValueError("exporter only reads an explicitly selected local HTTP service")
    with urllib.request.urlopen(url, timeout=30) as response:
        raw = response.read(10_000_001)
    if len(raw) > 10_000_000:
        raise ValueError("public report exceeds export size limit")
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--snapshot", type=Path)
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--overrides", type=Path, default=Path(__file__).resolve().parent / "publication_rules.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = fetch_snapshot(args.url) if args.url else json.loads(args.snapshot.read_text())
    notes = json.loads(args.notes.read_text()) if args.notes else None
    overrides = json.loads(args.overrides.read_text()) if args.overrides.exists() else None
    result = export(report, notes, overrides=overrides)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    temp.replace(args.output)
    print(json.dumps({"unique_papers": result["displayed_unique_papers"], "coverage": {k: result["coverage"][k] for k in ("complete", "total")}, "source_as_of": result["source_as_of"]}))


if __name__ == "__main__":
    main()
