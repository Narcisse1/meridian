"""Gemini model fallback: switching models must not use up the retry budget."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from meridian import llm
from meridian.llm import GeminiLLM, LLMQuotaExceeded

pytest.importorskip("google.genai")


class Out(BaseModel):
    answer: str


class APIError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


DAILY = "429 RESOURCE_EXHAUSTED: GenerateRequestsPerDayPerProjectPerModel. Please retry in 17h29m"


class FakeModels:
    def __init__(self, names: list[str], fails: dict[str, APIError]) -> None:
        self.names, self.fails, self.tried = names, fails, []

    def list(self):
        return [SimpleNamespace(name=f"models/{n}", supported_actions=["generateContent"])
                for n in self.names]  # fmt: skip

    def generate_content(self, *, model, contents, config):
        self.tried.append(model)
        if model in self.fails:
            raise self.fails[model]
        return SimpleNamespace(model=model)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(llm, "_candidates", {})
    monkeypatch.setattr(llm, "_unavailable", {})


def make(names, fails, attempts=6):
    models = FakeModels(names, fails)
    return GeminiLLM(model=None, client=SimpleNamespace(models=models), attempts=attempts), models


def test_falls_through_many_exhausted_models_to_one_that_works():
    names = [
        "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
        "gemini-2.5-flash", "gemini-3.5-flash-lite",
    ]  # fmt: skip
    fails = {n: APIError(429, DAILY) for n in names[:4]}
    fails["gemini-2.5-flash"] = APIError(404, "404 NOT_FOUND")
    g, models = make(names, fails, attempts=2)  # fewer retries than models to skip
    assert g._generate("sys", "prompt", Out).model == "gemini-3.5-flash-lite"
    assert models.tried == names


def test_all_models_exhausted_raises_quota_error():
    names = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    g, _ = make(names, {n: APIError(429, DAILY) for n in names})
    with pytest.raises(LLMQuotaExceeded):
        g._generate("sys", "prompt", Out)
