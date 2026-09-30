"""Architecture diagram generation tool.

Replaces the deprecated `aws-diagram-mcp-server` (removed from
https://github.com/awslabs/mcp). Rather than shelling out to the Python
`diagrams` package (which needs a Graphviz binary baked into the runtime
image), this tool emits:
  1. A Mermaid diagram (plain text, renders natively in GitHub/most chat
     UIs/Markdown viewers, no extra system dependencies).
  2. A draw.io (diagrams.net) XML document using the built-in AWS4 icon
     shape library, for a proper box-and-icon architecture diagram - open
     https://app.diagrams.net, File > Import From > Device, and pick the
     saved .drawio file (or paste the XML directly).
"""
from __future__ import annotations

import uuid
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from strands import tool

_VALID_DIAGRAM_TYPES = {"architecture", "flow", "sequence"}

# Maps our generic node "type" to a draw.io AWS4 shape library icon name
# (`mxgraph.aws4.<name>`). Unrecognized types fall back to a generic box.
_AWS4_ICON_MAP = {
    "ec2": "ec2",
    "instance": "instance",
    "s3": "s3",
    "storage": "s3",
    "rds": "rds",
    "database": "rds",
    "db": "rds",
    "dynamo": "dynamodb",
    "dynamodb": "dynamodb",
    "lambda": "lambda",
    "alb": "application_load_balancer",
    "elb": "classic_load_balancer",
    "load_balancer": "application_load_balancer",
    "cloudfront": "cloudfront",
    "cdn": "cloudfront",
    "vpc": "virtual_private_cloud_vpc",
    "api_gateway": "api_gateway",
    "apigateway": "api_gateway",
    "sqs": "simple_queue_service_sqs",
    "sns": "simple_notification_service_sns",
    "ecs": "elastic_container_service",
    "eks": "elastic_kubernetes_service",
    "route53": "route_53",
    "dns": "route_53",
    "iam": "identity_and_access_management_iam",
    "cloudwatch": "cloudwatch_2",
    "elasticache": "elasticache",
    "cache": "elasticache",
    "amplify": "amplify",
    "cognito": "cognito",
    "kinesis": "kinesis",
    "stepfunctions": "step_functions",
    "eventbridge": "eventbridge",
    "waf": "waf",
    "user": "user",
    "client": "user",
    "actor": "user",
    "external": "user",
}


def _sanitize_id(raw_id: str) -> str:
    """Mermaid node ids must be alphanumeric/underscore."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in raw_id)
    return safe or "node"


def _render_architecture_or_flow(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], direction: str
) -> str:
    lines = [f"flowchart {direction}"]
    id_map = {n["id"]: _sanitize_id(n["id"]) for n in nodes}

    groups: dict[str, list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for node in nodes:
        group = node.get("group")
        if group:
            groups.setdefault(group, []).append(node)
        else:
            ungrouped.append(node)

    def node_line(node: dict[str, Any]) -> str:
        label = node.get("label", node["id"]).replace('"', "'")
        node_type = node.get("type", "").lower()
        node_id = id_map[node["id"]]
        # Shape conventions: databases/storage as cylinders, external/users
        # as rounded, everything else as a rectangle.
        if any(k in node_type for k in ("db", "database", "storage", "s3", "rds", "dynamo")):
            return f'    {node_id}[("{label}")]'
        if any(k in node_type for k in ("user", "client", "external", "actor")):
            return f'    {node_id}(("{label}"))'
        return f'    {node_id}["{label}"]'

    for group_name, group_nodes in groups.items():
        safe_group = _sanitize_id(group_name)
        lines.append(f'    subgraph {safe_group} ["{group_name}"]')
        for node in group_nodes:
            lines.append("    " + node_line(node).strip())
        lines.append("    end")

    for node in ungrouped:
        lines.append(node_line(node))

    for edge in edges:
        source = id_map.get(edge["source"], _sanitize_id(edge["source"]))
        target = id_map.get(edge["target"], _sanitize_id(edge["target"]))
        label = edge.get("label")
        arrow = "-->" if not label else f'-- "{label}" -->'
        lines.append(f"    {source} {arrow} {target}")

    return "\n".join(lines)


def _render_sequence(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    lines = ["sequenceDiagram"]
    for node in nodes:
        label = node.get("label", node["id"]).replace('"', "'")
        lines.append(f"    participant {_sanitize_id(node['id'])} as {label}")
    for edge in edges:
        source = _sanitize_id(edge["source"])
        target = _sanitize_id(edge["target"])
        label = edge.get("label", "")
        lines.append(f"    {source}->>+{target}: {label}")
    return "\n".join(lines)


_ICON_SIZE = 78
_ICON_X_SPACING = 160
_ROW_Y_SPACING = 160
_GROUP_PADDING = 30


def _node_vertex_style(node: dict[str, Any]) -> str:
    node_type = node.get("type", "").strip().lower()
    icon = _AWS4_ICON_MAP.get(node_type)
    if icon:
        return (
            "sketch=0;outlineConnect=0;fontColor=#232F3E;gradientColor=none;"
            "fillColor=#E7157B;strokeColor=none;dashed=0;verticalLabelPosition=bottom;"
            "verticalAlign=top;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;"
            f"shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{icon};"
        )
    # Unrecognized type: plain labeled box, still renders fine, just without an icon.
    return "rounded=1;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;fontSize=12;"


def _render_drawio_xml(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], title: str) -> str:
    """Render a draw.io (diagrams.net) mxGraph XML document using the
    built-in AWS4 icon shape library, so opening it in app.diagrams.net
    gives a real box-and-icon AWS architecture diagram."""
    groups: dict[str, list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for node in nodes:
        group = node.get("group")
        if group:
            groups.setdefault(group, []).append(node)
        else:
            ungrouped.append(node)

    cells: list[str] = []
    vertex_id: dict[str, str] = {}
    y = 40

    def add_vertex(node: dict[str, Any], x: int, y: int, parent: str = "1") -> str:
        node_id = f"n{uuid.uuid4().hex[:8]}"
        vertex_id[node["id"]] = node_id
        label = xml_escape(node.get("label", node["id"]))
        style = _node_vertex_style(node)
        cells.append(
            f'<mxCell id="{node_id}" value="{label}" style="{style}" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x}" y="{y}" width="{_ICON_SIZE}" height="{_ICON_SIZE}" as="geometry" /></mxCell>'
        )
        return node_id

    for i, node in enumerate(ungrouped):
        add_vertex(node, x=40 + i * _ICON_X_SPACING, y=y)
    if ungrouped:
        y += _ROW_Y_SPACING

    for group_name, group_nodes in groups.items():
        group_width = max(len(group_nodes) * _ICON_X_SPACING + _GROUP_PADDING, 200)
        group_id = f"g{uuid.uuid4().hex[:8]}"
        cells.append(
            f'<mxCell id="{group_id}" value="{xml_escape(group_name)}" '
            'style="rounded=0;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#232F3E;'
            'verticalAlign=top;fontStyle=1;fontSize=13;" vertex="1" parent="1">'
            f'<mxGeometry x="40" y="{y}" width="{group_width}" height="{_ICON_SIZE + 60}" as="geometry" />'
            "</mxCell>"
        )
        for i, node in enumerate(group_nodes):
            add_vertex(node, x=20 + i * _ICON_X_SPACING, y=40, parent=group_id)
        y += _ROW_Y_SPACING

    for edge in edges:
        source_id = vertex_id.get(edge["source"])
        target_id = vertex_id.get(edge["target"])
        if not source_id or not target_id:
            continue  # skip edges referencing an unknown node id rather than producing invalid XML
        label = xml_escape(edge.get("label", ""))
        edge_id = f"e{uuid.uuid4().hex[:8]}"
        cells.append(
            f'<mxCell id="{edge_id}" value="{label}" style="edgeStyle=orthogonalEdgeStyle;html=1;'
            f'fontSize=11;" edge="1" parent="1" source="{source_id}" target="{target_id}">'
            "<mxGeometry relative=\"1\" as=\"geometry\" /></mxCell>"
        )

    body = "".join(cells)
    return (
        '<mxfile host="app.diagrams.net">'
        f'<diagram name="{xml_escape(title)}">'
        '<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" '
        'arrows="1" fold="1" page="1" pageScale="1" pageWidth="1100" pageHeight="850" math="0" shadow="0">'
        f'<root><mxCell id="0" /><mxCell id="1" parent="0" />{body}</root>'
        "</mxGraphModel></diagram></mxfile>"
    )


@tool
def generate_architecture_diagram(
    title: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    diagram_type: str = "architecture",
    direction: str = "LR",
) -> str:
    """Generate an architecture diagram: a Mermaid preview plus a draw.io
    (diagrams.net) file using real AWS service icons.

    Use this during the Analysis phase to visualize an existing system's
    components and dependencies, and during the Planning phase to visualize
    the proposed target AWS architecture.

    Args:
        title: Human-readable diagram title, e.g. "Target State - 3-Tier Web App".
        nodes: List of components. Each item: {"id": str, "label": str,
            "type": str (e.g. "ec2", "rds", "s3", "user", "alb", "lambda",
            "cloudfront", "dynamodb", "vpc", "sqs", "sns" - see
            tools/diagrams.py's _AWS4_ICON_MAP for the full list; unknown
            types still work, just render as a plain labeled box),
            "group": str optional (e.g. "VPC", "Public Subnet")}.
        edges: List of connections. Each item: {"source": node id,
            "target": node id, "label": str optional (e.g. "HTTPS")}.
        diagram_type: One of "architecture", "flow", or "sequence". Only
            "architecture"/"flow" get a draw.io export; "sequence" is
            Mermaid-only (AWS icons don't apply to sequence diagrams).
        direction: Flowchart direction for architecture/flow diagrams:
            "LR" (left-right, default) or "TB" (top-bottom).

    Returns:
        Markdown containing a ```mermaid preview block plus (for
        architecture/flow diagrams) a ```xml draw.io document - tell the
        user to save that second block as a `.drawio` file and open it at
        https://app.diagrams.net (File > Import From > Device) for a full
        AWS-icon architecture diagram they can edit and export as PNG/SVG.
    """
    if diagram_type not in _VALID_DIAGRAM_TYPES:
        raise ValueError(f"diagram_type must be one of {_VALID_DIAGRAM_TYPES}")
    if not nodes:
        raise ValueError("At least one node is required to generate a diagram")

    if diagram_type == "sequence":
        body = _render_sequence(nodes, edges)
        return f"### {title}\n\n```mermaid\n{body}\n```"

    safe_direction = direction if direction in ("LR", "TB") else "LR"
    mermaid_body = _render_architecture_or_flow(nodes, edges, safe_direction)
    drawio_xml = _render_drawio_xml(nodes, edges, title)
    return (
        f"### {title}\n\n"
        f"```mermaid\n{mermaid_body}\n```\n\n"
        "**draw.io version** (save the block below as `diagram.drawio` and open it at "
        "https://app.diagrams.net for a real AWS-icon architecture diagram):\n\n"
        f"```xml\n{drawio_xml}\n```"
    )
