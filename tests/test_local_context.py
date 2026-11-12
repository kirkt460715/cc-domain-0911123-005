import unittest
from examples.context import load, run

class ContextTests(unittest.TestCase):
    def test_product_accepts_local_context(self):
        result = run()
        self.assertTrue(result['ok'])
        self.assertGreater(result['records'], 0)
