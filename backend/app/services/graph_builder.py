"""
图谱构建服务
接口2：使用进程内 Graphiti 把文本写入本地图数据库
"""

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass

from ..utils.graph_client import add_episode, delete_group, fetch_edges, fetch_nodes
from ..utils.ontology import build_graphiti_types

# 每个文本块都要跑多轮抽取调用（实体、关系、去重、摘要），串行写入会让大文档的建图时间
# 线性增长到数小时。graphiti 客户端本身是并发的，这里用有界线程池并行写入若干块：每个块
# 仍然独立抽取并各自去重，图数据库写入由驱动串行化。
INGEST_WORKERS = 4


@dataclass
class GraphInfo:
    """图谱信息"""
    graph_id: str
    node_count: int
    edge_count: int
    entity_types: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "entity_types": self.entity_types,
        }


class GraphBuilderService:
    """
    图谱构建服务
    负责把文本逐块写入进程内 Graphiti：add_episode 返回后数据即可检索，
    因此不存在提交/等待/续跑等异步批次逻辑。
    """

    def __init__(self):
        # set_ontology 转换出的类型定义，供紧随其后的 add_episodes 复用。
        self._ontology_types: Dict[str, Any] = {}

    def create_graph(self, name: str, *, graph_id: str | None = None) -> str:
        """分配图谱 ID 并返回；不产生任何 I/O。

        graphiti 以 group id 分区存储，图谱不再需要预先创建。`name` 仅用于语义
        标注：本地图数据库里没有独立的“图谱名称”字段。
        """

        return graph_id or f"mirofish_{uuid.uuid4().hex[:16]}"

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]) -> None:
        """转换本体定义并缓存，供随后的 add_episodes 调用；不产生任何 I/O。"""

        self._ontology_types = build_graphiti_types(ontology)

    def add_episodes(
        self,
        graph_id: str,
        chunks: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, Any]:
        """并发写入文本块（INGEST_WORKERS 个工人），全部写完即返回，无需等待远端处理。

        Returns:
            {'item_count': 文本块数, 'node_count': 新增节点数, 'edge_count': 新增边数}
        """

        if not graph_id:
            raise ValueError("graph_id is required")
        if not chunks:
            raise ValueError("At least one text chunk is required")

        entity_types = self._ontology_types.get("entity_types")
        edge_types = self._ontology_types.get("edge_types")
        edge_type_map = self._ontology_types.get("edge_type_map")

        total_chunks = len(chunks)
        node_count = 0
        edge_count = 0
        completed = 0
        workers = max(1, min(INGEST_WORKERS, total_chunks))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    self._add_chunk,
                    graph_id,
                    index,
                    total_chunks,
                    chunk,
                    entity_types,
                    edge_types,
                    edge_type_map,
                )
                for index, chunk in enumerate(chunks, start=1)
            ]
            # as_completed 在主线程里推进进度，回调因此不会被并发调用。
            for future in as_completed(futures):
                result = future.result()
                node_count += result.get("node_count", 0)
                edge_count += result.get("edge_count", 0)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total_chunks)

        return {
            "item_count": total_chunks,
            "node_count": node_count,
            "edge_count": edge_count,
        }

    @staticmethod
    def _add_chunk(
        graph_id: str,
        index: int,
        total_chunks: int,
        chunk: str,
        entity_types: Optional[Dict[str, Any]],
        edge_types: Optional[Dict[str, Any]],
        edge_type_map: Optional[Dict[Any, Any]],
    ) -> Dict[str, Any]:
        """写入单个文本块；名称里的序号让 episode 在图上可追溯。"""

        return add_episode(
            name=f"chunk {index}/{total_chunks}",
            body=chunk,
            group_id=graph_id,
            source_description="MiroFish source document chunk",
            entity_types=entity_types,
            edge_types=edge_types,
            edge_type_map=edge_type_map,
        )

    def _get_graph_info(self, graph_id: str) -> GraphInfo:
        """获取图谱信息"""
        nodes = fetch_nodes(graph_id)
        edges = fetch_edges(graph_id)

        entity_types = set()
        for node in nodes:
            for label in node.get("labels") or []:
                if label not in ("Entity", "Node"):
                    entity_types.add(label)

        return GraphInfo(
            graph_id=graph_id,
            node_count=len(nodes),
            edge_count=len(edges),
            entity_types=sorted(entity_types),
        )

    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        """
        获取完整图谱数据（包含详细信息）

        Args:
            graph_id: 图谱ID

        Returns:
            包含nodes和edges的字典，包括时间信息、属性等详细数据
        """

        nodes = fetch_nodes(graph_id)
        edges = fetch_edges(graph_id)

        # 创建节点映射用于获取节点名称；未出现的 uuid 名称为 None。
        node_map = {node.get("uuid"): node.get("name") for node in nodes}

        nodes_data = [
            {
                "uuid": node.get("uuid"),
                "name": node.get("name"),
                "labels": list(node.get("labels") or []),
                "summary": node.get("summary") or "",
                "attributes": dict(node.get("attributes") or {}),
                "created_at": node.get("created_at"),
            }
            for node in nodes
        ]

        edges_data = [
            {
                "uuid": edge.get("uuid"),
                "name": edge.get("name"),
                "fact": edge.get("fact") or "",
                "fact_type": edge.get("name"),
                "source_node_uuid": edge.get("source_node_uuid"),
                "target_node_uuid": edge.get("target_node_uuid"),
                "source_node_name": node_map.get(edge.get("source_node_uuid")),
                "target_node_name": node_map.get(edge.get("target_node_uuid")),
                "attributes": dict(edge.get("attributes") or {}),
                "created_at": edge.get("created_at"),
                "valid_at": edge.get("valid_at"),
                "invalid_at": edge.get("invalid_at"),
                "expired_at": edge.get("expired_at"),
                "episodes": list(edge.get("episodes") or []),
            }
            for edge in edges
        ]

        return {
            "graph_id": graph_id,
            "nodes": nodes_data,
            "edges": edges_data,
            "node_count": len(nodes_data),
            "edge_count": len(edges_data),
        }

    def delete_graph(self, graph_id: str) -> None:
        """删除图谱（按 group id 清空该图谱的全部数据）"""

        delete_group(graph_id)
