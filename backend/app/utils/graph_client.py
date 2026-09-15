"""进程内 Graphiti 图谱客户端。

MiroFish 过去通过 Zep Cloud 的 HTTP Batch API 写入图谱：写入是异步的，调用方必须轮询
直到远端处理完成（旧实现最多等 600 秒）。现在图谱由 graphiti_core 直接在本地图数据库
（Neo4j 或内嵌 Kuzu）中读写，`add_episode` 返回时数据已经可以查询，调用方不再需要任何
等待循环。

Flask 请求处理与后台线程都是同步的，而 graphiti_core 是 asyncio 的，因此所有协程都跑在
一个专用事件循环线程上：驱动连接池与客户端实例都绑定在同一个循环里，跨线程调用通过
`run_sync` 提交并阻塞等待结果。
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Dict, List, Optional, TypeVar

from ..config import Config
from .logger import get_logger

if TYPE_CHECKING:  # 仅用于类型标注，避免导入期依赖
    from graphiti_core import Graphiti

logger = get_logger("mirofish.graph")

T = TypeVar("T")

# 单次图读取的分页大小，与旧 Zep 分页适配器保持同样的量级。
PAGE_SIZE = 200

# 集成策略常量（内部行为，不作为用户可调项暴露）。
MAX_SEARCH_QUERY_CHARS = 400
MAX_SEARCH_RESULTS = 50
DEFAULT_SEARCH_LIMIT = 10

_LOOP_READY_TIMEOUT_SECONDS = 30.0
# 客户端初始化包含建索引等 DDL，正常几秒完成；超时说明后端不可达，此时必须报错而不是挂起。
_CLIENT_BUILD_TIMEOUT_SECONDS = 120.0

# 两个独立的锁：_client_lock 只在构建/丢弃客户端时短暂持有，_loop_lock 负责事件循环
# 的创建。两者都不能跨越 run_sync 持有，否则首次构建客户端会自我死锁。
_client_lock = threading.Lock()
_loop_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_client: Optional["Graphiti"] = None


class GraphClientError(RuntimeError):
    """图谱客户端不可用（配置缺失或后端初始化失败）。"""


def normalize_search_query(query: Any) -> str:
    """返回非空且不超过上限的检索查询串。"""

    normalized = " ".join(str(query or "").split())
    if not normalized:
        raise ValueError("检索查询不能为空")
    return normalized[:MAX_SEARCH_QUERY_CHARS]


def normalize_search_limit(limit: Any) -> int:
    """把检索结果条数收敛到合法区间。"""

    try:
        normalized = int(limit)
    except (TypeError, ValueError):
        normalized = DEFAULT_SEARCH_LIMIT
    if normalized < 1:
        normalized = 1
    return min(normalized, MAX_SEARCH_RESULTS)


def _loop_main(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
    global _loop_thread

    _loop_thread = threading.current_thread()
    asyncio.set_event_loop(loop)
    ready.set()
    loop.run_forever()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop

    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        thread = threading.Thread(
            target=_loop_main,
            args=(loop, ready),
            name="mirofish-graph-loop",
            daemon=True,
        )
        thread.start()
        if not ready.wait(_LOOP_READY_TIMEOUT_SECONDS):
            raise GraphClientError("图谱事件循环启动超时")
        _loop = loop
        return loop


async def _call(factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return await factory()


def _ensure_client() -> "Graphiti":
    """构建并缓存共享客户端。

    必须在事件循环之外的线程上调用：它会阻塞等待事件循环完成构建。协程内部只能通过
    `get_graphiti()` 取用已经建好的实例。
    """

    global _client

    if _client is None:
        with _client_lock:
            if _client is None:
                loop = _ensure_loop()
                future = asyncio.run_coroutine_threadsafe(_build_client(), loop)
                try:
                    _client = future.result(timeout=_CLIENT_BUILD_TIMEOUT_SECONDS)
                except FuturesTimeout as exc:
                    # 否则后端不可达时会让调用线程永久挂起。
                    raise GraphClientError(
                        f"图谱客户端初始化超时（{_CLIENT_BUILD_TIMEOUT_SECONDS:.0f} 秒），"
                        f"请检查 {Config.GRAPH_BACKEND} 后端是否可达"
                    ) from exc
    return _client


def run_sync(factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """在专用事件循环上执行协程工厂并同步等待结果。

    传入工厂（而不是协程对象）是为了让协程在事件循环线程内创建：graphiti 的客户端与驱动
    都绑定到创建它们的事件循环。客户端在这里先于提交创建，避免协程内部再回到事件循环等待。
    """

    if _loop_thread is threading.current_thread():
        raise GraphClientError("不能在图谱事件循环线程内调用 run_sync")

    _ensure_client()

    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(_call(factory), loop)
    return future.result()


async def _build_client() -> "Graphiti":
    """按配置构建 Graphiti 客户端。"""

    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    if not Config.LLM_API_KEY:
        raise GraphClientError("LLM_API_KEY 未配置")

    backend = Config.GRAPH_BACKEND
    if backend == "kuzu":
        from graphiti_core.driver.kuzu_driver import KuzuDriver

        driver = KuzuDriver(db=Config.KUZU_DB_PATH)
    elif backend == "neo4j":
        if not Config.NEO4J_PASSWORD:
            raise GraphClientError("NEO4J_PASSWORD 未配置")
        from graphiti_core.driver.neo4j_driver import Neo4jDriver

        driver = Neo4jDriver(
            uri=Config.NEO4J_URI,
            user=Config.NEO4J_USER,
            password=Config.NEO4J_PASSWORD,
        )
    else:
        raise GraphClientError(f"不支持的图谱后端: {backend}")

    llm_config = LLMConfig(
        api_key=Config.LLM_API_KEY,
        base_url=Config.LLM_BASE_URL,
        model=Config.LLM_MODEL_NAME,
        small_model=Config.LLM_MODEL_NAME,
    )
    # 兼容任意 OpenAI 协议的 /chat/completions 服务：MiroFish 的 LLM_BASE_URL 指向
    # OpenRouter 之类的聚合网关，原生 Responses API 客户端在这里不可用。
    # json_schema 模式会约束模型输出结构，抽取失败率明显低于 json_object。
    llm_client = OpenAIGenericClient(config=llm_config, structured_output_mode="json_schema")
    embedder = OpenAIEmbedder(
        OpenAIEmbedderConfig(
            api_key=Config.LLM_API_KEY,
            base_url=Config.LLM_BASE_URL,
            embedding_model=Config.EMBEDDING_MODEL_NAME,
            embedding_dim=Config.EMBEDDING_DIM,
        )
    )

    client = Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=OpenAIRerankerClient(config=llm_config),
    )
    await client.build_indices_and_constraints()
    logger.info(
        "图谱客户端已就绪 backend=%s model=%s embedder=%s(%s)",
        backend,
        Config.LLM_MODEL_NAME,
        Config.EMBEDDING_MODEL_NAME,
        Config.EMBEDDING_DIM,
    )
    return client


def get_graphiti() -> "Graphiti":
    """返回进程共享的 Graphiti 客户端（首次调用时惰性创建）。"""

    return _ensure_client()


def reset_graph_client() -> None:
    """丢弃缓存的客户端；测试与受控重配置使用。"""

    global _client

    with _client_lock:
        _client = None


def _isoformat(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def node_to_dict(node: Any) -> Dict[str, Any]:
    """把 graphiti 实体节点转成 API/前端沿用的节点字典。"""

    return {
        "uuid": node.uuid,
        "name": node.name,
        "labels": list(node.labels or []),
        "summary": node.summary or "",
        "attributes": dict(node.attributes or {}),
        "created_at": _isoformat(node.created_at),
    }


def edge_to_dict(edge: Any) -> Dict[str, Any]:
    """把 graphiti 实体边转成 API/前端沿用的边字典。"""

    return {
        "uuid": edge.uuid,
        "name": edge.name,
        "fact": edge.fact,
        "source_node_uuid": edge.source_node_uuid,
        "target_node_uuid": edge.target_node_uuid,
        "attributes": dict(edge.attributes or {}),
        "created_at": _isoformat(edge.created_at),
        "valid_at": _isoformat(edge.valid_at),
        "invalid_at": _isoformat(edge.invalid_at),
        "expired_at": _isoformat(edge.expired_at),
        "episodes": list(edge.episodes or []),
    }


def episode_to_dict(episode: Any) -> Dict[str, Any]:
    return {
        "uuid": episode.uuid,
        "name": episode.name,
        "content": episode.content,
        "group_id": episode.group_id,
        "source": getattr(episode.source, "value", episode.source),
        "source_description": episode.source_description,
        "created_at": _isoformat(episode.created_at),
        "valid_at": _isoformat(episode.valid_at),
    }


async def _collect(page_loader: Callable[[Optional[str]], Any]) -> List[Any]:
    """按 uuid 游标翻页，直到取回全部记录。"""

    collected: List[Any] = []
    cursor: Optional[str] = None
    while True:
        page = await page_loader(cursor)
        collected.extend(page)
        if len(page) < PAGE_SIZE:
            return collected
        cursor = page[-1].uuid


async def _add_episode(
    *,
    name: str,
    body: str,
    group_id: str,
    source_description: str,
    reference_time: Optional[datetime],
    source: str,
    entity_types: Optional[Dict[str, Any]],
    edge_types: Optional[Dict[str, Any]],
    edge_type_map: Optional[Dict[Any, Any]],
    uuid: Optional[str],
) -> Dict[str, Any]:
    from graphiti_core.nodes import EpisodeType

    graphiti = get_graphiti()
    try:
        episode_type = EpisodeType[source]
    except KeyError as exc:  # pragma: no cover - 调用方只传 text/message/json
        raise GraphClientError(f"不支持的 episode 类型: {source}") from exc

    result = await graphiti.add_episode(
        name=name,
        episode_body=body,
        source=episode_type,
        source_description=source_description,
        reference_time=reference_time or datetime.now(timezone.utc),
        group_id=group_id,
        uuid=uuid,
        entity_types=entity_types,
        edge_types=edge_types,
        edge_type_map=edge_type_map,
    )
    return {
        "episode": episode_to_dict(result.episode),
        "node_count": len(result.nodes),
        "edge_count": len(result.edges),
        "node_uuids": [node.uuid for node in result.nodes],
        "edge_uuids": [edge.uuid for edge in result.edges],
    }


def add_episode(
    *,
    name: str,
    body: str,
    group_id: str,
    source_description: str = "",
    reference_time: Optional[datetime] = None,
    source: str = "text",
    entity_types: Optional[Dict[str, Any]] = None,
    edge_types: Optional[Dict[str, Any]] = None,
    edge_type_map: Optional[Dict[Any, Any]] = None,
    uuid: Optional[str] = None,
) -> Dict[str, Any]:
    """写入一段文本并同步等待抽取完成。

    返回 `{episode, node_count, edge_count, node_uuids, edge_uuids}`；函数返回后该段文本
    产生的实体与关系已经可以检索。
    """

    return run_sync(
        lambda: _add_episode(
            name=name,
            body=body,
            group_id=group_id,
            source_description=source_description,
            reference_time=reference_time,
            source=source,
            entity_types=entity_types,
            edge_types=edge_types,
            edge_type_map=edge_type_map,
            uuid=uuid,
        )
    )


async def _search_edges(query: str, group_id: str, limit: int) -> List[Dict[str, Any]]:
    graphiti = get_graphiti()
    edges = await graphiti.search(query=query, group_ids=[group_id], num_results=limit)
    return [edge_to_dict(edge) for edge in edges]


def search_edges(query: Any, group_id: str, limit: Any = DEFAULT_SEARCH_LIMIT) -> List[Dict[str, Any]]:
    """语义 + 关键词混合检索关系（事实）。"""

    return run_sync(
        lambda: _search_edges(
            normalize_search_query(query), group_id, normalize_search_limit(limit)
        )
    )


async def _search_nodes(query: str, group_id: str, limit: int) -> List[Dict[str, Any]]:
    from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF

    graphiti = get_graphiti()
    config = NODE_HYBRID_SEARCH_RRF.model_copy(deep=True)
    config.limit = limit
    results = await graphiti.search_(query=query, config=config, group_ids=[group_id])
    return [node_to_dict(node) for node in results.nodes[:limit]]


def search_nodes(query: Any, group_id: str, limit: Any = DEFAULT_SEARCH_LIMIT) -> List[Dict[str, Any]]:
    """语义 + 关键词混合检索实体节点。"""

    return run_sync(
        lambda: _search_nodes(
            normalize_search_query(query), group_id, normalize_search_limit(limit)
        )
    )


async def _fetch_nodes(group_id: str) -> List[Dict[str, Any]]:
    graphiti = get_graphiti()
    driver = graphiti.driver

    async def loader(cursor: Optional[str]):
        return await driver.entity_node_ops.get_by_group_ids(
            driver, [group_id], limit=PAGE_SIZE, uuid_cursor=cursor
        )

    return [node_to_dict(node) for node in await _collect(loader)]


def fetch_nodes(group_id: str) -> List[Dict[str, Any]]:
    """读取某个图谱（group）的全部实体节点。"""

    return run_sync(lambda: _fetch_nodes(group_id))


async def _fetch_edges(group_id: str) -> List[Dict[str, Any]]:
    graphiti = get_graphiti()
    driver = graphiti.driver

    async def loader(cursor: Optional[str]):
        return await driver.entity_edge_ops.get_by_group_ids(
            driver, [group_id], limit=PAGE_SIZE, uuid_cursor=cursor
        )

    return [edge_to_dict(edge) for edge in await _collect(loader)]


def fetch_edges(group_id: str) -> List[Dict[str, Any]]:
    """读取某个图谱（group）的全部关系。"""

    return run_sync(lambda: _fetch_edges(group_id))


async def _get_node(uuid: str) -> Optional[Dict[str, Any]]:
    from graphiti_core.errors import NodeNotFoundError

    graphiti = get_graphiti()
    try:
        node = await graphiti.driver.entity_node_ops.get_by_uuid(graphiti.driver, uuid)
    except NodeNotFoundError:
        return None
    return node_to_dict(node)


def get_node(uuid: str) -> Optional[Dict[str, Any]]:
    """按 uuid 读取单个实体节点，不存在时返回 None。"""

    return run_sync(lambda: _get_node(uuid))


async def _get_node_edges(node_uuid: str) -> List[Dict[str, Any]]:
    graphiti = get_graphiti()
    edges = await graphiti.driver.entity_edge_ops.get_by_node_uuid(graphiti.driver, node_uuid)
    return [edge_to_dict(edge) for edge in edges]


def get_node_edges(node_uuid: str) -> List[Dict[str, Any]]:
    """读取与某个节点相连的全部关系（出边与入边）。"""

    return run_sync(lambda: _get_node_edges(node_uuid))


async def _fetch_episodes(group_id: str, limit: int) -> List[Dict[str, Any]]:
    from graphiti_core.utils.maintenance.graph_data_operations import retrieve_episodes

    graphiti = get_graphiti()
    episodes = await retrieve_episodes(
        graphiti.driver,
        reference_time=datetime.now(timezone.utc),
        last_n=limit,
        group_ids=[group_id],
    )
    return [episode_to_dict(episode) for episode in episodes]


def fetch_episodes(group_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """读取某个图谱最近写入的 episode。"""

    return run_sync(lambda: _fetch_episodes(group_id, int(limit)))


async def _delete_group(group_id: str) -> None:
    from graphiti_core.utils.maintenance.graph_data_operations import clear_data

    graphiti = get_graphiti()
    await clear_data(graphiti.driver, group_ids=[group_id])


def delete_group(group_id: str) -> None:
    """删除某个图谱（group）的全部数据。"""

    run_sync(lambda: _delete_group(group_id))


async def _clear_graph() -> None:
    from graphiti_core.utils.maintenance.graph_data_operations import clear_data

    graphiti = get_graphiti()
    await clear_data(graphiti.driver)
    await graphiti.driver.build_indices_and_constraints()


def clear_graph() -> None:
    """清空整个图数据库（仅测试与受控重置使用）。"""

    run_sync(_clear_graph)
