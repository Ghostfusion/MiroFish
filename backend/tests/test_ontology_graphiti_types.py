"""本体定义到 graphiti 类型定义的转换契约。"""

from app.utils.ontology import (
    MAX_ONTOLOGY_ATTRIBUTES,
    MAX_ONTOLOGY_SOURCE_TARGETS,
    RESERVED_ONTOLOGY_ATTRIBUTE_NAMES,
    WILDCARD_ENTITY_TYPE,
    build_graphiti_types,
)


def test_entity_and_edge_attributes_become_model_fields():
    types = build_graphiti_types({
        "entity_types": [{
            "name": "Speaker",
            "attributes": ["role", {"name": "age", "type": "integer"}],
        }],
        "edge_types": [{
            "name": "MENTIONS",
            "attributes": ["reason"],
        }],
    })

    speaker_fields = types["entity_types"]["Speaker"].model_fields
    assert set(speaker_fields) == {"role", "age"}
    assert speaker_fields["role"].description == "role"
    assert speaker_fields["age"].default is None

    mentions_fields = types["edge_types"]["MENTIONS"].model_fields
    assert set(mentions_fields) == {"reason"}


def test_attribute_names_reserved_by_the_graph_are_rewritten():
    types = build_graphiti_types({
        "entity_types": [{
            "name": "Speaker",
            "attributes": sorted(RESERVED_ONTOLOGY_ATTRIBUTE_NAMES),
        }],
        "edge_types": [],
    })

    fields = types["entity_types"]["Speaker"].model_fields
    assert len(fields) == len(RESERVED_ONTOLOGY_ATTRIBUTE_NAMES)
    assert set(fields) == {
        f"entity_{name}" for name in RESERVED_ONTOLOGY_ATTRIBUTE_NAMES
    }


def test_attribute_list_is_capped_and_never_empty():
    types = build_graphiti_types({
        "entity_types": [{
            "name": "Speaker",
            "attributes": [
                f"field_{index}" for index in range(MAX_ONTOLOGY_ATTRIBUTES + 5)
            ],
        }],
        "edge_types": [{"name": "MENTIONS", "attributes": []}],
    })

    assert len(types["entity_types"]["Speaker"].model_fields) == MAX_ONTOLOGY_ATTRIBUTES
    assert set(types["edge_types"]["MENTIONS"].model_fields) == {"details"}


def test_edge_signatures_are_deduplicated_capped_and_mapped_to_edge_names():
    signatures = [
        {"source": f"Source{index}", "target": f"Target{index}"}
        for index in range(MAX_ONTOLOGY_SOURCE_TARGETS + 2)
    ]
    signatures.insert(1, dict(signatures[0]))

    types = build_graphiti_types({
        "entity_types": [],
        "edge_types": [{
            "name": "RELATED_TO",
            "attributes": ["reason"],
            "source_targets": signatures,
        }],
    })

    assert len(types["edge_type_map"]) == MAX_ONTOLOGY_SOURCE_TARGETS
    assert types["edge_type_map"][("Source0", "Target0")] == ["RELATED_TO"]


def test_edge_without_a_usable_signature_falls_back_to_the_wildcard():
    types = build_graphiti_types({
        "entity_types": [],
        "edge_types": [{
            "name": "RELATED_TO",
            "attributes": ["reason"],
            "source_targets": [{"source": "Speaker", "target": None}],
        }],
    })

    assert types["edge_type_map"] == {
        (WILDCARD_ENTITY_TYPE, WILDCARD_ENTITY_TYPE): ["RELATED_TO"]
    }
