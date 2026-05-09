"""Tests for the Storyboard worker handlers.

Plan: .omc/plans/storyboard-image-node.md §5 Phase 3, §7 acceptance #3-#8.

`gen_storyboard` walks a continuity tree:
  Phase A — gen_image roots in parallel chunks of ≤4.
  Phase B — BFS children, edit_image with base = parent.mediaId,
            siblings parallel.
`retry_storyboard_shot` reads Node.data.shots[idx] and re-dispatches.

Each test stubs `get_flow_sdk` with a minimal in-memory SDK that records
calls and returns deterministic media_ids.
"""
from __future__ import annotations

import asyncio

import pytest

from flowboard.db import get_session
from flowboard.db.models import Board, Node, Request
from flowboard.worker import processor as proc
from flowboard.worker.processor import (
    WorkerController,
    _aggregate_node_status,
    _propagate_blocked,
)


# ── helpers ───────────────────────────────────────────────────────────────


def _seed_storyboard_node() -> int:
    """Create a Board + a Storyboard target node. Return node.id."""
    with get_session() as s:
        b = Board(name="sb-worker")
        s.add(b); s.commit(); s.refresh(b)
        target = Node(
            board_id=b.id, short_id="sbtg", type="Storyboard",
            x=0, y=0, w=240, h=180,
            data={"title": "Story"},
            status="idle",
        )
        s.add(target); s.commit(); s.refresh(target)
        return target.id


class _StubSdk:
    """Minimal SDK stub.

    `gen_image_results` / `edit_image_results` are FIFO queues — each
    call pops the next response. If `_raise` is set on a call payload,
    the call raises instead of returning. This lets a test inject root
    success + child failure + retry success in a deterministic order.
    """

    def __init__(self):
        self.gen_calls: list[dict] = []
        self.edit_calls: list[dict] = []
        self.gen_image_results: list = []  # list[dict | Exception]
        self.edit_image_results: list = []

    async def gen_image(self, **kwargs):
        self.gen_calls.append(kwargs)
        if not self.gen_image_results:
            return {
                "media_ids": [
                    f"gen-{len(self.gen_calls)}-{i}"
                    for i in range(kwargs.get("variant_count", 1))
                ],
                "media_entries": [],
            }
        nxt = self.gen_image_results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def edit_image(self, **kwargs):
        self.edit_calls.append(kwargs)
        if not self.edit_image_results:
            return {
                "media_ids": [f"edit-{len(self.edit_calls)}"],
                "media_entries": [],
            }
        nxt = self.edit_image_results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# ── pure helpers ──────────────────────────────────────────────────────────


def test_propagate_blocked_one_level():
    shots = [
        {"idx": 0, "parentShotIdx": None, "status": "error"},
        {"idx": 1, "parentShotIdx": 0, "status": "queued"},
        {"idx": 2, "parentShotIdx": 1, "status": "queued"},
    ]
    _propagate_blocked(shots)
    assert shots[1]["status"] == "blocked"
    assert shots[1]["error"] == "parent_failed"
    assert shots[2]["status"] == "blocked"  # transitive


def test_propagate_blocked_independent_root_unaffected():
    shots = [
        {"idx": 0, "parentShotIdx": None, "status": "error"},
        {"idx": 1, "parentShotIdx": 0, "status": "queued"},
        {"idx": 2, "parentShotIdx": None, "status": "done"},  # independent root
        {"idx": 3, "parentShotIdx": 2, "status": "queued"},
    ]
    _propagate_blocked(shots)
    assert shots[1]["status"] == "blocked"
    assert shots[2]["status"] == "done"
    assert shots[3]["status"] == "queued"  # not blocked — its parent is fine


def test_aggregate_node_status_done():
    shots = [{"status": "done"}, {"status": "done"}]
    assert _aggregate_node_status(shots) == "done"


def test_aggregate_node_status_partial():
    shots = [{"status": "done"}, {"status": "error"}]
    assert _aggregate_node_status(shots) == "partial"


def test_aggregate_node_status_all_error():
    shots = [{"status": "error"}, {"status": "blocked"}]
    assert _aggregate_node_status(shots) == "error"


# ── _handle_gen_storyboard happy paths ────────────────────────────────────


@pytest.mark.asyncio
async def test_gen_storyboard_all_roots_chunks_into_two_calls(
    client, monkeypatch
):
    """parents=[None]*8 → Phase A only, two chunks of 4. No edit_image calls."""
    node_id = _seed_storyboard_node()
    sdk = _StubSdk()
    sdk.gen_image_results = [
        {
            "media_ids": [f"root-{i}" for i in range(4)],
            "media_entries": [],
        },
        {
            "media_ids": [f"root-{4+i}" for i in range(4)],
            "media_entries": [],
        },
    ]
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)

    out, err = await proc._handle_gen_storyboard(
        {
            "shot_count": 8,
            "project_id": "abcd1234",
            "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
            "paygate_tier": "PAYGATE_TIER_ONE",
            "global_ref_media_ids": ["ref-aaa"],
            "shot_prompts": [f"beat {k}" for k in range(8)],
            "shot_parents": [None] * 8,
            "__node_id": node_id,
        }
    )
    assert err is None, err
    assert out["node_status"] == "done"
    assert out["media_ids"] == [f"root-{i}" for i in range(8)]
    assert len(sdk.gen_calls) == 2
    assert all(c["variant_count"] == 4 for c in sdk.gen_calls)
    assert sdk.edit_calls == []  # no children


@pytest.mark.asyncio
async def test_gen_storyboard_mixed_tree_dispatches_levels(client, monkeypatch):
    """parents=[None,0,1,2,3,None,5,6] — Phase A: gen_image(2 roots),
    Phase B: 6 sequential edit levels (each child waits for parent)."""
    node_id = _seed_storyboard_node()
    sdk = _StubSdk()
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)

    parents = [None, 0, 1, 2, 3, None, 5, 6]
    out, err = await proc._handle_gen_storyboard(
        {
            "shot_count": 8,
            "project_id": "abcd1234",
            "paygate_tier": "PAYGATE_TIER_ONE",
            "shot_prompts": [f"beat {k}" for k in range(8)],
            "shot_parents": parents,
            "__node_id": node_id,
        }
    )
    assert err is None, err
    assert out["node_status"] == "done"
    # Phase A: 1 gen_image call covering 2 roots in one batch.
    assert len(sdk.gen_calls) == 1
    assert sdk.gen_calls[0]["variant_count"] == 2
    # Phase B: 6 edit_image calls. Each child references its parent's mediaId.
    assert len(sdk.edit_calls) == 6
    # The two roots are shots 0 + 5 → media_ids[0] and [1] from the batch.
    # Then children 1, 6 (each edit from their root), 2, 7, 3, 4.
    sources_in_order = [c["source_media_id"] for c in sdk.edit_calls]
    # First level: children of done roots {0, 5} = shots 1 and 6, dispatched
    # in parallel (asyncio.gather), so order between them is non-deterministic;
    # but both must reference the right roots.
    first_level_sources = set(sources_in_order[:2])
    assert first_level_sources <= {out["shots"][0]["mediaId"], out["shots"][5]["mediaId"]}


@pytest.mark.asyncio
async def test_gen_storyboard_chain_serial_levels(client, monkeypatch):
    """parents=[None,0,1,2] — pure chain; each Phase B level has 1 shot."""
    node_id = _seed_storyboard_node()
    sdk = _StubSdk()
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)

    out, err = await proc._handle_gen_storyboard(
        {
            "shot_count": 4,
            "project_id": "abcd1234",
            "paygate_tier": "PAYGATE_TIER_ONE",
            "shot_prompts": ["a", "b", "c", "d"],
            "shot_parents": [None, 0, 1, 2],
            "__node_id": node_id,
        }
    )
    assert err is None
    assert out["node_status"] == "done"
    # 1 root → 1 gen_image call, variant_count=1
    assert len(sdk.gen_calls) == 1
    assert sdk.gen_calls[0]["variant_count"] == 1
    # 3 children, dispatched serial level-by-level.
    assert len(sdk.edit_calls) == 3
    # source_media_id chain: each edit_image(k+1) sources shot[k]'s mediaId.
    assert sdk.edit_calls[0]["source_media_id"] == out["shots"][0]["mediaId"]
    assert sdk.edit_calls[1]["source_media_id"] == out["shots"][1]["mediaId"]
    assert sdk.edit_calls[2]["source_media_id"] == out["shots"][2]["mediaId"]


# ── _handle_gen_storyboard failure paths ──────────────────────────────────


@pytest.mark.asyncio
async def test_gen_storyboard_root_failure_blocks_descendants(client, monkeypatch):
    """parents=[None,0,1,None,3] — root 0 fails; shots 1,2 → blocked.
    Independent root 3 succeeds; shot 4 dispatches normally."""
    node_id = _seed_storyboard_node()
    sdk = _StubSdk()

    # Phase A dispatches both roots in ONE chunk of 2. Inject a "missing media"
    # response so root 0 errors but root 3 succeeds.
    sdk.gen_image_results = [
        {"media_ids": [None, "root-3-mid"], "media_entries": []},
    ]
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)

    out, err = await proc._handle_gen_storyboard(
        {
            "shot_count": 5,
            "project_id": "abcd1234",
            "paygate_tier": "PAYGATE_TIER_ONE",
            "shot_prompts": [f"b{k}" for k in range(5)],
            "shot_parents": [None, 0, 1, None, 3],
            "__node_id": node_id,
        }
    )
    assert err is None
    shots = out["shots"]
    assert shots[0]["status"] == "error"
    assert shots[0]["error"] == "missing_media"
    assert shots[1]["status"] == "blocked"
    assert shots[1]["error"] == "parent_failed"
    assert shots[2]["status"] == "blocked"
    # Shot 3 (independent root) must succeed
    assert shots[3]["status"] == "done"
    assert shots[3]["mediaId"] == "root-3-mid"
    # Shot 4 (child of 3) edits successfully
    assert shots[4]["status"] == "done"
    # Mixed status → "partial"
    assert out["node_status"] == "partial"


@pytest.mark.asyncio
async def test_gen_storyboard_validates_parents_root_must_be_null(
    client, monkeypatch
):
    sdk = _StubSdk()
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)
    out, err = await proc._handle_gen_storyboard(
        {
            "shot_count": 2,
            "project_id": "abcd1234",
            "paygate_tier": "PAYGATE_TIER_ONE",
            "shot_prompts": ["a", "b"],
            "shot_parents": [0, 0],  # parents[0] != null
            "__node_id": _seed_storyboard_node(),
        }
    )
    assert err == "parents_root_must_be_null"


@pytest.mark.asyncio
async def test_gen_storyboard_validates_count_range(client, monkeypatch):
    out, err = await proc._handle_gen_storyboard(
        {
            "shot_count": 9,
            "project_id": "abcd1234",
            "paygate_tier": "PAYGATE_TIER_ONE",
            "shot_prompts": ["a"] * 9,
            "shot_parents": [None] * 9,
        }
    )
    assert err == "shot_count_out_of_range"


# ── _handle_retry_storyboard_shot ─────────────────────────────────────────


def _seed_storyboard_with_shots(shots: list[dict]) -> int:
    """Create a node with a pre-populated shots[] state for retry tests."""
    with get_session() as s:
        b = Board(name="sb-retry")
        s.add(b); s.commit(); s.refresh(b)
        n = Node(
            board_id=b.id, short_id="sbrt", type="Storyboard",
            x=0, y=0, w=240, h=180,
            data={
                "shots": shots,
                "shotCount": len(shots),
                "projectId": "abcd1234",
                "paygateTier": "PAYGATE_TIER_ONE",
                "aspectRatio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
            },
            status="partial",
        )
        s.add(n); s.commit(); s.refresh(n)
        return n.id


@pytest.mark.asyncio
async def test_retry_storyboard_shot_root_dispatches_gen_image(
    client, monkeypatch
):
    node_id = _seed_storyboard_with_shots(
        [
            {
                "idx": 0, "prompt": "beat 0", "parentShotIdx": None,
                "mediaId": None, "status": "error", "error": "boom",
            },
        ]
    )
    sdk = _StubSdk()
    sdk.gen_image_results = [{"media_ids": ["new-root"], "media_entries": []}]
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)

    out, err = await proc._handle_retry_storyboard_shot(
        {"shot_idx": 0, "__node_id": node_id}
    )
    assert err is None
    assert out["media_id"] == "new-root"
    assert len(sdk.gen_calls) == 1
    assert sdk.gen_calls[0]["variant_count"] == 1
    assert sdk.edit_calls == []


@pytest.mark.asyncio
async def test_retry_storyboard_shot_child_dispatches_edit_image(
    client, monkeypatch
):
    node_id = _seed_storyboard_with_shots(
        [
            {
                "idx": 0, "prompt": "p0", "parentShotIdx": None,
                "mediaId": "root-mid", "status": "done", "error": None,
            },
            {
                "idx": 1, "prompt": "p1", "parentShotIdx": 0,
                "mediaId": None, "status": "error", "error": "boom",
            },
        ]
    )
    sdk = _StubSdk()
    sdk.edit_image_results = [{"media_ids": ["new-edit"], "media_entries": []}]
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)

    out, err = await proc._handle_retry_storyboard_shot(
        {"shot_idx": 1, "__node_id": node_id}
    )
    assert err is None
    assert out["media_id"] == "new-edit"
    assert sdk.gen_calls == []
    assert len(sdk.edit_calls) == 1
    assert sdk.edit_calls[0]["source_media_id"] == "root-mid"


@pytest.mark.asyncio
async def test_retry_storyboard_shot_forwards_ref_media_ids(client, monkeypatch):
    """Regression — refs were silently dropped on retry because the frontend
    didn't pass `ref_media_ids` and node.data.globalRefMediaIds was never
    persisted. After the fix the frontend collects refs at retry time and
    passes them via params; this test pins the worker side of that contract:
    explicit ref_media_ids in params MUST reach the SDK call."""
    node_id = _seed_storyboard_with_shots(
        [
            {
                "idx": 0, "prompt": "p0", "parentShotIdx": None,
                "mediaId": None, "status": "error", "error": "boom",
            },
        ]
    )
    sdk = _StubSdk()
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)

    refs = ["char-mid-1", "wardrobe-mid-1"]
    out, err = await proc._handle_retry_storyboard_shot(
        {
            "shot_idx": 0,
            "__node_id": node_id,
            "ref_media_ids": refs,
        }
    )
    assert err is None
    assert len(sdk.gen_calls) == 1
    assert sdk.gen_calls[0]["ref_media_ids"] == refs


@pytest.mark.asyncio
async def test_retry_storyboard_shot_child_rejects_when_parent_unhealthy(
    client, monkeypatch
):
    node_id = _seed_storyboard_with_shots(
        [
            {
                "idx": 0, "prompt": "p0", "parentShotIdx": None,
                "mediaId": None, "status": "error", "error": "boom",
            },
            {
                "idx": 1, "prompt": "p1", "parentShotIdx": 0,
                "mediaId": None, "status": "blocked", "error": "parent_failed",
            },
        ]
    )
    sdk = _StubSdk()
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)

    out, err = await proc._handle_retry_storyboard_shot(
        {"shot_idx": 1, "__node_id": node_id}
    )
    assert err == "parent_not_ready"
    assert sdk.gen_calls == []
    assert sdk.edit_calls == []


@pytest.mark.asyncio
async def test_retry_storyboard_shot_validates_shot_idx(client, monkeypatch):
    node_id = _seed_storyboard_with_shots(
        [
            {
                "idx": 0, "prompt": "p0", "parentShotIdx": None,
                "mediaId": "m", "status": "done",
            }
        ]
    )
    sdk = _StubSdk()
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)

    _, err = await proc._handle_retry_storyboard_shot(
        {"shot_idx": 5, "__node_id": node_id}
    )
    assert err == "shot_idx_out_of_range"


# ── End-to-end through /api/requests + WorkerController ───────────────────


@pytest.mark.asyncio
async def test_gen_storyboard_via_request_endpoint(client, monkeypatch):
    """Full round-trip: POST /api/requests {type:"gen_storyboard"} → poll."""
    node_id = _seed_storyboard_node()
    sdk = _StubSdk()
    monkeypatch.setattr(proc, "get_flow_sdk", lambda: sdk)

    row = client.post(
        "/api/requests",
        json={
            "type": "gen_storyboard",
            "node_id": node_id,
            "params": {
                "shot_count": 3,
                "project_id": "abcd1234",
                "paygate_tier": "PAYGATE_TIER_ONE",
                "shot_prompts": ["a", "b", "c"],
                "shot_parents": [None, 0, 1],
            },
        },
    ).json()
    assert "id" in row, row

    w = WorkerController(handlers={
        "gen_storyboard": proc._handle_gen_storyboard,
    })
    task = asyncio.create_task(w.start())
    try:
        w.enqueue(row["id"])
        for _ in range(60):
            await asyncio.sleep(0.05)
            current = client.get(f"/api/requests/{row['id']}").json()
            if current["status"] not in ("queued", "running"):
                break
        assert current["status"] == "done", current
        assert current["result"]["node_status"] == "done"
        assert len(current["result"]["shots"]) == 3
        assert all(s["mediaId"] for s in current["result"]["shots"])
    finally:
        w.request_shutdown()
        await asyncio.wait_for(task, timeout=2.0)
