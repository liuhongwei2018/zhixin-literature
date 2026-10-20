"""Complete-or-fail source retrieval. HTTP and storage are supplied by s02.

No success cursor is written here. A caller may advance it only after parsing,
screening and the normal check gate have also completed.
"""
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
import xml.etree.ElementTree as ET

SEARCH_CONTRACT = "complete-indexed-v2"
PM_SPLIT_LIMIT = 5000  # safely below PubMed ESearch's 9,999 accessible IDs
PM_PAGE_SIZE = 5000
EP_PAGE_SIZE = 1000


class IncompleteSearch(RuntimeError):
    def __init__(self, source, reason, **details):
        self.details = {"status": "incomplete", "source": source, "reason": reason, **details}
        super().__init__(json.dumps(self.details, ensure_ascii=False, sort_keys=True))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def checked_ids(ids, count, source, **details):
    if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids):
        raise IncompleteSearch(source, "invalid_ids", **details)
    if len(ids) != count or len(set(ids)) != count:
        raise IncompleteSearch(source, "count_mismatch", expected=count,
                               returned=len(ids), unique=len(set(ids)), **details)


def complete_result(source, date_field, start, end, ids, **extra):
    return {"contract": SEARCH_CONTRACT, "status": "complete", "source": source,
            "date_field": date_field, "from": start, "to": end,
            "count": len(ids), "returned": len(ids), "unique": len(set(ids)),
            "ids_hash": digest(ids), "completed_at": datetime.now(timezone.utc).isoformat(), **extra}


def require_complete(meta, ids, source):
    if meta.get("contract") != SEARCH_CONTRACT or meta.get("status") != "complete":
        raise IncompleteSearch(source, "unverified_cache")
    checked_ids(ids, meta.get("count"), source)
    if meta.get("ids_hash") != digest(ids):
        raise IncompleteSearch(source, "cache_hash_mismatch")


def validate_retrieval_state(state):
    if state.get("contract") != SEARCH_CONTRACT or state.get("retrieval_complete") is not True:
        raise IncompleteSearch("check", "unverified_retrieval")
    required = {"pubmed_new", "pubmed_updates", "epmc_new", "epmc_updates"}
    lanes = state.get("lanes", {})
    if not required.issubset(lanes) or any(lanes[k].get("status") != "complete" for k in required):
        raise IncompleteSearch("check", "incomplete_lane")
    for key in required:
        lane = lanes[key]
        counts = [lane.get(field) for field in ("count", "returned", "unique")]
        if any(not isinstance(n, int) or n < 0 for n in counts) or len(set(counts)) != 1:
            raise IncompleteSearch("check", "lane_count_mismatch", lane=key)


def pubmed_range(query, start, end, *, date_field, scope, cache_dir, request, hard_max):
    """Date bisect before ESearch limits; reject inconsistent or truncated pages.

    Each completed child shard is cached independently. A retry can reuse those
    shards but cannot reuse a partial root result. Single-day overflow stops with
    its exact interval; it never claims completeness or advances a watermark.
    """
    first_day, last_day = date.fromisoformat(start), date.fromisoformat(end)
    if first_day > last_day or date_field not in {"crdt", "mdat"}:
        raise ValueError("Invalid PubMed date range or field")

    def retrieve(lo, hi):
        key = digest([SEARCH_CONTRACT, query, date_field, lo, hi, scope])[:24]
        cache = cache_dir / f"esearch_v2_{key}.json"
        if cache.exists():
            out = json.loads(cache.read_text())
            require_complete(out, out.get("pmids"), "pubmed")
            return out
        base = dict(db="pubmed", term=query, retmode="json", datetype=date_field,
                    mindate=lo.replace("-", "/"), maxdate=hi.replace("-", "/"))

        def search(**paging):
            payload = request({**base, **paging})
            result = payload.get("esearchresult", {})
            if payload.get("error") or payload.get("ERROR") or result.get("error") or result.get("errorlist") or result.get("ERROR"):
                raise IncompleteSearch("pubmed", "api_error", interval=[lo, hi])
            try:
                count = int(result["count"])
            except (KeyError, TypeError, ValueError):
                raise IncompleteSearch("pubmed", "invalid_response", interval=[lo, hi])
            if count < 0:
                raise IncompleteSearch("pubmed", "invalid_count", interval=[lo, hi])
            return count, result

        count, header = search(retmax=0)
        if count > hard_max:
            raise IncompleteSearch("pubmed", "hard_limit", expected=count, limit=hard_max,
                                   interval=[lo, hi])
        partitions = []
        if count > PM_SPLIT_LIMIT:
            d1, d2 = date.fromisoformat(lo), date.fromisoformat(hi)
            if d1 == d2:
                raise IncompleteSearch("pubmed", "single_day_overflow", expected=count,
                                       limit=PM_SPLIT_LIMIT, interval=[lo, hi])
            mid = d1 + (d2 - d1) // 2
            left = retrieve(lo, mid.isoformat())
            right = retrieve((mid + timedelta(days=1)).isoformat(), hi)
            ids = left["pmids"] + right["pmids"]
            partitions = left["partitions"] + right["partitions"]
        else:
            ids = []
            for offset in range(0, count, PM_PAGE_SIZE):
                page_count, page = search(retstart=offset, retmax=min(PM_PAGE_SIZE, count - offset))
                if page_count != count:
                    raise IncompleteSearch("pubmed", "count_changed", expected=count,
                                           observed=page_count, interval=[lo, hi])
                batch = page.get("idlist")
                checked_ids(batch, min(PM_PAGE_SIZE, count - offset), "pubmed", interval=[lo, hi])
                ids.extend(batch)
            partitions = [{"from": lo, "to": hi, "count": count}]
        checked_ids(ids, count, "pubmed", interval=[lo, hi])
        out = complete_result("pubmed", date_field, lo, hi, ids, pmids=ids,
                              partitions=partitions, querytranslation=header.get("querytranslation"),
                              warnings=header.get("warninglist"))
        cache.write_text(json.dumps(out, ensure_ascii=False))
        return out

    return retrieve(first_day.isoformat(), last_day.isoformat())


def epmc_identity(record):
    if not record.get("source") or not record.get("id"):
        raise IncompleteSearch("europepmc", "missing_identity")
    return str(record["source"]) + ":" + str(record["id"])


def epmc_range(query, start, end, *, date_field, scope, cache_dir, request, hard_max):
    if date_field not in {"FIRST_IDATE", "UPDATE_DATE"} or date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Invalid Europe PMC date range or field")
    full_query = f"({query}) AND {date_field}:[{start} TO {end}]"
    key = digest([SEARCH_CONTRACT, full_query, scope])[:24]
    records, ids, visited, cursor, expected, pages = [], [], set(), "*", None, 0
    while True:
        if cursor in visited:
            raise IncompleteSearch("europepmc", "cursor_cycle", returned=len(ids), expected=expected)
        visited.add(cursor)
        cache = cache_dir / f"search_v2_{key}_{digest(cursor)[:16]}.json"
        if cache.exists():
            payload = json.loads(cache.read_text())
        else:
            payload = request(dict(query=full_query, format="json", resultType="core",
                                   pageSize=EP_PAGE_SIZE, cursorMark=cursor))
        try:
            observed = int(payload["hitCount"])
            batch = payload["resultList"]["result"]
            if not isinstance(batch, list) or observed < 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise IncompleteSearch("europepmc", "invalid_response")
        if payload.get("error"):
            raise IncompleteSearch("europepmc", "api_error")
        if expected is None:
            expected = observed
        if expected > hard_max:
            raise IncompleteSearch("europepmc", "hard_limit", expected=expected, limit=hard_max)
        if observed != expected:
            raise IncompleteSearch("europepmc", "count_changed", expected=expected, observed=observed)
        records.extend(batch)
        ids.extend(epmc_identity(r) for r in batch)
        pages += 1
        if len(set(ids)) != len(ids) or len(ids) > expected:
            raise IncompleteSearch("europepmc", "duplicate_or_excess_records", expected=expected,
                                   returned=len(ids), unique=len(set(ids)))
        if len(ids) == expected:
            checked_ids(ids, expected, "europepmc")
            cache.write_text(json.dumps(payload, ensure_ascii=False))
            meta = complete_result("europepmc", date_field, start, end, ids,
                                   query=full_query, pages=pages)
            (cache_dir / f"complete_v2_{key}.json").write_text(json.dumps(meta, ensure_ascii=False))
            return meta, records
        next_cursor = payload.get("nextCursorMark")
        if not batch or not next_cursor or next_cursor in visited:
            raise IncompleteSearch("europepmc", "pagination_ended_early", expected=expected,
                                   returned=len(ids), unique=len(set(ids)))
        # Cache only a structurally valid page, after its continuation is checked.
        # A truncated response must be fetched again on the same check's retry.
        cache.write_text(json.dumps(payload, ensure_ascii=False))
        cursor = next_cursor


def validate_pubmed_xml(text, expected):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise IncompleteSearch("pubmed", "invalid_xml")
    if root.tag != "PubmedArticleSet" or root.find("ERROR") is not None:
        raise IncompleteSearch("pubmed", "invalid_xml_root")
    # Unsupported book records stop explicitly; no requested ID may vanish.
    ids = [a.findtext("MedlineCitation/PMID") for a in root.findall("PubmedArticle")]
    checked_ids(ids, len(expected), "pubmed", stage="efetch")
    if set(ids) != set(expected):
        raise IncompleteSearch("pubmed", "fetch_id_mismatch", expected=len(expected), returned=len(ids))
    return text
