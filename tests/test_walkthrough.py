"""Step 4 acceptance: the scripted walkthrough matches SPEC 13 at every step."""
from datetime import timedelta

from scenarios.walkthrough import run
from tests.conftest import ADMIN_URL, APP_URL, ARCHIVER_URL


def test_walkthrough_passes_every_check_and_is_deterministic(test_database, tmp_path, capsys):
    outputs = []
    for i in range(2):
        out = tmp_path / f"walkthrough_{i}.txt"
        failures = run(admin_url=ADMIN_URL, app_url=APP_URL, archiver_url=ARCHIVER_URL,
                       threshold=timedelta(minutes=15), out_path=out)
        assert failures == []
        outputs.append(out.read_text())
    capsys.readouterr()  # keep pytest output short; the text is in the files
    text = outputs[0]
    assert outputs[0] == outputs[1]  # SPEC 17.31: reruns produce an identical file
    assert "v3 missed (due Wed 04 Nov 09:00)" in text
    assert "dispute DSP-1001: provisional_credit_issued" in text
    assert text.count("FAIL") == 0 and text.rstrip().endswith("RESULT: all checks passed")


def test_threshold_30_makes_the_wed_0915_check_fail(test_database, tmp_path, capsys):
    """The checks are real: with a 30-minute threshold the callback is not missed at 09:15,
    and the walkthrough says so instead of passing."""
    failures = run(admin_url=ADMIN_URL, app_url=APP_URL, archiver_url=ARCHIVER_URL,
                   threshold=timedelta(minutes=30), out_path=None)
    capsys.readouterr()
    assert any(f.startswith("Wed 09:15") for f in failures)
