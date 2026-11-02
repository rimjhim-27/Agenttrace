import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from agenttrace import SQLiteStore, Tracer  # noqa: E402
from agenttrace.api import create_app  # noqa: E402
from agenttrace.cli import main  # noqa: E402


def span_json(trace_id, seq, **kw):
    base = {"span_id": f"{trace_id}-{seq}", "trace_id": trace_id, "name": "search", "kind": "tool", "seq": seq,
            "start_ts": 100.0 + seq, "end_ts": 100.5 + seq, "duration_ms": 500.0, "input": {"q": "x"}, "output": "r"}
    base.update(kw)
    return base


@pytest.fixture
def client():
    return TestClient(create_app(SQLiteStore(":memory:"), api_key=None))


def test_health_and_viewer_are_served(client):
    assert client.get("/api/v1/health").json()["status"] == "ok"
    html = client.get("/").text
    assert "AgentTrace" in html and "textContent" in html and "innerHTML" not in html  # XSS-safe viewer


def test_ingest_then_query_roundtrip(client):
    body = {"spans": [span_json("t1", 0), span_json("t1", 1, error="X: y")], "trace_name": "remote", "finish": True}
    r = client.post("/api/v1/spans", json=body)
    assert r.status_code == 202 and r.json() == {"accepted": 2, "traces": ["t1"]}
    runs = client.get("/api/v1/runs").json()
    assert runs["total"] == 1 and runs["runs"][0]["name"] == "remote" and runs["runs"][0]["status"] == "ok"
    detail = client.get("/api/v1/runs/t1").json()
    assert [s["seq"] for s in detail["spans"]] == [0, 1] and detail["run"]["error_count"] == 1


def test_ingest_is_idempotent(client):
    body = {"spans": [span_json("t1", 0)]}
    for _ in range(3):
        client.post("/api/v1/spans", json=body)
    assert len(client.get("/api/v1/runs/t1").json()["spans"]) == 1


def test_validation_errors(client):
    assert client.post("/api/v1/spans", json={"spans": []}).status_code == 422
    assert client.post("/api/v1/spans", json={"spans": [span_json("t", 0, kind="bogus")]}).status_code == 422
    assert client.get("/api/v1/runs?limit=0").status_code == 422
    assert client.get("/api/v1/runs/nope").status_code == 404


def test_insights_and_diff_endpoints(client):
    looping = [span_json("a", i) for i in range(4)]
    other = [span_json("b", 0, output="different")]
    client.post("/api/v1/spans", json={"spans": looping})
    client.post("/api/v1/spans", json={"spans": other})
    ins = client.get("/api/v1/runs/a/insights").json()["insights"]
    assert any(i["kind"] == "loop" for i in ins)
    diff = client.get("/api/v1/diff", params={"a": "a", "b": "b"}).json()
    assert diff["summary"]["removed"] == 3 and diff["summary"]["changed"] == 1
    assert client.get("/api/v1/diff", params={"a": "a", "b": "ghost"}).status_code == 404


def test_api_key_protects_data_routes_but_not_health():
    c = TestClient(create_app(SQLiteStore(":memory:"), api_key="s3cret"))
    assert c.get("/api/v1/health").status_code == 200
    assert c.get("/api/v1/runs").status_code == 401
    assert c.post("/api/v1/spans", json={"spans": [span_json("t", 0)]}).status_code == 401
    assert c.get("/api/v1/runs", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/api/v1/runs", headers={"X-API-Key": "s3cret"}).status_code == 200


# --------------------------------------------------------------------- CLI
@pytest.fixture
def seeded_db(tmp_path):
    path = str(tmp_path / "t.db")
    t = Tracer(SQLiteStore(path))

    @t.trace(kind="tool", name="search")
    def search(q):
        return "r"

    @t.trace(kind="agent", name="agent")
    def agent(n):
        for _ in range(n):
            search("same")

    ids = []
    for n in (1, 4):  # second run loops
        with t.run("demo") as r:
            agent(n)
        ids.append(r.trace_id)
    t.close()
    t.store.close()
    return path, ids


def test_cli_runs_show_insights_diff(seeded_db, capsys):
    path, (good, loopy) = seeded_db
    assert main(["--db", path, "runs"]) == 0
    out = capsys.readouterr().out
    assert good[:8] in out and loopy[:8] in out

    assert main(["--db", path, "show", loopy[:8], "--io"]) == 0
    out = capsys.readouterr().out
    assert "[agent] agent" in out and out.count("[tool] search") == 4

    assert main(["--db", path, "insights", loopy[:8]]) == 0
    assert "loop" in capsys.readouterr().out

    assert main(["--db", path, "diff", good[:8], loopy[:8]]) == 0
    assert "added=3" in capsys.readouterr().out


def test_cli_missing_db_and_unknown_run(tmp_path, seeded_db, capsys):
    assert main(["--db", str(tmp_path / "nope.db"), "runs"]) == 2
    path, _ = seeded_db
    with pytest.raises(SystemExit):
        main(["--db", path, "show", "zzzz"])


def test_cli_export_json(seeded_db, capsys):
    path, (good, loopy) = seeded_db
    assert main(["--db", path, "export", good[:8]]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["run"]["trace_id"] == good
    assert [s["name"] for s in payload["spans"]] == ["agent", "search"]
    assert all(s["span_id"] for s in payload["spans"])


def test_cli_runs_since_and_status_filters(seeded_db, capsys):
    path, (good, loopy) = seeded_db
    # both runs share the same start time, so a since filter in the future returns nothing
    assert main(["--db", path, "runs", "--since", "2e9"]) == 0
    assert "no runs yet" in capsys.readouterr().out
    # both seeded runs finish without error, so they are "ok"; filtering by a
    # status that no run has returns nothing
    assert main(["--db", path, "runs", "--status", "running"]) == 0
    assert "no runs yet" in capsys.readouterr().out
    assert main(["--db", path, "runs", "--status", "error"]) == 0
    assert "no runs yet" in capsys.readouterr().out
    # invalid status choice is rejected by argparse (raises SystemExit)
    with pytest.raises(SystemExit):
        main(["--db", path, "runs", "--status", "bogus"])
