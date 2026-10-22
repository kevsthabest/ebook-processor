"""Decision-client transport discipline (Decision, 2026-10-22).

The judge is a hosted service when it runs on OpenRouter
(typesafe/jev-1.13), and a rate limit there is the normal shape of a
busy minute. The client used to spend one generic `except` on every
failure, so three consecutive 429s incremented the same counter as
three dead-socket errors and disabled the judge for the rest of the
book: every later claim silently took its no-judgment path and the
preview recorded a quality downgrade as if it were a model verdict.

These tests pin the split:

  * a "try again" status (429, 408, 425, 5xx) is re-sent, honouring
    Retry-After (capped), up to _DECISION_MAX_ATTEMPTS;
  * a deterministic refusal (401 and friends) is never re-sent —
    paying twice for the same wrong key is waste — and never moves
    the breaker, because it says nothing about the model's health;
  * status failures trip the breaker only after a longer unbroken
    run than transport failures do;
  * transport errors keep their old three-strike contract;
  * the batched transport (quote poll, discovery) shares the policy.

Stdlib unittest only. No network, no Supabase, no real ebook files.
Run: python -m unittest discover -s tests -v
"""
import email.message
import importlib.util
import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_MOD_PATH = str(Path(__file__).resolve().parent.parent
                / "process-portable.py")
_spec = importlib.util.spec_from_file_location("process_portable", _MOD_PATH)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _ok(payload):
    class _Resp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def _err(code, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "http://judge.invalid/v1/systemone", code, "refused", hdrs,
        io.BytesIO(b""))


def _answers(p=0.91, key="trigger_check"):
    return {"answers": {key: {"type": "noul", "noul": p}}}


class _ClientCase(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        self.sleeps = []
        self.queue = []
        self._orig_sleep = pp.time.sleep

        def fake_sleep(seconds):
            self.sleeps.append(seconds)

        def fake_urlopen(req, timeout=None):
            self.calls += 1
            if not self.queue:
                raise AssertionError("unexpected extra request")
            step = self.queue.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step

        pp.time.sleep = fake_sleep
        self.addCleanup(setattr, pp.time, "sleep", self._orig_sleep)
        patcher = mock.patch.object(pp.urllib.request, "urlopen",
                                    fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dv = pp.DecisionValidator("http://judge.invalid/v1",
                                       model="jev-1.13")

    def ask(self):
        verdict, p = self.dv.validate("suicide", "a quote")
        return p


class TestRetryPolicy(_ClientCase):
    def test_429_is_retried_then_succeeds(self):
        self.queue = [_err(429), _ok(_answers())]
        p = self.ask()
        self.assertAlmostEqual(p, 0.91)
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(self.sleeps), 1)
        self.assertFalse(self.dv.disabled)

    def test_retry_after_is_honoured(self):
        self.queue = [_err(429, retry_after=0), _ok(_answers())]
        self.ask()
        self.assertEqual(self.sleeps, [0.0])

    def test_retry_after_is_capped(self):
        self.queue = [_err(503, retry_after=9999), _ok(_answers())]
        self.ask()
        self.assertEqual(self.sleeps, [pp._DECISION_RETRY_AFTER_CAP])

    def test_backoff_grows_between_attempts(self):
        self.queue = [_err(500), _err(500), _ok(_answers())]
        self.ask()
        self.assertEqual(len(self.sleeps), 2)
        self.assertLess(self.sleeps[0], self.sleeps[1])

    def test_deterministic_refusal_is_not_retried(self):
        self.queue = [_err(401)]
        self.assertIsNone(self.ask())
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.dv.disabled)
        self.assertEqual(self.dv._consec_status, 0)

    def test_status_failures_trip_only_after_the_longer_run(self):
        # One exhausted request = one status failure, however many
        # attempts it spent. Six of them disable; five do not.
        for _ in range(5):
            self.queue = [_err(429)] * pp._DECISION_MAX_ATTEMPTS
            self.assertIsNone(self.ask())
            self.assertFalse(self.dv.disabled)
        self.queue = [_err(429)] * pp._DECISION_MAX_ATTEMPTS
        self.assertIsNone(self.ask())
        self.assertTrue(self.dv.disabled)

    def test_transport_errors_keep_the_three_strike_contract(self):
        for _ in range(pp._DECISION_ERROR_TRIP):
            self.queue = [ConnectionError("refused")]
            self.assertIsNone(self.ask())
        self.assertTrue(self.dv.disabled)

    def test_success_resets_both_counters(self):
        self.queue = [ConnectionError("refused"), _ok(_answers())]
        self.assertIsNone(self.ask())
        self.assertAlmostEqual(self.ask(), 0.91)
        self.assertEqual(self.dv._consec_errors, 0)
        self.assertEqual(self.dv._consec_status, 0)


class TestBatchTransport(_ClientCase):
    def test_batch_retries_a_429(self):
        self.queue = [_err(429), _ok(_answers(key="q0"))]
        out = pp._ask_noul_batch(self.dv, {"q0": "is it?"}, "state")
        self.assertEqual(out, {"q0": 0.91})
        self.assertEqual(self.calls, 2)

    def test_batch_does_not_retry_a_401(self):
        self.queue = [_err(403)]
        self.assertIsNone(
            pp._ask_noul_batch(self.dv, {"q0": "is it?"}, "state"))
        self.assertEqual(self.calls, 1)
        self.assertFalse(self.dv.disabled)


if __name__ == "__main__":
    unittest.main()
