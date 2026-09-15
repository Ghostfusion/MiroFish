"""图读取失败必须向上传播，不能被伪装成空的实体集合。"""

import pytest

from app.services.graph_entity_reader import GraphEntityReader
from app.utils import graph_client


def _failing(_graph_id):
    raise RuntimeError("graph backend unavailable")


def test_node_read_failure_is_not_reported_as_an_empty_entity_set(monkeypatch):
    monkeypatch.setattr(graph_client, "fetch_nodes", _failing)

    reader = GraphEntityReader()

    with pytest.raises(RuntimeError, match="graph backend unavailable"):
        reader.filter_defined_entities("graph-1", enrich_with_edges=False)


def test_edge_read_failure_is_not_reported_as_an_empty_entity_set(monkeypatch):
    monkeypatch.setattr(
        graph_client,
        "fetch_nodes",
        lambda _graph_id: [{
            "uuid": "node-1",
            "name": "Speaker",
            "labels": ["Entity", "Speaker"],
            "summary": "",
            "attributes": {},
        }],
    )
    monkeypatch.setattr(graph_client, "fetch_edges", _failing)

    reader = GraphEntityReader()

    with pytest.raises(RuntimeError, match="graph backend unavailable"):
        reader.filter_defined_entities("graph-1", enrich_with_edges=True)
