def test_create_list_get_board(client):
    r = client.post("/api/boards", json={"name": "Scene 01"})
    assert r.status_code == 200
    board = r.json()
    assert board["name"] == "Scene 01"
    assert isinstance(board["id"], int)

    r = client.get("/api/boards")
    assert r.status_code == 200
    listing = r.json()
    assert any(b["id"] == board["id"] for b in listing)

    r = client.get(f"/api/boards/{board['id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["board"]["id"] == board["id"]
    assert detail["nodes"] == []
    assert detail["edges"] == []


def test_get_missing_board_returns_404(client):
    r = client.get("/api/boards/999")
    assert r.status_code == 404


def test_patch_board_rename(client):
    b = client.post("/api/boards", json={"name": "Old"}).json()
    r = client.patch(f"/api/boards/{b['id']}", json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"

    # persistence
    r = client.get(f"/api/boards/{b['id']}")
    assert r.json()["board"]["name"] == "New"


def test_patch_missing_board_returns_404(client):
    r = client.patch("/api/boards/999", json={"name": "x"})
    assert r.status_code == 404


def test_delete_board_cascades_children(client):
    """DELETE /api/boards/{id} must remove every child row that references
    the board so a re-create with the same id (sqlite autoincrement edge
    case) doesn't pull in orphan rows."""
    from flowboard.db import get_session
    from flowboard.db.models import (
        Asset,
        BoardFlowProject,
        ChatMessage,
        Edge,
        Node,
        PipelineRun,
        Plan,
        PlanRevision,
        Reference,
        Request,
    )
    from sqlmodel import select

    b = client.post("/api/boards", json={"name": "to-be-deleted"}).json()
    bid = b["id"]

    # Seed: 2 nodes + 1 edge + 1 request + 1 asset + 1 chat + 1 plan with
    # 1 revision + 1 pipeline run + Flow-project mapping.
    n1 = client.post("/api/nodes", json={"board_id": bid, "type": "image"}).json()
    n2 = client.post("/api/nodes", json={"board_id": bid, "type": "video"}).json()
    client.post(
        "/api/edges", json={"board_id": bid, "source_id": n1["id"], "target_id": n2["id"]}
    ).json()
    client.post(
        "/api/requests",
        json={
            "node_id": n1["id"],
            "type": "proxy",
            "params": {"url": "https://aisandbox-pa.googleapis.com/v1/x"},
        },
    ).json()
    with get_session() as s:
        s.add(Asset(uuid_media_id="11111111-2222-3333-4444-555555555555", node_id=n1["id"], kind="image"))
        s.add(ChatMessage(board_id=bid, role="user", content="hi"))
        plan = Plan(board_id=bid, spec={"k": "v"})
        s.add(plan)
        s.commit()
        s.refresh(plan)
        s.add(PlanRevision(plan_id=plan.id, rev_no=1, spec={}, edits={}))
        s.add(PipelineRun(plan_id=plan.id, status="pending"))
        s.add(BoardFlowProject(board_id=bid, flow_project_id="fpfpfpfp"))
        s.add(
            Reference(
                media_id="22222222-3333-4444-5555-666666666666",
                kind="image",
                label="saved reference",
                source_board_id=bid,
                source_node_short_id=n1["short_id"],
            )
        )
        s.commit()

    # Delete.
    r = client.delete(f"/api/boards/{bid}")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": bid}

    # Board itself gone.
    assert client.get(f"/api/boards/{bid}").status_code == 404

    # Every child table swept.
    with get_session() as s:
        for table, where in [
            (Node, Node.board_id == bid),
            (Edge, Edge.board_id == bid),
            (ChatMessage, ChatMessage.board_id == bid),
            (Plan, Plan.board_id == bid),
            (BoardFlowProject, BoardFlowProject.board_id == bid),
        ]:
            rows = s.exec(select(table).where(where)).all()
            assert rows == [], f"{table.__name__} not cleared: {rows}"
        # Asset / Request reference node_id, which no longer exists.
        assert s.exec(select(Asset).where(Asset.node_id.in_([n1["id"], n2["id"]]))).all() == []
        assert s.exec(select(Request).where(Request.node_id.in_([n1["id"], n2["id"]]))).all() == []
        refs = s.exec(
            select(Reference).where(
                Reference.media_id == "22222222-3333-4444-5555-666666666666"
            )
        ).all()
        assert len(refs) == 1
        assert refs[0].source_board_id is None


def test_delete_missing_board_returns_404(client):
    r = client.delete("/api/boards/999")
    assert r.status_code == 404
