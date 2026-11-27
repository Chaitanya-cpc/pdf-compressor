import os
import threading
import unittest
from tempfile import TemporaryDirectory

from loadgate import borrow, compressor_workers, decide, snapshot


class DecideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["ERUDORE_MAX_BROWSERS"] = "2"
        os.environ["ERUDORE_RESERVE_MB"] = "1800"
        os.environ["ERUDORE_BROWSER_MB"] = "1600"
        os.environ["ERUDORE_COMPRESSOR_MAX"] = "2"
        import loadgate
        loadgate.MAX_BROWSERS = 2
        loadgate.RESERVE_MB = 1800
        loadgate.BROWSER_MB = 1600
        loadgate.COMPRESSOR_MAX = 2

    def test_a_free_server_can_take_two_browsers(self):
        self.assertEqual(decide(0, 8000, want=2, minimum=1, others_waiting=False, older_waiting=False), 2)

    def test_a_second_desk_keeps_the_extra_browser_for_itself(self):
        self.assertEqual(decide(0, 8000, want=2, minimum=1, others_waiting=True, older_waiting=False), 1)

    def test_an_older_desk_goes_first(self):
        self.assertEqual(decide(0, 8000, want=1, minimum=1, others_waiting=True, older_waiting=True), 0)

    def test_two_browsers_already_up_means_wait(self):
        self.assertEqual(decide(2, 8000, want=1, minimum=1, others_waiting=False, older_waiting=False), 0)

    def test_low_memory_means_wait_even_if_a_slot_is_free(self):
        self.assertEqual(decide(1, 2000, want=1, minimum=1, others_waiting=False, older_waiting=False), 0)


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["ERUDORE_MAX_BROWSERS"] = "2"
        os.environ["ERUDORE_RESERVE_MB"] = "1800"
        os.environ["ERUDORE_BROWSER_MB"] = "1600"
        os.environ["ERUDORE_COMPRESSOR_MAX"] = "2"
        import loadgate
        loadgate.MAX_BROWSERS = 2
        loadgate.RESERVE_MB = 1800
        loadgate.BROWSER_MB = 1600
        loadgate.COMPRESSOR_MAX = 2

    def test_a_busy_desk_lets_the_next_one_start_with_one_browser(self):
        with TemporaryDirectory() as folder:
            os.environ["ERUDORE_LOAD_DIR"] = folder
            os.environ["ERUDORE_LOAD_POLL"] = "0.05"
            started = threading.Event()
            release = threading.Event()

            def hold_one():
                with borrow("watchout"):
                    started.set()
                    release.wait(2)

            thread = threading.Thread(target=hold_one)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertEqual(snapshot()["browsers"], 1)
            with borrow("income-tax", want=2) as granted:
                self.assertEqual(granted, 1)
                self.assertEqual(snapshot()["browsers"], 2)
            release.set()
            thread.join(2)
            self.assertEqual(snapshot()["browsers"], 0)

    def test_the_compressor_uses_one_worker_while_a_browser_is_up(self):
        with TemporaryDirectory() as folder:
            os.environ["ERUDORE_LOAD_DIR"] = folder
            with borrow("gst"):
                self.assertEqual(compressor_workers(3), 1)
            self.assertEqual(compressor_workers(3), 2)


if __name__ == "__main__":
    unittest.main()
