"""Step 0 sanity test.

Why it exists: pytest exits with code 5 when it collects zero tests, so an
empty suite would fail the Step 0 acceptance check. This test also guards a
real risk: a setting used in code but missing from .env.example.
"""
from pathlib import Path

REQUIRED_KEYS = {
    "DATABASE_URL_ADMIN",
    "DATABASE_URL_APP",
    "ANTHROPIC_API_KEY",
    "MODEL_ID",
    "EXTRACTION_MODEL_ID",
    "LLM_MODE",
    "MISSED_THRESHOLD_MINUTES",
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
}


def test_env_example_lists_every_setting():
    path = Path(__file__).resolve().parent.parent / ".env.example"
    keys = {
        line.split("=", 1)[0].strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert REQUIRED_KEYS <= keys, f"missing: {REQUIRED_KEYS - keys}"
