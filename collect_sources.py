"""Public API adapters. Failed/limited pages never advance a completed window."""
import email.utils
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
import requests
import metadata
import retrieval
import topics

HOSTS = {"eutils.ncbi.nlm.nih.gov": .6, "www.ebi.ac.uk": .4,
         "export.arxiv.org": 3.1, "api.biorxiv.org": .6}


class SourceFailure(RuntimeError):
    def __init__(self, reason, host=None):
        self.reason, self.host = reason, host
        super().__init__(reason)


class BudgetExpired(SourceFailure):
    pass


class Client:
    def __init__(self, state, budget_seconds=3300, session=None, clock=time.time, sleep=time.sleep):
        self.state, self.session = state, session or requests.Session()
        self.clock, self.sleep = clock, sleep
        self.deadline = clock()+budget_seconds
        self.last = {}
        self.requests = 0
        self.state.setdefault("cooldowns", {})

    def check_budget(self):
        if self.clock()+45 >= self.deadline:
            raise BudgetExpired("run_budget_reached")

    def get(self, url, params=None, kind="json"):
        host = urlparse(url).hostname
        if urlparse(url).scheme != "https" or host not in HOSTS:
            raise SourceFailure("unapproved_source_host")
        for attempt in range(3):
            self.check_budget()
            cooldown = self.state["cooldowns"].get(host, 0)
            if cooldown > self.clock():
                raise SourceFailure("rate_limit_cooldown", host)
            pause = HOSTS[host]-(self.clock()-self.last.get(host, 0))
            if pause > 0: self.sleep(pause)
            self.last[host] = self.clock()
            self.requests += 1
            try:
                response = self.session.get(url, params=params, timeout=(12, 35),
                    headers={"User-Agent": "ZhixinLiterature/1.0 (+https://github.com/liuhongwei2018/zhixin-literature)"},
                    allow_redirects=False)
            except requests.RequestException:
                if attempt == 2: raise SourceFailure("network_timeout_or_connection", host)
                self.sleep(2**(attempt+1)); continue
            if response.status_code == 429:
                wait = 600
                retry = response.headers.get("Retry-After", "")
                try:
                    wait = int(retry)
                except ValueError:
                    try: wait = int(email.utils.parsedate_to_datetime(retry).timestamp()-self.clock())
                    except (TypeError, ValueError): pass
                self.state["cooldowns"][host] = self.clock()+max(60, wait)
                raise SourceFailure("http_429", host)
            if response.status_code >= 500 and attempt < 2:
                self.sleep(2**(attempt+1)); continue
            if response.status_code != 200:
                raise SourceFailure("http_"+str(response.status_code), host)
            if len(response.content) > 24_000_000:
                raise SourceFailure("response_size_limit", host)
            try: return response.json() if kind == "json" else response.text
            except (ValueError, UnicodeError): raise SourceFailure("invalid_response_encoding", host)


def pubmed(client, profile, lane, start, end, cache):
    field = "crdt" if lane == "new" else "mdat"
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    meta = retrieval.pubmed_range(profile["queries"]["pubmed"], start, end,
        date_field=field, scope=profile["key"], cache_dir=cache,
        request=lambda p: client.get(url+"esearch.fcgi", {**p, "tool": "zhixin_literature"}), hard_max=100000)
    ids = meta["pmids"]
    rows = []
    for offset in range(0, len(ids), 200):
        batch = ids[offset:offset+200]
        xml = client.get(url+"efetch.fcgi", {"db": "pubmed", "id": ",".join(batch), "retmode": "xml", "tool": "zhixin_literature"}, "text")
        rows.extend(metadata.pubmed_rows(xml, batch))
    if len(rows) != len(ids): raise SourceFailure("pubmed_fetch_count_mismatch")
    return rows, {"expected": len(ids), "received": len(rows), "date_field": field, "contract": retrieval.SEARCH_CONTRACT}


def epmc(client, profile, lane, start, end, cache):
    field = "FIRST_IDATE" if lane == "new" else "UPDATE_DATE"
    meta, rows = retrieval.epmc_range(profile["queries"]["epmc"], start, end,
        date_field=field, scope=profile["key"], cache_dir=cache,
        request=lambda p: client.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", p), hard_max=100000)
    return [metadata.epmc_row(row) for row in rows], {"expected": meta["count"], "received": len(rows), "date_field": field, "contract": retrieval.SEARCH_CONTRACT}


def arxiv(client, profile, start, end, cache):
    # The sorted recent prefix is complete only after crossing the start date.
    collected, seen = [], set()
    for offset in range(0, 3000, 100):
        xml = client.get("https://export.arxiv.org/api/query", {"search_query": topics.ARXIV_SEEDS[profile["key"]],
            "start": offset, "max_results": 100, "sortBy": "lastUpdatedDate", "sortOrder": "descending"}, "text")
        total, rows = metadata.arxiv_page(xml)
        crossed = False
        for p in rows:
            if p["id"] in seen: raise SourceFailure("arxiv_duplicate_page_identity")
            seen.add(p["id"])
            if p["updated"][:10] < start: crossed = True; continue
            if p["updated"][:10] <= end: collected.append(p)
        if crossed or offset+len(rows) >= total:
            return collected, {"expected": len(collected), "received": len(collected), "date_field": "lastUpdatedDate", "contract": "sorted_recent_prefix_v1"}
        if not rows: raise SourceFailure("arxiv_pagination_ended_early")
    raise SourceFailure("arxiv_recent_prefix_limit")


def preprints(client, server, start, end, cache):
    rows, ids, cursor, expected = [], set(), 0, None
    while True:
        payload = client.get(f"https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}/json")
        messages = payload.get("messages", [])
        if len(messages) != 1 or messages[0].get("status") != "ok": raise SourceFailure(server+"_invalid_messages")
        try: total = int(messages[0]["total"]); batch = payload["collection"]
        except (ValueError, KeyError, TypeError): raise SourceFailure(server+"_invalid_response")
        if not isinstance(batch, list) or total < 0 or total > 100000: raise SourceFailure(server+"_count_limit")
        if expected is None: expected = total
        if expected != total: raise SourceFailure(server+"_count_changed")
        for p in batch:
            ident = metadata.doi(p.get("doi")); version = str(p.get("version", ""))
            if not ident or not version.isdigit(): raise SourceFailure(server+"_invalid_identity")
            identity = (ident, version)
            if identity in ids: raise SourceFailure(server+"_duplicate_versions")
            ids.add(identity)
            rows.append({"source": server, "id": ident+"v"+version, "doi": ident,
                "published_doi": metadata.doi(p.get("published")), "title": p.get("title") or "",
                "abstract": p.get("abstract") or "", "keywords": [], "published": p.get("date"),
                "updated": p.get("date"), "version_date": p.get("date"), "version": version, "kind": "preprint", "journal": server})
        if len(rows) > expected: raise SourceFailure(server+"_excess_records")
        if len(rows) == expected:
            # Newer versions are applied last; canonical first-publication date stays earliest.
            rows.sort(key=lambda p: (p["doi"], int(p["version"])))
            return rows, {"expected": expected, "received": len(rows), "date_field": "version_date", "contract": "all_versions_count_checked_v1"}
        if not batch: raise SourceFailure(server+"_pagination_ended_early")
        cursor += len(batch)


def collect(client, profile, source, lane, start, end, cache):
    if source == "pubmed": return pubmed(client, profile, lane, start, end, cache)
    if source == "epmc": return epmc(client, profile, lane, start, end, cache)
    if source == "arxiv": return arxiv(client, profile, start, end, cache)
    if source in ("biorxiv", "medrxiv"): return preprints(client, source, start, end, cache)
    raise SourceFailure("unsupported_source")
