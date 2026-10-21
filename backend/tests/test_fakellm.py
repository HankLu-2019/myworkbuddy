"""The fake gateway must speak enough OpenAI for pydantic-ai, or CI e2e tests would test nothing."""

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from tests.fakellm.server import ANSWER, PLAIN_ANSWER, app


def _model() -> OpenAIChatModel:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fake")
    return OpenAIChatModel("chat", provider=OpenAIProvider(base_url="http://fake/v1", api_key="x", http_client=client))


async def test_plain_answer_without_tools() -> None:
    assert (await Agent(_model()).run("hello")).output == PLAIN_ANSWER


async def test_streams_tool_call_then_cited_answer() -> None:
    seen: list[str] = []
    agent = Agent(_model())

    @agent.tool_plain
    def search_knowledge(query: str) -> list[dict]:
        seen.append(query)
        return [{"n": 1, "title": "doc"}]

    async with agent.run_stream("why does spark OOM?") as run:
        text = await run.get_output()
    assert seen == ["why does spark OOM?"]
    assert text == ANSWER


async def test_structured_output_from_schema() -> None:
    class Summary(BaseModel):
        title: str
        decisions: list[str] = []
        count: int

    out = (await Agent(_model(), output_type=Summary).run("summarise")).output
    assert out == Summary(title="fake", decisions=[], count=0)


async def test_dict_output_and_failure_marker() -> None:
    assert (await Agent(_model(), output_type=dict[str, str]).run("x")).output == {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fake") as c:
        r = await c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "[[fail]]"}]})
    assert r.status_code == 500


async def test_embeddings_are_normalised_and_sized() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fake") as c:
        r = await c.post("/v1/embeddings", json={"model": "embed", "input": ["a b", "c"], "dimensions": 16})
    data = r.json()["data"]
    assert [d["index"] for d in data] == [0, 1] and len(data[0]["embedding"]) == 16
    assert abs(sum(v * v for v in data[0]["embedding"]) - 1) < 1e-9
