"""Offline tests of the request checks (no network, no law binary)."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import intake  # noqa: E402

SHA = "a" * 64
REQUEST = {
    "schema": "arxo.registry-request/1",
    "source": {"repository": "acme/parcel-tariff", "release": "parcel-tariff-0.1.0", "commit": "b" * 40},
    "license": "Apache-2.0",
    "packages": [
        {"name": "acme.parcel_tariff", "version": "0.1.0", "path": p,
         "file": f"acme.parcel_tariff-0.1.0.{p}", "bytes": 10, "sha256": SHA}
        for p in ("release.json", "package.lawir.json", "world.lawir.json", "law.lock")
    ],
}


def variant(**changes):
    request = copy.deepcopy(REQUEST)
    for key, value in changes.items():
        request[key] = value
    return request


class RequestTests(unittest.TestCase):
    def test_valid_request(self):
        rows = intake.validate_request(REQUEST, "r")
        self.assertEqual(intake.artifacts_of(rows), [("acme.parcel_tariff", "0.1.0")])
        self.assertEqual(intake.check_request_location("requests/acme.parcel_tariff-0.1.0/request.json", rows),
                         ("acme.parcel_tariff", "0.1.0"))

    def test_license_is_required(self):
        request = copy.deepcopy(REQUEST)
        del request["license"]
        with self.assertRaisesRegex(intake.IntakeError, "license"):
            intake.validate_request(request, "r")

    def test_dotdot_path_refused(self):
        request = copy.deepcopy(REQUEST)
        request["packages"][1]["path"] = "../escape.json"
        with self.assertRaisesRegex(intake.IntakeError, "escape"):
            intake.validate_request(request, "r")

    def test_file_with_separator_refused(self):
        request = copy.deepcopy(REQUEST)
        request["packages"][1]["file"] = "dir/x.json"
        with self.assertRaises(intake.IntakeError):
            intake.validate_request(request, "r")

    def test_oversized_file_refused(self):
        request = copy.deepcopy(REQUEST)
        request["packages"][1]["bytes"] = intake.MAX_FILE_BYTES + 1
        with self.assertRaisesRegex(intake.IntakeError, "предел|limit"):
            intake.validate_request(request, "r")

    def test_missing_release_json_refused(self):
        request = copy.deepcopy(REQUEST)
        request["packages"] = request["packages"][1:]
        with self.assertRaisesRegex(intake.IntakeError, "release.json"):
            intake.validate_request(request, "r")

    def test_wrong_directory_refused(self):
        rows = intake.validate_request(REQUEST, "r")
        with self.assertRaises(intake.IntakeError):
            intake.check_request_location("requests/acme.other-0.1.0/request.json", rows)
        with self.assertRaises(intake.IntakeError):
            intake.check_request_location("requests/x/../acme.parcel_tariff-0.1.0/request.json", rows)

    def test_reserved_prefix_refused(self):
        request = copy.deepcopy(REQUEST)
        for row in request["packages"]:
            row["name"] = "kz.parcel"
            row["file"] = row["file"].replace("acme.parcel_tariff", "kz.parcel")
        rows = intake.validate_request(request, "r")
        with self.assertRaisesRegex(intake.IntakeError, "reserved"):
            intake.check_prefixes(rows, "acme/parcel-tariff")

    def test_prefix_claim_and_second_owner(self):
        rows = intake.validate_request(REQUEST, "r")
        self.assertEqual(intake.check_prefixes(rows, "acme/parcel-tariff"), ["acme"])
        original = intake.load_prefixes
        intake.load_prefixes = lambda: (set(), {"acme": "acme"})
        try:
            self.assertEqual(intake.check_prefixes(rows, "ACME/other-repo"), [])
            with self.assertRaisesRegex(intake.IntakeError, "belongs to acme"):
                intake.check_prefixes(rows, "mallory/parcel-tariff")
        finally:
            intake.load_prefixes = original

    def test_reserved_list_covers_language_families(self):
        reserved, _ = intake.load_prefixes()
        self.assertTrue({"law", "calc", "std", "kz", "fr", "de", "ru"} <= reserved)


if __name__ == "__main__":
    unittest.main()
