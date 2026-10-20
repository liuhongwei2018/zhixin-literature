"""Failure-boundary tests for source completeness, identity and HOT evidence."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
import catalogue
import collect_sources
import hot
import metadata
import retrieval
import sync
import topics

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
PROFILES = topics.profiles(Path(__file__).resolve().parent)


def paper(**extra):
    return {"source": "pubmed", "id": "12345", "pmid": "12345", "doi": "10.1000/test", "kind": "journal",
            "published": "2026-10-19", "title": "Clonal hematopoiesis and risk in patients",
            "abstract": "Clonal hematopoiesis was evaluated in patients with independent validation in a prospective cohort.", **extra}


def metric(value=10, hours=0, entity="MED:12345"):
    return {"source": "Europe PMC", "metric": "citedByCount", "entity": entity, "lineage": "epmc-citedByCount-v1",
            "status": "ok", "value": value, "observed_at": (NOW-timedelta(hours=hours)).isoformat()}


class HotEvidence(unittest.TestCase):
    def test_future_or_missing_validation_not_awarded(self):
        p=paper(title="MRI study",abstract="External validation is required. Future prospective multicenter studies are needed. No independent cohort was available.")
        features=hot.signals(p,{})
        for key in ("external_validation","prospective","multicenter"):self.assertFalse(features[key])

    def test_missing_counter_not_zero(self):
        a = hot.attention({}, NOW)
        self.assertIsNone(a["value"])
        self.assertIsNone(a["growth"]["delta"])

    def test_actual_elapsed_time_and_same_counter(self):
        latest = metric(15)
        g = hot.growth([metric(9, 18), metric(0, 12, "PPR:PPR123")], latest, NOW)
        self.assertEqual(g["delta"], 6)
        self.assertEqual(g["per_day"], 8)
        self.assertEqual(g["actual_hours"], 18)

    def test_no_growth_before_12h_or_counter_correction(self):
        self.assertIsNone(hot.growth([metric(5, 11.9)], metric(8), NOW)["delta"])
        self.assertEqual(hot.growth([metric(8, 12)], metric(5), NOW)["status"], "counter_correction")

    def test_stale_malformed_and_future_metrics(self):
        bad = metric(); bad["observed_at"] = "invalid"
        self.assertEqual(hot.attention({"metrics": [bad]}, NOW)["status"], "unavailable")
        self.assertEqual(hot.attention({"metrics": [metric(8, 37)]}, NOW)["status"], "stale")
        self.assertNotEqual(hot.attention({"metrics": [metric(8, -1)]}, NOW)["status"], "ok")

    def test_missing_rows_keep_base_position(self):
        def row(k, s, count):
            return {"key":k,"kind":"journal","age_days":10,"hot":{"score":s,"attention":{"status":"unavailable" if count is None else "ok","value":count}}}
        rows = hot.order([row("a",91.8,1),row("b",91.4,None),row("c",91.1,100)])
        self.assertEqual([r["key"] for r in rows], ["c","b","a"])

    def test_protocol_commentary_and_future_never_inflated(self):
        p = paper(); r = topics.classify(p,"CHIP_mCA",PROFILES["CHIP_mCA"],NOW)
        r["signals"] = hot.signals(p,r); r["topic_strength"] = 55
        p["key"] = "pmid:12345"
        regular = hot.score(p,r,NOW)
        protocol = hot.score(p,{**r,"article_type":"研究方案 / 设计"},NOW)
        self.assertLess(protocol["score"],regular["score"])
        self.assertFalse(hot.score({**p,"published":"2026-10-21"},r,NOW)["eligible"])
        self.assertFalse(hot.score({**p,"retracted":True},r,NOW)["eligible"])


class RetrievalIntegrity(unittest.TestCase):
    def test_pubmed_single_day_overflow_does_not_truncate(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(retrieval.IncompleteSearch) as error:
                retrieval.pubmed_range("x", "2026-10-19", "2026-10-19", date_field="crdt", scope="x", cache_dir=Path(temp),
                    request=lambda p:{"esearchresult":{"count":"5001","idlist":[]}},hard_max=100000)
            self.assertEqual(error.exception.details["reason"],"single_day_overflow")

    def test_epmc_duplicate_and_early_end_fail(self):
        for records in ([{"source":"MED","id":"1"},{"source":"MED","id":"1"}], [{"source":"MED","id":"1"}]):
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(retrieval.IncompleteSearch):
                    retrieval.epmc_range("x","2026-10-19","2026-10-19",date_field="FIRST_IDATE",scope="x",cache_dir=Path(temp),
                        request=lambda p:{"hitCount":2,"resultList":{"result":records}},hard_max=100000)

    def test_pubmed_fetch_checks_exact_ids_and_supports_books(self):
        xml='<PubmedArticleSet><PubmedBookArticle><BookDocument><PMID>12</PMID><ArticleTitle>Book</ArticleTitle><Book><PubDate><Year>2026</Year></PubDate></Book></BookDocument></PubmedBookArticle></PubmedArticleSet>'
        self.assertEqual(metadata.pubmed_rows(xml,["12"])[0]["kind"],"book")
        with self.assertRaises(metadata.SourceError):metadata.pubmed_rows(xml,["13"])

    def test_429_retry_after_is_persisted_and_not_retried(self):
        class Response:
            status_code=429; headers={"Retry-After":"720"}; content=b''
        class Session:
            count=0
            def get(self,*a,**k):self.count+=1;return Response()
        state={}; session=Session()
        c=collect_sources.Client(state,session=session,clock=lambda:10000,sleep=lambda x:None)
        for _ in range(2):
            with self.assertRaises(collect_sources.SourceFailure):c.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi")
        self.assertEqual(session.count,1)
        self.assertEqual(state["cooldowns"]["eutils.ncbi.nlm.nih.gov"],10720)

    def test_three_bounded_network_attempts(self):
        class Session:
            count=0
            def get(self,*a,**k):self.count+=1;raise requests.Timeout()
        s=Session();c=collect_sources.Client({},session=s,clock=lambda:10000,sleep=lambda x:None)
        with self.assertRaises(collect_sources.SourceFailure):c.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search")
        self.assertEqual(s.count,3)


class ClassificationAndIdentity(unittest.TestCase):
    def test_computational_single_cell_is_not_biology(self):
        p=paper(title="A method for a single cell-average sequence",abstract="We propose a high-order numerical mesh method and a sequence of approximations.")
        self.assertFalse(topics.classify(p,"omics_methods_applications",PROFILES["omics_methods_applications"],NOW)["eligible"])

    def test_clinical_data_needs_no_epidemiology_title(self):
        p=paper(title="Treatment and mortality in routine care",abstract="Electronic health records were used to estimate treatment outcomes and mortality among patients.")
        self.assertTrue(topics.classify(p,"clinical_observational",PROFILES["clinical_observational"],NOW)["eligible"])

    def test_identifier_merge_and_journal_preprint_link(self):
        cat=catalogue.Catalogue()
        p=paper();r=topics.classify(p,"CHIP_mCA",PROFILES["CHIP_mCA"],NOW)
        cat.update(p,{"CHIP_mCA":r},NOW.isoformat(),"r1")
        pre=paper(source="biorxiv",id="10.1101/abc",pmid=None,doi="10.1101/abc",published_doi="10.1000/test",kind="preprint")
        cat.update(pre,{"CHIP_mCA":r},NOW.isoformat(),"r1")
        cat.update(p,{"CHIP_mCA":r},NOW.isoformat(),"r2")
        self.assertEqual(len(cat.data["papers"]),1)
        self.assertFalse(cat.data["papers"]["pmid:12345"]["identity_review"])

    def test_different_pmid_does_not_merge_on_doi(self):
        cat=catalogue.Catalogue();p=paper();r=topics.classify(p,"CHIP_mCA",PROFILES["CHIP_mCA"],NOW)
        cat.update(p,{"CHIP_mCA":r},NOW.isoformat(),"r1")
        self.assertIsNone(cat.update({**p,"pmid":"99999"},{"CHIP_mCA":r},NOW.isoformat(),"r1"))
        self.assertEqual(cat.data["identity_conflicts"],1)
        self.assertTrue(cat.data["papers"]["pmid:12345"]["identity_review"])

    def test_failed_ingest_is_atomic(self):
        cat=catalogue.Catalogue();before=copy.deepcopy(cat.data)
        with self.assertRaises(ValueError):sync.ingest(cat,[paper(),paper(source="invalid")],"CHIP_mCA",PROFILES,NOW,"r1")
        self.assertEqual(cat.data,before)

    def test_failed_old_day_remains_due_after_watermark(self):
        state=sync.initial_state(NOW)
        state["baseline_start"]="2026-09-01"
        lane={"closed_through":"2026-10-19","days":{"2026-09-01":{"status":"failed"}}}
        planned=sync.daily_plan(state,lane,NOW)
        self.assertIn("2026-09-01",planned)
        self.assertIn("2026-10-20",planned)
        self.assertEqual(planned[0],"2026-10-19")

    def test_private_payload_rejected(self):
        with self.assertRaises(ValueError):catalogue.validate_public({"abstract":"private content"})
        with self.assertRaises(ValueError):catalogue.validate_public({"path":"/Users/example/private"})


if __name__ == "__main__":unittest.main()
