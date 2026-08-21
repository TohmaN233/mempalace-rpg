from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from mempalace_rpg import DrawerCompensationError, RpgMemoryKernel, SceneEventInput
from mempalace_rpg.adapter import (
    MempalaceEpisodeAdapter,
    RecordingEpisodeAdapter,
)


class FaultDrawerAdapter(RecordingEpisodeAdapter):
    def __init__(self, failure: str | None = None) -> None:
        super().__init__()
        self.failure = failure
        self.delete_calls: list[str] = []

    def add_scene_drawer(self, **kwargs) -> str:
        if self.failure == "drawer_before":
            self.failure = None
            raise RuntimeError("injected drawer failure before effect")
        drawer_id = super().add_scene_drawer(**kwargs)
        if self.failure == "drawer_after":
            self.failure = None
            raise RuntimeError("injected drawer failure after effect")
        return drawer_id

    def delete_scene_drawer(self, *, drawer_id: str) -> None:
        self.delete_calls.append(drawer_id)
        if self.failure == "cleanup":
            raise RuntimeError("injected drawer cleanup failure")
        super().delete_scene_drawer(drawer_id=drawer_id)


class FaultKernel(RpgMemoryKernel):
    def __init__(self, *args, fail_stage: str | None = None, **kwargs) -> None:
        self.fail_stage = fail_stage
        super().__init__(*args, **kwargs)

    def _scene_commit_checkpoint(self, stage: str) -> None:
        if stage == self.fail_stage:
            self.fail_stage = None
            raise RuntimeError(f"injected SQLite failure at {stage}")

    def _commit_scene_sqlite(self, conn) -> None:
        if self.fail_stage == "sqlite_commit":
            self.fail_stage = None
            raise RuntimeError("injected SQLite failure at sqlite_commit")
        if self.fail_stage == "sqlite_commit_after_effect":
            super()._commit_scene_sqlite(conn)
            self.fail_stage = None
            raise RuntimeError("injected SQLite failure at sqlite_commit_after_effect")
        super()._commit_scene_sqlite(conn)


def _request(*, transcript: str = "Aster placed the ember key on the gate altar.") -> dict:
    return {
        "scene_id": "atomic-scene",
        "campaign_id": " atomic-campaign ",
        "in_world_time": "day-12",
        "location_id": "loc_gate",
        "active_quest_ids": ["quest_gate"],
        "participants": [" char_aster ", "char_aster"],
        "witnesses": ["char_witness"],
        "transcript": transcript,
        "events": [
            SceneEventInput(
                event_type="evidence",
                summary="Aster placed the ember key on the gate altar.",
                branch_id="main",
                branch_status="active",
                actor_id="char_aster",
                target_id="item_ember_key",
                truth_status="canonical",
                visibility="public_world",
                witness_set=["char_witness"],
                related_entities=["item_ember_key"],
                related_quests=["quest_gate"],
                related_locations=["loc_gate"],
                source_span="Aster placed the ember key on the gate altar.",
                importance=0.8,
                payload={"nested": {"b": 2, "a": 1}},
            )
        ],
    }


TARGET_ENTITIES = (
    "char_aster",
    "char_witness",
    "item_ember_key",
    "loc_gate",
    "quest_gate",
)


def _sqlite_state(kernel: RpgMemoryKernel) -> dict[str, int]:
    conn = kernel._conn()
    placeholders = ",".join("?" for _ in TARGET_ENTITIES)
    return {
        "scenes": conn.execute(
            "SELECT COUNT(*) FROM scene_record WHERE scene_id='atomic-scene'"
        ).fetchone()[0],
        "events": conn.execute(
            "SELECT COUNT(*) FROM scene_event WHERE scene_id='atomic-scene'"
        ).fetchone()[0],
        "memory": conn.execute(
            "SELECT COUNT(*) FROM memory_item WHERE source_scene_id='atomic-scene'"
        ).fetchone()[0],
        "facts": conn.execute("SELECT COUNT(*) FROM world_fact").fetchone()[0],
        "beliefs": conn.execute("SELECT COUNT(*) FROM actor_belief").fetchone()[0],
        "entities": conn.execute(
            f"SELECT COUNT(*) FROM entity_registry WHERE entity_id IN ({placeholders})",
            TARGET_ENTITIES,
        ).fetchone()[0],
        "importance": conn.execute(
            f"SELECT COUNT(*) FROM entity_importance WHERE entity_id IN ({placeholders})",
            TARGET_ENTITIES,
        ).fetchone()[0],
    }


@pytest.mark.parametrize(
    ("failure", "kernel_stage"),
    [
        ("drawer_before", None),
        ("drawer_after", None),
        (None, "scene_insert"),
        (None, "event_insert"),
        (None, "projection_insert"),
        (None, "sqlite_commit"),
    ],
)
def test_single_failure_leaves_no_half_state_and_retry_is_idempotent(
    tmp_path, failure, kernel_stage
):
    adapter = FaultDrawerAdapter(failure=failure)
    kernel = FaultKernel(
        db_path=str(tmp_path / "atomic.sqlite3"),
        episode_adapter=adapter,
        fail_stage=kernel_stage,
    )
    request = _request()

    with pytest.raises(RuntimeError, match="injected"):
        kernel.commit_scene(**request)

    assert _sqlite_state(kernel) == {
        "scenes": 0,
        "events": 0,
        "memory": 0,
        "facts": 0,
        "beliefs": 0,
        "entities": 0,
        "importance": 0,
    }
    assert adapter.drawers == []
    assert adapter.delete_calls == ["rpg_scene_atomic-scene"]

    assert kernel.commit_scene(**request) == "atomic-scene"
    committed = _sqlite_state(kernel)
    assert committed["scenes"] == 1
    assert committed["events"] == 1
    assert committed["memory"] > 0
    assert committed["entities"] == len(TARGET_ENTITIES)
    assert len(adapter.drawers) == 1

    normalized_retry = _request()
    normalized_retry["campaign_id"] = "atomic-campaign"
    normalized_retry["participants"] = ["char_aster"]
    assert kernel.commit_scene(**normalized_retry) == "atomic-scene"
    assert _sqlite_state(kernel) == committed
    assert len(adapter.drawers) == 1


def test_same_scene_id_with_different_payload_fails_before_drawer_write(tmp_path):
    adapter = FaultDrawerAdapter()
    kernel = RpgMemoryKernel(
        db_path=str(tmp_path / "conflict.sqlite3"),
        episode_adapter=adapter,
    )
    request = _request()
    kernel.commit_scene(**request)
    committed = _sqlite_state(kernel)

    changed = _request(transcript="Aster placed the ember key on the gate altar. Extra.")
    with pytest.raises(ValueError, match="different request fingerprint"):
        kernel.commit_scene(**changed)

    assert _sqlite_state(kernel) == committed
    assert len(adapter.drawers) == 1
    assert adapter.delete_calls == []


def test_post_commit_failure_is_observable_and_retry_recovers_without_duplicates(tmp_path):
    adapter = FaultDrawerAdapter()
    db_path = str(tmp_path / "post-commit.sqlite3")
    kernel = FaultKernel(
        db_path=db_path,
        episode_adapter=adapter,
        fail_stage="sqlite_commit_after_effect",
    )
    request = _request()

    with pytest.raises(RuntimeError, match="sqlite_commit_after_effect"):
        kernel.commit_scene(**request)

    committed = _sqlite_state(kernel)
    assert committed["scenes"] == 1
    assert committed["events"] == 1
    assert committed["memory"] > 0
    assert len(adapter.drawers) == 1
    assert adapter.delete_calls == []

    kernel.close()
    kernel = RpgMemoryKernel(db_path=db_path, episode_adapter=adapter)
    assert kernel.commit_scene(**request) == "atomic-scene"
    assert _sqlite_state(kernel) == committed
    assert len(adapter.drawers) == 1


def test_cleanup_failure_raises_observable_compound_error(tmp_path):
    adapter = FaultDrawerAdapter(failure="drawer_after")
    original_delete = adapter.delete_scene_drawer

    def fail_cleanup(*, drawer_id: str) -> None:
        adapter.failure = "cleanup"
        original_delete(drawer_id=drawer_id)

    adapter.delete_scene_drawer = fail_cleanup
    kernel = RpgMemoryKernel(
        db_path=str(tmp_path / "cleanup.sqlite3"),
        episode_adapter=adapter,
    )

    with pytest.raises(DrawerCompensationError) as caught:
        kernel.commit_scene(**_request())

    error = caught.value
    assert error.drawer_id == "rpg_scene_atomic-scene"
    assert "after effect" in str(error.original_error)
    assert "cleanup failure" in str(error.cleanup_error)
    assert "original=RuntimeError" in str(error)
    assert "cleanup=RuntimeError" in str(error)
    assert _sqlite_state(kernel)["scenes"] == 0


def test_mempalace_adapter_deletes_by_deterministic_drawer_id(monkeypatch):
    collection = SimpleNamespace(delete_calls=[])

    def delete(*, ids):
        collection.delete_calls.append(ids)

    collection.delete = delete
    palace_module = ModuleType("mempalace.palace")
    palace_module.get_collection = lambda *args, **kwargs: collection
    package_module = ModuleType("mempalace")
    package_module.__path__ = []
    monkeypatch.setitem(sys.modules, "mempalace", package_module)
    monkeypatch.setitem(sys.modules, "mempalace.palace", palace_module)

    adapter = MempalaceEpisodeAdapter("unused")
    adapter.delete_scene_drawer(drawer_id="rpg_scene_stable")

    assert collection.delete_calls == [["rpg_scene_stable"]]
