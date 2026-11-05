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
            lines = [json.loads(line) for line in (data / "run-log.jsonl").read_text().splitlines()]
        self.assertEqual([line["event"] for line in lines], ["started", "finished"])
        self.assertEqual(lines[0]["app"], "gst")
        self.assertNotIn("password", json.dumps(lines))
        self.assertEqual(lines[1]["items"][0]["reference"], "ASSAM")
        self.assertEqual(lines[1]["status"], "done")


if __name__ == "__main__":
    unittest.main()
