"""Tests for the llm_chat provider dispatcher (OpenAI vs Perplexity routing)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("httpx")
import podcast  # noqa: E402


def _spies(monkeypatch):
    calls = {}

    def oai(p, **k):
        calls["openai"] = True
        return "OAI"

    def pplx(p, **k):
        calls["pplx"] = True
        return "PPLX"

    monkeypatch.setattr(podcast, "openai_chat", oai)
    monkeypatch.setattr(podcast, "pplx_chat", pplx)
    return calls


def test_llm_chat_routes_openai_models(monkeypatch):
    calls = _spies(monkeypatch)
    for m in ("gpt-4o", "gpt-4o-mini", "o3-mini", "o4-mini"):
        calls.clear()
        assert podcast.llm_chat("hi", model=m) == "OAI"
        assert "openai" in calls and "pplx" not in calls


def test_llm_chat_routes_sonar_to_perplexity(monkeypatch):
    calls = _spies(monkeypatch)
    for m in ("sonar", "sonar-pro"):
        calls.clear()
        assert podcast.llm_chat("hi", model=m) == "PPLX"
        assert "pplx" in calls and "openai" not in calls


def test_llm_chat_forwards_all_kwargs(monkeypatch):
    seen = {}

    def fake_openai(prompt, *, model, max_tokens, system, temperature, retries):
        seen.update(model=model, max_tokens=max_tokens, system=system,
                    temperature=temperature, retries=retries, prompt=prompt)
        return "ok"

    monkeypatch.setattr(podcast, "openai_chat", fake_openai)
    podcast.llm_chat("PROMPT", model="gpt-4o", max_tokens=1234,
                     system="SYS", temperature=0.9, retries=2)
    assert seen == {"prompt": "PROMPT", "model": "gpt-4o", "max_tokens": 1234,
                    "system": "SYS", "temperature": 0.9, "retries": 2}


def test_generation_and_judge_models_are_openai():
    # Guardrail: the writer + editorial-judgment roles must run on OpenAI, and the
    # residual search role must stay on a (cheap) sonar tier.
    assert podcast.SCRIPT_MODEL.startswith(("gpt", "o1", "o3", "o4"))
    assert podcast.CURATOR_MODEL.startswith(("gpt", "o1", "o3", "o4"))
    assert podcast.JUDGE_MODEL.startswith(("gpt", "o1", "o3", "o4"))
    assert podcast.RECAP_MODEL.startswith(("gpt", "o1", "o3", "o4"))
    assert podcast.SEARCH_MODEL.startswith("sonar")
