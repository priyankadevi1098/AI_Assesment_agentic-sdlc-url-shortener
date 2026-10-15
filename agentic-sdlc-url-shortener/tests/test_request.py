"""Tests for the request contract (request.py)."""
import json
import tempfile
import unittest
from pathlib import Path

from sdlc_agent.request import RequestValidationError, load_request, parse_request


class RequestTests(unittest.TestCase):
    def _valid_document(self) -> dict:
        return {
            "name": "sample",
            "mode": "greenfield",
            "requirement": "Build a thing.",
            "acceptance_criteria": ["It does the thing."],
        }

    def test_valid_request_parses(self):
        request = parse_request(self._valid_document())
        self.assertEqual(request.name, "sample")
        self.assertEqual(request.mode, "greenfield")
        self.assertEqual(request.acceptance_criteria, ("It does the thing.",))

    def test_invalid_request_rejected(self):
        document = self._valid_document()
        del document["requirement"]
        with self.assertRaises(RequestValidationError) as ctx:
            parse_request(document)
        self.assertEqual(ctx.exception.field, "requirement")

    def test_invalid_mode_rejected(self):
        document = self._valid_document()
        document["mode"] = "sidefield"
        with self.assertRaises(RequestValidationError) as ctx:
            parse_request(document)
        self.assertEqual(ctx.exception.field, "mode")

    def test_empty_acceptance_criteria_rejected(self):
        document = self._valid_document()
        document["acceptance_criteria"] = []
        with self.assertRaises(RequestValidationError) as ctx:
            parse_request(document)
        self.assertEqual(ctx.exception.field, "acceptance_criteria")

    def test_non_dict_document_rejected(self):
        with self.assertRaises(RequestValidationError):
            parse_request(["not", "a", "dict"])

    def test_load_request_rejects_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "request.json"
            path.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(RequestValidationError) as ctx:
                load_request(path)
            self.assertEqual(ctx.exception.field, "document")

    def test_load_request_reads_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "request.json"
            path.write_text(json.dumps(self._valid_document()), encoding="utf-8")
            request = load_request(path)
            self.assertEqual(request.name, "sample")


if __name__ == "__main__":
    unittest.main()
