"""Reading the delivered sample's Authentication-Results over IMAP (faked)."""

import unittest

from core.sample_proof import describe_verdict, fetch_auth_verdict

HEADER = (b"Authentication-Results: mx.google.com;\r\n       dkim=pass header.i=@brightline.example;\r\n"
          b"       spf=pass smtp.mailfrom=jamie@brightline.example;\r\n       dmarc=pass header.from=brightline.example\r\n\r\n")


class FakeIMAP:
    def __init__(self, found_in=None, header=HEADER, missing_folders=()):
        self.found_in, self.header, self.missing = found_in, header, set(missing_folders)
        self.selected = None
        self.readonly = []

    def select(self, folder, readonly=False):
        folder = folder.strip('"')          # real servers get quoted names
        self.readonly.append(readonly)
        if folder in self.missing:
            return "NO", [b"no such folder"]
        self.selected = folder
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [b"7" if self.selected == self.found_in else b""]

    def fetch(self, num, query):
        return "OK", [(b"7 (BODY[HEADER.FIELDS (AUTHENTICATION-RESULTS)] {120}", self.header), b")"]

    def logout(self):
        return "OK", []


def run(fake, attempts=2):
    naps = []
    result = fetch_auth_verdict("<abc@brightline.example>", connect=lambda: fake, attempts=attempts, wait=1,
                                sleep=naps.append)
    return result, naps


class TestSampleProof(unittest.TestCase):
    def test_found_in_inbox(self):
        result, _ = run(FakeIMAP(found_in="INBOX"))
        self.assertEqual((result["spf"], result["dkim"], result["dmarc"], result["folder"]), ("pass", "pass", "pass", "INBOX"))
        self.assertIn("passed", describe_verdict(result))

    def test_landed_in_spam(self):
        result, _ = run(FakeIMAP(found_in="Junk", missing_folders=("[Gmail]/Spam",)))
        self.assertEqual(result["folder"], "Junk")
        self.assertTrue(result["spam"])
        self.assertIn("spam", describe_verdict(result).lower())

    def test_not_arrived_yet_retries_then_gives_up(self):
        result, naps = run(FakeIMAP(found_in=None), attempts=3)
        self.assertIsNone(result)
        self.assertEqual(len(naps), 2)

    def test_internal_mail_without_a_verdict(self):
        result, _ = run(FakeIMAP(found_in="INBOX", header=b"\r\n"))
        self.assertEqual(result, {"folder": "INBOX", "spam": False})
        self.assertIn("Gmail", describe_verdict(result))

    def test_inbox_opened_read_only(self):
        fake = FakeIMAP(found_in="INBOX")
        run(fake)
        self.assertTrue(all(fake.readonly))

    def test_connection_failure_is_quiet(self):
        def broken():
            raise OSError("no network")
        self.assertIsNone(fetch_auth_verdict("<x@y>", connect=broken, attempts=1, wait=0, sleep=lambda s: None))

    def test_failures_are_explained(self):
        text = describe_verdict({"spf": "softfail", "dkim": "none", "dmarc": "fail", "folder": "INBOX", "spam": False})
        self.assertIn("DKIM", text)
        self.assertIn("SPF", text)


if __name__ == "__main__":
    unittest.main()
