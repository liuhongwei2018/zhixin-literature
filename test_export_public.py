"""Public export contract check (the retrieval/HOT suite is test_cloud.py)."""
import unittest
from sync import ROOT,validate

class PublicExport(unittest.TestCase):
    def test_current_public_data(self):
        doc=validate(ROOT)
        self.assertTrue(doc["formal_hot_enabled"])
        self.assertFalse(doc["push_enabled"])

if __name__=="__main__":unittest.main()
