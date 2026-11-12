import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from runlog import note_status


class RunLogTests(unittest.TestCase):
    def test_a_run_is_logged_when_it_starts_and_when_it_finishes(self):
        with TemporaryDirectory() as folder:
            data = Path(folder)
            shared = data / "shared.jsonl"
            os.environ["ERUDORE_RUN_LOG"] = str(shared)
            try:
                note_status(
                    "gst",
                    data,
                    "abc",
                    None,
                    {
                        "status": "queued",
                        "employee_id": "A0003",
                        "message": "Queued 2 login(s).",
                        "password": "secret",
                        "rows": [{"reference": "ASSAM", "status": "queued", "password": "nope"}],
                    },
                )
                note_status("gst", data, "abc", "queued", {"status": "running", "message": "Working"})
                note_status(
                    "gst",
                    data,
                    "abc",
                    "running",
                    {
                        "status": "done",
                        "employee_id": "A0003",
                        "message": "Finished.",
                        "rows": [{"reference": "ASSAM", "status": "ok", "message": "Saved 3 pages."}],
                    },
                )
            finally:
                os.environ.pop("ERUDORE_RUN_LOG", None)
            lines = [json.loads(line) for line in shared.read_text().splitlines()]
        self.assertEqual([line["event"] for line in lines], ["started", "finished"])
        self.assertEqual(lines[0]["app"], "gst")
        self.assertNotIn("password", json.dumps(lines))
        self.assertEqual(lines[1]["items"][0]["reference"], "ASSAM")
        self.assertEqual(lines[1]["status"], "done")
        self.assertFalse((data / "run-log.jsonl").exists())

    def test_a_finished_run_keeps_the_compressed_files(self):
        with TemporaryDirectory() as folder:
            data = Path(folder)
            job = data / "jobs" / "abc"
            job.mkdir(parents=True)
            pdf = job / "000_compressed.pdf"
            archive = job / "compressed.zip"
            pdf.write_bytes(b"%PDF")
            archive.write_bytes(b"PK")
            os.environ["ERUDORE_RUN_LOG"] = str(data / "log.jsonl")
            try:
                note_status("plugin", data, "abc", "running", {"status": "done", "message": "Compressed 1 file."})
            finally:
                os.environ.pop("ERUDORE_RUN_LOG", None)
            self.assertEqual(pdf.read_bytes(), b"%PDF")
            self.assertEqual(archive.read_bytes(), b"PK")


if __name__ == "__main__":
    unittest.main()
