"""Helpers for validating LLM-generated ontology structures."""

import re
from typing import Any, Dict, List, Optional, Tuple, Type

from pydantic import Field, create_model


MAX_ONTOLOGY_TYPES = 10
MAX_ONTOLOGY_ATTRIBUTES = 10
MAX_ONTOLOGY_SOURCE_TARGETS = 10
RESERVED_ONTOLOGY_ATTRIBUTE_NAMES = frozenset({
    "uuid",
    "name",
    "group_id",
    "graph_id",
    "name_embedding",
    "summary",
    "created_at",
})

_FALLBACK_ATTRIBUTE = {
    "name": "details",
    "type": "text",
    "description": "Additional details about this ontology type.",
}


def normalize_ontology_attribute(attribute: Any) -> Optional[Dict[str, Any]]:
    """Return a safe attribute definition, or ``None`` for unusable values."""

    if isinstance(attribute, str):
        if not attribute.strip():
            return None
        return {
            "name": attribute,
            "type": "text",
            "description": attribute,
        }

    if not isinstance(attribute, dict):
        return None

    name = attribute.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    normalized = dict(attribute)
    description = normalized.get("description")
    if not isinstance(description, str) or not description:
        normalized["description"] = name
    return normalized


def normalize_ontology_attributes(attributes: Any) -> List[Dict[str, Any]]:
    """Return a non-empty attribute list within service limits."""

    if not isinstance(attributes, list):
        attributes = []

    normalized_attributes: List[Dict[str, Any]] = []
    for attribute in attributes:
        normalized = normalize_ontology_attribute(attribute)
        if normalized is None:
            continue
        normalized_attributes.append(normalized)
        if len(normalized_attributes) == MAX_ONTOLOGY_ATTRIBUTES:
            break

    if not normalized_attributes:
        normalized_attributes.append(dict(_FALLBACK_ATTRIBUTE))

    return normalized_attributes


def normalize_ontology_source_targets(
    source_targets: Any,
    *,
    limit: int | None = MAX_ONTOLOGY_SOURCE_TARGETS,
) -> List[Dict[str, str]]:
    """Return unique, structurally valid source-target pairs within service limits."""

    if not isinstance(source_targets, list):
        return []

    normalized_targets: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source_target in source_targets:
        if not isinstance(source_target, dict):
            continue
        source = source_target.get("source")
        target = source_target.get("target")
        if not isinstance(source, str) or not source.strip():
            continue
        if not isinstance(target, str) or not target.strip():
            continue

        pair = (source.strip(), target.strip())
        if pair in seen:
            continue
        seen.add(pair)
        normalized_targets.append({"source": pair[0], "target": pair[1]})
        if limit is not None and len(normalized_targets) == limit:
            break

    return normalized_targets


# 本体声明的属性类型 -> pydantic 字段类型；未知类型一律退化为 str。
_ATTRIBUTE_TYPES: Dict[str, Any] = {
    "text": str,
    "string": str,
    "str": str,
    "integer": int,
    "int": int,
    "number": float,
    "float": float,
    "double": float,
    "boolean": bool,
    "bool": bool,
    "list": List[str],
    "array": List[str],
    "object": Dict[str, Any],
    "dict": Dict[str, Any],
    "json": Dict[str, Any],
}

# 没有签名信息的边类型使用的通配签名。
WILDCARD_ENTITY_TYPE = "Entity"


def _safe_attribute_name(attribute_name: str) -> str:
    """把与图谱保留字段冲突的属性名改写成不会互相覆盖的名称。"""

    if attribute_name.lower() in RESERVED_ONTOLOGY_ATTRIBUTE_NAMES:
        return f"entity_{attribute_name}"
    return attribute_name


def _safe_field_name(attribute_name: str, used_names: set[str]) -> str:
    """返回合法的 pydantic 字段名，必要时去重。"""

    candidate = attribute_name if attribute_name.isidentifier() else re.sub(r"\W", "_", attribute_name)
    if not candidate or candidate[0].isdigit():
        candidate = f"attr_{candidate}"
    if not candidate.isidentifier():
        candidate = "attribute"

    unique_name = candidate
    suffix = 2
    while unique_name in used_names:
        unique_name = f"{candidate}_{suffix}"
        suffix += 1
    used_names.add(unique_name)
    return unique_name


def _model_class_name(type_name: str) -> str:
    """返回合法的 pydantic 模型类名；本体里的原始名称仍作为字典键保留。"""

    candidate = re.sub(r"\W", "_", type_name).strip("_")
    if not candidate:
        return "OntologyType"
    if candidate[0].isdigit():
        return f"Type_{candidate}"
    return candidate


def _attribute_model_type(attribute: Dict[str, Any]) -> Any:
    """按本体声明的类型取字段类型，未知类型安全退化为 str。"""

    declared = attribute.get("type")
    if isinstance(declared, str):
        return _ATTRIBUTE_TYPES.get(declared.strip().lower(), str)
    return str


def _build_type_model(name: str, description: str, attributes: Any) -> Type[Any]:
    """按本体定义生成一个 pydantic 模型：docstring 即类型描述，字段即属性。"""

    used_names: set[str] = set()
    fields: Dict[str, Any] = {}
    for attribute in normalize_ontology_attributes(attributes):
        field_name = _safe_field_name(
            _safe_attribute_name(attribute["name"]), used_names
        )
        fields[field_name] = (
            Optional[_attribute_model_type(attribute)],
            Field(default=None, description=attribute["description"]),
        )

    return create_model(_model_class_name(name), __doc__=description, **fields)


def build_graphiti_types(ontology: Any) -> Dict[str, Any]:
    """把接口1生成的本体定义转换成 graphiti 需要的类型定义。

    返回 ``entity_types`` / ``edge_types`` / ``edge_type_map``：前两者是类型名到
    pydantic 模型的映射，``edge_type_map`` 把 (source, target) 签名映射到该签名下
    可用的边类型名；没有签名信息的边类型使用 'Entity' 通配签名。
    """

    if not isinstance(ontology, dict):
        ontology = {}

    entity_types: Dict[str, Type[Any]] = {}
    edge_types: Dict[str, Type[Any]] = {}
    edge_type_map: Dict[Tuple[str, str], List[str]] = {}

    entity_defs = ontology.get("entity_types")
    for entity_def in (entity_defs if isinstance(entity_defs, list) else [])[:MAX_ONTOLOGY_TYPES]:
        if not isinstance(entity_def, dict):
            continue
        name = entity_def.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if name in entity_types:
            continue

        description = entity_def.get("description")
        if not isinstance(description, str) or not description.strip():
            description = f"A {name} entity."
        entity_types[name] = _build_type_model(
            name, description, entity_def.get("attributes")
        )

    edge_defs = ontology.get("edge_types")
    for edge_def in (edge_defs if isinstance(edge_defs, list) else [])[:MAX_ONTOLOGY_TYPES]:
        if not isinstance(edge_def, dict):
            continue
        name = edge_def.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if name in edge_types:
            continue

        description = edge_def.get("description")
        if not isinstance(description, str) or not description.strip():
            description = f"A {name} relationship."
        edge_types[name] = _build_type_model(
            name, description, edge_def.get("attributes")
        )

        signatures = normalize_ontology_source_targets(edge_def.get("source_targets"))
        if not signatures:
            signatures = [{"source": WILDCARD_ENTITY_TYPE, "target": WILDCARD_ENTITY_TYPE}]
        for signature in signatures:
            source = signature.get("source") or WILDCARD_ENTITY_TYPE
            target = signature.get("target") or WILDCARD_ENTITY_TYPE
            type_names = edge_type_map.setdefault((source, target), [])
            if name not in type_names:
                type_names.append(name)

    return {
        "entity_types": entity_types,
        "edge_types": edge_types,
        "edge_type_map": edge_type_map,
    }
