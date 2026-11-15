"""Tests for scripts/explain.py: data gathering, prompt building and failure handling. Gemini is mocked: no API calls.

    python tests/test_explain.py        (or: python -m unittest tests/test_explain.py)
"""
import io, json, os, shutil, sys, tempfile, unittest
from unittest import mock

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import explain as ex  # noqa: E402


def jdump(obj, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def jload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

DATES = ["2026-06-01", "2026-06-13", "2026-06-25", "2026-07-07", "2026-07-19", "2026-07-31",
         "2026-08-12", "2026-08-24", "2026-09-05", "2026-09-17"]
EVENT = "2026-08-26"


def make_dataset(root, name="data_test", dates=DATES, with_drivers=True, missing=(), label_extra=False):
    """A tiny viewer dataset: 64x48 PNGs in the viewer's dB encoding, a manifest and optionally drivers.json."""
    d = os.path.join(root, name)
    os.makedirs(d)
    rng = np.random.default_rng(0)
    frames = []
    for i, date in enumerate(dates):
        fn = f"s1_{date}_hh.png"
        if date not in missing:
            a = rng.integers(134, 147, (48, 64)).astype("uint8")   # about +-1 dB of speckle
            if date > EVENT:
                a[10:20, 10:30] = 230          # a bright "deposit" after the event
            a[:, :3] = 0                       # a no-data strip
            Image.fromarray(a, "L").save(os.path.join(d, fn))
        frames.append({"date": date, "sensor": "Sentinel-1 C-band", "pols": ["VV", "VH"], "hh": fn})
    frames.append({"date": "2026-06-01", "sensor": "NISAR L-band", "pols": ["HH", "HV"], "hh": "n_x.png"})
    if label_extra:
        frames.append({"date": "2026-08-24", "sensor": "Sentinel-1 C-band", "label": "Avg", "hh": frames[0]["hh"]})
    jdump({"title": "Test valley", "subtitle": "unit test", "bbox": [85, 28, 85.1, 28.1], "width": 64,
               "height": 48, "pixel_size_m": 20, "db_range": [-30, 5],
               "event": {"date": EVENT, "label": "Test avalanche"}, "frames": frames},
              os.path.join(d, "manifest.json"))
    if with_drivers:
        jdump({"site": None, "event": EVENT, "kind": "test", "baseline": "1980-2025", "summary": "Heat stood out.",
                   "factors": [{"key": "t", "label": "Air temperature", "value": 1.0, "unit": "°C", "normal": 0.0,
                                "percentile": 98.0, "verdict": "exceptional", "risky_percentile": 98.0,
                                "confidence": "good", "why": "melt", "years": "x", "sd": 1.2}],
                   "earthquakes": {"count": 0, "verdict": "normal"}},
                  os.path.join(d, "drivers.json"))
    return d


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = mock.Mock(prompt_token_count=1234, candidates_token_count=321)


class FakeClient:
    """Stands in for google.genai.Client: records calls, returns or raises what it is given."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.models = self

    def generate_content(self, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return FakeResponse(r)


class ApiError(Exception):
    def __init__(self, code, msg="boom"):
        super().__init__(msg)
        self.code = code


GOOD_REPLY = json.dumps({
    "headline": "A slope collapsed and left a bright scar.",
    "what_is_happening": "Between 24 Aug and 5 Sep a patch got much brighter.",
    "visible_changes": [{"between": "2026-08-24 - 2026-09-05", "change": "bright patch appears"}],
    "key_patterns": ["heat was exceptional"],
    "prediction": {"evidence_sufficient": True, "outlook": "The scar will stay.", "time_horizon": "1-3 months",
                   "confidence": "medium", "reasons": ["the change persisted"], "what_would_change_this": "new slides"},
    "caveats": ["one year of radar data"]})


class TestGathering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sample_frames_keeps_event_neighbours_and_limit(self):
        frames = [{"date": d} for d in DATES]
        got = [f["date"] for f in ex.sample_frames(frames, EVENT, n=5)]
        self.assertEqual(len(got), 5)
        self.assertEqual(got, sorted(got))
        self.assertIn("2026-08-24", got)          # last pass before the event
        self.assertIn("2026-09-05", got)          # first pass after it
        self.assertIn(DATES[0], got)
        self.assertIn(DATES[-1], got)

    def test_sample_frames_skips_averaged_passes(self):
        frames = [{"date": d} for d in DATES[:3]] + [{"date": "2026-06-20", "label": "Avg"}]
        got = ex.sample_frames(frames, None, n=6)
        self.assertTrue(all(not f.get("label") for f in got))
        self.assertEqual(len(got), 3)

    def test_pick_sensor_prefers_longest_record(self):
        frames = [{"sensor": "A"}] * 3 + [{"sensor": "B"}] * 5
        self.assertEqual(ex.pick_sensor(frames), "B")
        self.assertIsNone(ex.pick_sensor([]))

    def test_frame_jpeg_downscales_and_handles_missing(self):
        p = os.path.join(self.tmp, "big.png")
        Image.fromarray(np.full((1200, 900), 150, "uint8"), "L").save(p)
        jpg = ex.frame_jpeg(p, max_px=512)
        im = Image.open(io.BytesIO(jpg))
        self.assertEqual(im.format, "JPEG")
        self.assertLessEqual(max(im.size), 512)
        self.assertIsNone(ex.frame_jpeg(os.path.join(self.tmp, "nope.png")))
        empty = os.path.join(self.tmp, "empty.png")
        Image.fromarray(np.zeros((10, 10), "uint8"), "L").save(empty)
        self.assertIsNone(ex.frame_jpeg(empty))   # all no-data

    def test_pass_statistics_detects_post_event_change(self):
        d = make_dataset(self.tmp)
        meta = jload(os.path.join(d, "manifest.json"))
        s1 = [f for f in meta["frames"] if f["sensor"].startswith("Sentinel")]
        with mock.patch.object(ex, "STATS_STEP", 1):        # tiny test image: use every pixel
            rows = ex.pass_statistics(d, s1, meta)
        self.assertEqual(len(rows), len(DATES))
        by = {r["date"]: r for r in rows}
        self.assertNotIn("brighter_km2_vs_previous", by[DATES[0]])          # nothing to compare with
        # the planted deposit stands well above the random speckle between two ordinary passes
        self.assertGreater(by["2026-09-05"]["brighter_km2_vs_previous"], 3 * by["2026-08-12"]["brighter_km2_vs_previous"])

    def test_gather_collects_inputs_and_reports_gaps(self):
        make_dataset(self.tmp, missing=("2026-09-05",))
        ctx = ex.gather("data_test", viewer_dir=self.tmp)
        self.assertEqual(ctx["sensor"], "Sentinel-1 C-band")
        self.assertEqual(ctx["n_passes"], len(DATES))
        self.assertTrue(ctx["drivers"])
        self.assertTrue(any("2026-09-05" in p for p in ctx["problems"]))      # missing image reported, not fatal
        self.assertNotIn("2026-09-05", [i["date"] for i in ctx["images"]])
        self.assertLessEqual(len(ctx["images"]), ex.MAX_FRAMES)

    def test_gather_without_drivers_and_unknown_dataset(self):
        make_dataset(self.tmp, with_drivers=False)
        ctx = ex.gather("data_test", viewer_dir=self.tmp)
        self.assertIsNone(ctx["drivers"])
        self.assertTrue(any("drivers.json" in p for p in ctx["problems"]))
        with self.assertRaises(FileNotFoundError):
            ex.gather("does_not_exist", viewer_dir=self.tmp)


class TestPrompt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        make_dataset(self.tmp)
        self.ctx = ex.gather("data_test", viewer_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prompt_asks_for_both_tasks_in_plain_language(self):
        p = ex.build_prompt(self.ctx)
        for phrase in ("plain, everyday language", "EXPLAIN", "PREDICT", "confidence", "evidence_sufficient",
                       "Do not invent", "JSON"):
            self.assertIn(phrase, p)

    def test_prompt_carries_the_data(self):
        p = ex.build_prompt(self.ctx)
        data = json.loads(p.split("DATA (JSON):\n", 1)[1])
        self.assertEqual(data["site"]["event_date"], EVENT)
        self.assertEqual(data["time_lapse"]["images_attached_in_order"], [i["date"] for i in self.ctx["images"]])
        self.assertEqual(data["eda_possible_drivers"]["summary"], "Heat stood out.")
        self.assertNotIn("sd", data["eda_possible_drivers"]["factors"][0])   # trimmed
        self.assertEqual(len(data["per_pass_radar_statistics"]["passes"]), len(DATES))
        self.assertLess(len(p), 40000)                                       # stays small

    def test_contents_interleave_captions_and_images_in_date_order(self):
        parts = ex.build_contents(self.ctx)
        captions = [x for x in parts[:-1] if isinstance(x, str)]
        self.assertEqual(len(captions), len(self.ctx["images"]))
        dates = [c.split("pass of ")[1].split(" ")[0] for c in captions]
        self.assertEqual(dates, sorted(dates))
        self.assertIn("PREDICT", parts[-1])


class TestCallAndFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        make_dataset(self.tmp)
        self.cfg = {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model"}
        self.sleep = mock.patch.object(ex.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_explain(self, client, cfg=None):
        return ex.explain("data_test", cfg or self.cfg, client=client, viewer_dir=self.tmp)

    def test_success_writes_result(self):
        client = FakeClient([GOOD_REPLY])
        out = self.run_explain(client)
        self.assertEqual(out["source"], "gemini")
        self.assertEqual(out["model"], "test-model")
        self.assertEqual(out["result"]["prediction"]["confidence"], "medium")
        self.assertEqual(out["usage"]["prompt_tokens"], 1234)
        self.assertEqual(client.calls[0]["model"], "test-model")
        self.assertEqual(client.calls[0]["config"]["response_mime_type"], "application/json")
        saved = jload(os.path.join(self.tmp, "data_test", "explain.json"))
        self.assertEqual(saved["result"]["headline"], "A slope collapsed and left a bright scar.")

    def test_rate_limit_is_retried_then_succeeds(self):
        client = FakeClient([ApiError(429), GOOD_REPLY])
        out = self.run_explain(client)
        self.assertEqual(out["source"], "gemini")
        self.assertEqual(len(client.calls), 2)

    def test_persistent_errors_fall_back_without_crashing(self):
        client = FakeClient([ApiError(503), ApiError(503), ApiError(503)])
        out = self.run_explain(client)
        self.assertEqual(out["source"], "fallback")
        self.assertIn("503", out["error"])
        self.assertFalse(out["result"]["prediction"]["evidence_sufficient"])
        self.assertIn("Heat stood out.", out["result"]["what_is_happening"])

    def test_bad_key_is_not_retried(self):
        client = FakeClient([ApiError(401)])
        out = self.run_explain(client)
        self.assertEqual(out["source"], "fallback")
        self.assertIn("rejected", out["error"])
        self.assertEqual(len(client.calls), 1)

    def test_non_json_reply_falls_back(self):
        out = self.run_explain(FakeClient(["Sorry, here is some prose."]))
        self.assertEqual(out["source"], "fallback")
        self.assertIn("JSON", out["error"])

    def test_fenced_json_reply_is_accepted(self):
        out = self.run_explain(FakeClient(["```json\n" + GOOD_REPLY + "\n```"]))
        self.assertEqual(out["source"], "gemini")

    def test_missing_key_uses_fallback_and_makes_no_call(self):
        with mock.patch.object(ex, "call_gemini") as call:
            out = ex.explain("data_test", {"GEMINI_MODEL": "m"}, viewer_dir=self.tmp)
        call.assert_not_called()
        self.assertEqual(out["source"], "fallback")
        self.assertIn("GEMINI_API_KEY", out["error"])

    def test_dataset_with_no_passes_falls_back(self):
        d = os.path.join(self.tmp, "data_empty")
        os.makedirs(d)
        jdump({"title": "Empty", "frames": []}, os.path.join(d, "manifest.json"))
        client = FakeClient([GOOD_REPLY])
        out = ex.explain("data_empty", self.cfg, client=client, viewer_dir=self.tmp)
        self.assertEqual(out["source"], "fallback")
        self.assertEqual(client.calls, [])

    def test_config_env_overrides_file(self):
        p = os.path.join(self.tmp, "g.env")
        open(p, "w").write("GEMINI_API_KEY=from-file\n# comment\nGEMINI_MODEL=file-model\n")
        with mock.patch.dict(os.environ, {"GEMINI_MODEL": "env-model"}, clear=False):
            cfg = ex.load_config(p)
        self.assertEqual(cfg["GEMINI_API_KEY"], "from-file")
        self.assertEqual(cfg["GEMINI_MODEL"], "env-model")
        self.assertEqual(ex.load_config(os.path.join(self.tmp, "none.env")).get("GEMINI_MODEL"),
                         os.environ.get("GEMINI_MODEL", ex.DEFAULT_MODEL))


if __name__ == "__main__":
    unittest.main(verbosity=2)
