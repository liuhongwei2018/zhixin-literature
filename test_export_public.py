import copy
import json
import unittest
from pathlib import Path
from export_public import TRACKS, canonical, clean_text, export, validate


def fixture():
    p = {"paper_id": "private-local-id", "pmid": "12345678", "source": "pubmed", "id": "12345678",
         "doi": "10.1234/paper", "title": "Public study title", "journal": "Public Journal",
         "abstract": "Private intermediate must never be exported", "abstract_excerpt": "not allowed",
         "published": "2026-10-19", "first_seen": "2026-10-20T00:00:00+00:00",
         "url": "https://unexpected.invalid/paper", "identity_status": "active", "retracted": False,
         "kind": "journal", "tracks": list(TRACKS), "field_sources": {"path": "/Users/private/runtime"},
         "source_versions": [{"unwanted": "/Users/private/cache"}],
         "ranking": {"eligible": True, "score": 80, "published": "2026-10-19", "method_cue": True,
                     "article_type": "方法 / 评测候选", "labels": ["GWAS"], "score_components": {"topic_relevance": 55},
                     "evidence": {"path": "/Users/private/log"}, "reasons": ["/Users/private/note"]}}
    report = {"as_of": "2026-10-20T12:00:00+00:00", "formal_hot_enabled": False, "push_enabled": False,
              "config": {"secret": "never"}, "jobs": [], "tracks": {}}
    for k in TRACKS:
        report["tracks"][k] = {"candidate_count": 4, "readable_count": 2, "reading_list": [copy.deepcopy(p)]}
        report["jobs"].append({"profile": k, "lane": "pubmed_new", "slot": "2026-10-20", "status": "complete",
                               "start": "2026-10-13", "through": "2026-10-19", "result_path": "/Users/private/logs"})
    return report


class ExportBoundaryTests(unittest.TestCase):
    def test_allowlist_excludes_runtime_abstracts_and_local_ids(self):
        out = export(fixture()); raw = json.dumps(out)
        for value in ("/Users/", "private-local-id", "Private intermediate", "field_sources", "source_versions", "result_path", '"config"', '"abstract"'):
            self.assertNotIn(value, raw)
        self.assertEqual(out["displayed_unique_papers"], 1)

    def test_same_publication_keeps_multiple_directions_without_duplicates(self):
        source = fixture()
        for t in source["tracks"].values(): t["reading_list"] *= 2
        out = export(source)
        self.assertEqual(len(out["papers"]), 1)
        self.assertTrue(all(len(t["reading"]) == 1 for t in out["tracks"].values()))

    def test_failure_never_becomes_zero_or_complete(self):
        source = fixture(); source["jobs"][-1]["status"] = "failed"
        source["all_due_sources_complete"] = True
        out = export(source)
        self.assertFalse(out["all_due_sources_complete"])
        self.assertEqual(out["coverage"]["complete"], 3)
        self.assertEqual(out["coverage"]["total"], 4)
        self.assertEqual(out["coverage"]["jobs"][-1]["status"], "failed")

    def test_no_source_jobs_is_unknown(self):
        source = fixture(); source["jobs"] = []
        self.assertFalse(export(source)["all_due_sources_complete"])

    def test_invalid_identity_and_retraction_are_not_published(self):
        source = fixture()
        source["tracks"]["CHIP_mCA"]["reading_list"][0]["retracted"] = True
        source["tracks"]["AI_medical_imaging"]["reading_list"][0]["identity_status"] = "identity_review"
        out = export(source)
        self.assertEqual(out["tracks"]["CHIP_mCA"]["reading"], [])
        self.assertEqual(out["tracks"]["AI_medical_imaging"]["reading"], [])

    def test_untrusted_source_link_is_rebuilt_from_public_id(self):
        out = export(fixture())
        self.assertEqual(out["papers"]["pmid:12345678"]["url"], "https://pubmed.ncbi.nlm.nih.gov/12345678/")
        self.assertEqual(canonical({"doi":"10.1234/a#fragment"})[1], "https://doi.org/10.1234/a%23fragment")
        with self.assertRaises(ValueError): canonical({"url":"javascript:alert(1)"})

    def test_sensitive_text_halts_export(self):
        source = fixture(); source["tracks"]["CHIP_mCA"]["reading_list"][0]["title"] = "/Users/private/study"
        with self.assertRaises(ValueError): export(source)

    def test_identity_title_conflict_halts_export(self):
        source = fixture(); source["tracks"]["CHIP_mCA"]["reading_list"][0]["title"] = "Conflicting study"
        with self.assertRaises(ValueError): export(source)

    def test_push_and_hot_must_stay_disabled(self):
        for flag in ("formal_hot_enabled", "push_enabled"):
            source = fixture(); source[flag] = True
            with self.assertRaises(ValueError): export(source)

    def test_nonfinite_scores_halt_export(self):
        source = fixture(); source["tracks"]["CHIP_mCA"]["reading_list"][0]["ranking"]["score"] = float("nan")
        with self.assertRaises(ValueError): export(source)

    def test_missing_direction_halts_export(self):
        source = fixture(); source["tracks"].pop("CHIP_mCA")
        with self.assertRaises(ValueError): export(source)

    def test_notes_keep_own_summary_without_other_fields(self):
        notes = {"papers":[{"track":"CHIP_mCA","pmid":"12345678","abstract_classification_checked":True,
                            "original_title":"Public title","short_title_zh":"中文导读","summary_zh":"自撰摘要",
                            "abstract":"Private copied text","private_path":"/Users/private/file"}]}
        out = export(fixture(),notes)
        self.assertEqual(out["highlights"][0]["summary_zh"], "自撰摘要")
        self.assertNotIn("Private copied text",json.dumps(out))
        self.assertFalse(out["highlights"][0]["full_text_quality_reviewed"])

    def test_deployable_data_is_valid_when_present(self):
        path = Path(__file__).resolve().parent / "readings.json"
        if not path.exists(): self.skipTest("no generated public snapshot")
        data = json.loads(path.read_text()); validate(data)
        self.assertEqual(set(data["tracks"]),set(TRACKS))
        self.assertGreater(data["displayed_unique_papers"],0)

    def test_display_cleanup_preserves_less_than_comparison(self):
        self.assertEqual(clean_text("Clinical &lt;sup&gt;177&lt;/sup&gt;Lu and &lt;i&gt;TET2&lt;/i&gt;"),"Clinical 177Lu and TET2")
        self.assertEqual(clean_text("patients aged < 75 years"),"patients aged < 75 years")

    def test_checked_semantic_exclusion_affects_only_selected_direction(self):
        rules = {"exclusions":[{"track":"CHIP_mCA","key":"pmid:12345678"}]}
        out = export(fixture(),overrides=rules)
        self.assertEqual(out["tracks"]["CHIP_mCA"]["reading"],[])
        self.assertEqual(len(out["tracks"]["AI_medical_imaging"]["reading"]),1)
        self.assertEqual(out["public_screening"]["excluded_reading_entries"],1)

    def test_explicit_version_relation_merges_display_without_title_matching(self):
        source = fixture(); t = source["tracks"]["CHIP_mCA"]; t["reading_list"] = []
        for version in (1,2):
            p = copy.deepcopy(fixture()["tracks"]["CHIP_mCA"]["reading_list"][0]);p.pop("pmid")
            p["doi"] = "10.21203/rs.3.rs-11268522/v"+str(version); p["title"] = "Title for version "+str(version)
            t["reading_list"].append(p)
        rules = {"version_groups":[{"canonical":"doi:10.21203/rs.3.rs-11268522/v2","aliases":["doi:10.21203/rs.3.rs-11268522/v1"]}]}
        out = export(source,overrides=rules)
        self.assertEqual(out["tracks"]["CHIP_mCA"]["reading"],["doi:10.21203/rs.3.rs-11268522/v2"])
        self.assertEqual(out["papers"]["doi:10.21203/rs.3.rs-11268522/v2"]["title"],"Title for version 2")

    def test_missing_checked_target_is_not_invented(self):
        rules = {"version_groups":[{"canonical":"pmid:99999999","aliases":["pmid:12345678"]}]}
        self.assertEqual(export(fixture(),overrides=rules)["displayed_unique_papers"],1)


if __name__ == "__main__":
    unittest.main()
