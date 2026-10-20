"""Durable public bibliography: identifier merges, derived features, no abstracts."""
import html
import json
import re
from datetime import datetime, timezone, date
from urllib.parse import quote
import metadata
import hot

PRIVATE = re.compile(r"/Users/|/Volumes/|/mnt/|192\.168\.|127\.0\.0\.1|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{15,}|-----BEGIN .*PRIVATE KEY-----", re.I)


def text(value, limit=700):
    value = html.unescape(html.unescape(str(value or "")))
    value = re.sub(r"</?(?:sup|sub|i|b|em|strong|scp)(?:\s[^>]*)?>", "", value, flags=re.I)
    return " ".join(value.split())[:limit]


def valid_date(value):
    value = str(value or "")[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except (ValueError, TypeError):
        return None


def aliases(p):
    out = []
    if re.fullmatch(r"[1-9][0-9]{0,11}", str(p.get("pmid") or "")):
        out.append("pmid:"+str(p["pmid"]))
    for key in ("doi", "published_doi"):
        d = metadata.doi(p.get(key))
        if d:
            out.append("doi:"+d)
    if re.fullmatch(r"PMC[0-9]+", str(p.get("pmcid") or "")):
        out.append("pmcid:"+p["pmcid"])
    a = re.sub(r"v[0-9]+$", "", str(p.get("arxiv") or ""))
    if re.fullmatch(r"(?:[0-9]{4}\.[0-9]{4,5}|[a-z-]+/[0-9]{7})", a):
        out.append("arxiv:"+a)
    source_id = str(p.get("id") or "")
    if p.get("source") == "epmc" and re.fullmatch(r"(?:MED|PMC|PPR):[A-Za-z0-9]+", source_id):
        out.append("epmc:"+source_id)
    return sorted(set(out))


def canonical(ids):
    for prefix in ("pmid:", "doi:", "arxiv:", "pmcid:", "epmc:"):
        group = sorted(x for x in ids if x.startswith(prefix) and not (prefix == "doi:" and x.lower().startswith("doi:10.48550/arxiv.")))
        if group:
            key = group[0]
            if prefix == "pmid:": return key, "https://pubmed.ncbi.nlm.nih.gov/"+key[5:]+"/"
            if prefix == "doi:": return key, "https://doi.org/"+quote(key[4:], safe="/()")
            if prefix == "arxiv:": return key, "https://arxiv.org/abs/"+key[6:]
            if prefix == "pmcid:": return key, "https://europepmc.org/article/PMC/"+key[6:]
            src, ident = key[5:].split(":", 1)
            return key, "https://europepmc.org/article/"+src+"/"+ident
    raise ValueError("missing_public_identifier")


def empty():
    return {"schema_version": 2, "papers": {}, "identity_conflicts": 0, "source_records_processed": 0}


class Catalogue:
    def __init__(self, data=None, rules=None):
        self.data = data or empty()
        self.rules = rules or {}
        self.index = {}
        self.version_aliases = {a: g["canonical"] for g in self.rules.get("version_groups", []) for a in g["aliases"]}
        self.reindex()

    def reindex(self):
        self.index = {alias: key for key, p in self.data["papers"].items() for alias in p["aliases"]}

    def update(self, p, rankings, observed_at, run_id):
        ids = aliases(p)
        for alias in list(ids):
            if alias in self.version_aliases:
                ids.append(self.version_aliases[alias])
        ids = sorted(set(ids))
        if not ids:
            raise ValueError("source_record_without_public_identifier")
        existing = sorted({self.index[x] for x in ids if x in self.index})
        priors = [self.data["papers"][k] for k in existing]
        all_ids = sorted(set(ids+[a for old in priors for a in old["aliases"]]))
        pmids = {x for x in all_ids if x.startswith("pmid:")}
        journal_dois = {old["journal_doi"] for old in priors if old.get("journal_doi")}
        if p.get("kind") != "preprint" and metadata.doi(p.get("doi")):
            journal_dois.add("doi:"+metadata.doi(p["doi"]))
        if len(pmids) > 1 or len(journal_dois) > 1:
            self.data["identity_conflicts"] += 1
            for old in priors: old["identity_review"] = True
            return None
        selected_ids = [a for a in all_ids if a not in self.version_aliases]
        if journal_dois:
            selected_ids = [a for a in selected_ids if not a.startswith("doi:") or a in journal_dois]
        key, url = canonical(selected_ids)
        old = {}
        for previous in sorted(priors, key=lambda x: x["first_seen"], reverse=True):
            for k, v in previous.items():
                if k not in ("rankings", "metrics", "sources"): old[k] = v
            old.setdefault("rankings", {}).update(previous.get("rankings", {}))
            old.setdefault("metrics", []).extend(previous.get("metrics", []))
            old.setdefault("sources", []).extend(previous.get("sources", []))
        source = p.get("source")
        if source not in ("pubmed", "epmc", "arxiv", "biorxiv", "medrxiv"): raise ValueError("unknown_public_source")
        incoming_journal = p.get("kind") != "preprint"
        retain_journal = old.get("kind") == "journal" and not incoming_journal
        first_seen = min([old.get("first_seen", observed_at), str(p.get("first_seen") or observed_at), observed_at])
        record = {**old, "key": key, "url": url, "aliases": all_ids,
                  "first_seen": first_seen, "last_seen": observed_at,
                  "identity_review": bool(old.get("identity_review")),
                  "retracted": bool(old.get("retracted") or p.get("retracted")),
                  "sources": sorted(set(old.get("sources", [])+[source])),
                  "source": old.get("source") if retain_journal else source,
                  "kind": "journal" if incoming_journal or old.get("kind") == "journal" else "preprint",
                  "title": old.get("title") if retain_journal else text(p.get("title")) or old.get("title", ""),
                  "journal": text(p.get("journal"), 200) or old.get("journal", ""),
                  "rankings": old.get("rankings", {}), "metrics": old.get("metrics", [])}
        record["journal_doi"] = next(iter(journal_dois), None)
        published = next((r.get("published") for r in rankings.values() if r.get("published")), valid_date(p.get("published")))
        if not retain_journal:
            known = valid_date(record.get("published"))
            if old.get("kind") != "journal" and incoming_journal:
                record["published"] = valid_date(published)
            else:
                record["published"] = min([v for v in [known, valid_date(published)] if v], default=None)
        version_date = valid_date(p.get("updated") or p.get("version_date")) if p.get("kind") == "preprint" and source in ("arxiv", "biorxiv", "medrxiv") else None
        record["version_updated"] = max([v for v in [version_date, record.get("version_updated")] if v], default=None) if record["kind"] == "preprint" else None
        overrides = self.rules.get("classification", {})
        for track, r in rankings.items():
            if track not in topics_names(): raise ValueError("unknown_track")
            previous = record["rankings"].get(track, {})
            if retain_journal and previous.get("source_kind") == "journal": continue
            features = hot.signals(p, r)
            if not features["abstract_available"] and previous.get("signals", {}).get("abstract_available"):
                continue
            override = next((overrides[a].get(track) for a in all_ids if a in overrides and track in overrides[a]), None)
            compact = {"eligible": bool(r["eligible"]), "status": r.get("status", "related"),
                       "score": r["score"], "topic_strength": max(0, r["score_components"]["topic_relevance"]+min(0, r["score_components"].get("application_priority", 0))),
                       "score_components": r["score_components"], "published": r.get("published"),
                       "labels": r.get("labels", []), "article_type": r.get("article_type", ""),
                       "method_cue": bool(r.get("method_cue")), "signals": features,
                       "abstract_classification_checked": bool(r.get("review")), "source_kind": "journal" if incoming_journal else "preprint"}
            if override:
                for field in ("article_type", "method_cue"):
                    if field in override: compact[field] = override[field]
                compact["signals"]["method"] = compact["method_cue"]
                compact["abstract_classification_checked"] = True
                if override.get("force_related"): compact["eligible"] = False
            record["rankings"][track] = compact
        if source == "epmc" and type(p.get("citations")) is int and p["citations"] >= 0:
            metric = {"source": "Europe PMC", "metric": "citedByCount", "entity": str(p["id"]),
                      "lineage": "epmc-citedByCount-v1", "status": "ok", "value": p["citations"],
                      "observed_at": observed_at, "run_id": run_id}
            record["metrics"] = [m for m in record["metrics"] if not (m["entity"] == metric["entity"] and m.get("run_id") == run_id)] + [metric]
        # Keep eight real observations for each source entity; never use search hits as counters.
        histories = {}
        for metric in record["metrics"]:
            histories.setdefault((metric["source"], metric["entity"], metric["metric"]), []).append(metric)
        record["metrics"] = [v for values in histories.values() for v in sorted(values, key=lambda x: x["observed_at"])[-8:]]
        for previous_key in existing:
            self.data["papers"].pop(previous_key, None)
        self.data["papers"][key] = record
        for alias in all_ids: self.index[alias] = key
        self.data["source_records_processed"] += 1
        return key

    def trim(self, now, max_age_days=180):
        # This is a public runtime cache, not a sealed scientific archive.
        keep = {}
        for key, p in self.data["papers"].items():
            pub = valid_date(p.get("published")); updated = valid_date(p.get("version_updated"))
            recent = max([v for v in [pub, updated] if v], default=valid_date(p["first_seen"]))
            if recent and (now.date()-date.fromisoformat(recent)).days <= max_age_days: keep[key] = p
        self.data["papers"] = keep
        self.reindex()


def topics_names():
    return {"CHIP_mCA", "AI_medical_imaging", "omics_methods_applications", "clinical_observational"}


def validate_public(data):
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False)
    if PRIVATE.search(raw): raise ValueError("private_data_detected")
    for field in ('"abstract"', '"abstract_excerpt"', '"result_path"', '"api_key"', '"authorization"'):
        if field in raw.lower(): raise ValueError("forbidden_public_field")
    if len(raw.encode()) > 24_000_000: raise ValueError("public_file_size_limit")
    return raw
