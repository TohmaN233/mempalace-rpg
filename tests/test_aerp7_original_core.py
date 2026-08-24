import pytest

from benchmarks import aerp7_original_core as core
from benchmarks import aerp7_original_product as product


def projection():
    return {
        "schema": core.NORMALIZED_PROJECTION_SCHEMA,
        "corpora": [{"corpus_id": "a" * 64, "candidates": [
            {"candidate_id": "b" * 64, "order": 0, "text": "one"},
            {"candidate_id": "c" * 64, "order": 1, "text": "two"},
        ]}],
        "items": [{"item_id": "d" * 64, "corpus_id": "a" * 64, "query_text": "one"}],
    }


def test_dataset_neutral_original_core_validates_label_free_projection_and_namespace():
    frozen = core.validate_normalized_projection(projection())
    namespace = core.identity_namespace(frozen, separator="::core::", schema="test-namespace-v1")
    assert namespace["rows"][0]["physical_id"] == "a" * 64 + "::core::" + "b" * 64
    assert namespace["expected_unique_count"] == 2


def test_dataset_neutral_original_core_rejects_labels_and_cross_corpus_collision():
    leaked = projection(); leaked["items"][0]["ground_truth"] = "forbidden"
    with pytest.raises(core.OriginalCoreError, match="forbidden"):
        core.validate_normalized_projection(leaked)
    collision = projection(); collision["corpora"].append({"corpus_id": "e" * 64, "candidates": [{"candidate_id": "b" * 64, "order": 0, "text": "other"}]})
    with pytest.raises(core.OriginalCoreError, match="candidate_id"):
        core.validate_normalized_projection(collision)


def test_generic_draft_packet_binds_adapter_identity_without_convomem_schema():
    draft = product.OriginalProductWorkerDraft(projection={"opaque": "projection"}, namespace={"opaque": "namespace"}, replicate_without_coordinator_audit={"rankings": []}, worker_physical_receipt={"physical": "receipt"}, telemetry={"resources": {}})
    payload = product.serialize_generic_worker_draft(draft=draft, adapter_id="aerp8-test")
    assert product.load_generic_worker_draft(payload=payload, adapter_id="aerp8-test").namespace == {"opaque": "namespace"}
    with pytest.raises(product.OriginalProductError, match="schema"):
        product.load_generic_worker_draft(payload=payload, adapter_id="wrong")
