"""Unit tests for tools.diagrams (pure text generation, no external deps)."""
from tools.diagrams import generate_architecture_diagram


def test_architecture_diagram_contains_mermaid_block():
    output = generate_architecture_diagram(
        title="Target State",
        nodes=[
            {"id": "user", "label": "End User", "type": "user"},
            {"id": "alb", "label": "ALB", "type": "alb", "group": "Public Subnet"},
            {"id": "app", "label": "App Server", "type": "ec2", "group": "Private Subnet"},
            {"id": "db", "label": "Orders DB", "type": "rds"},
        ],
        edges=[
            {"source": "user", "target": "alb", "label": "HTTPS"},
            {"source": "alb", "target": "app"},
            {"source": "app", "target": "db", "label": "SQL"},
        ],
    )

    assert output.startswith("### Target State")
    assert "```mermaid" in output
    assert "flowchart LR" in output
    assert 'subgraph' in output
    assert "-->" in output
    assert "```xml" in output
    assert "app.diagrams.net" in output


def test_drawio_xml_uses_aws_icons_and_valid_structure():
    output = generate_architecture_diagram(
        title="Static Website",
        nodes=[
            {"id": "user", "label": "End User", "type": "user"},
            {"id": "cf", "label": "CloudFront", "type": "cloudfront"},
            {"id": "bucket", "label": "Website Bucket", "type": "s3", "group": "Storage"},
        ],
        edges=[
            {"source": "user", "target": "cf", "label": "HTTPS"},
            {"source": "cf", "target": "bucket"},
        ],
    )

    assert "<mxfile" in output
    assert "mxgraph.aws4.cloudfront" in output
    assert "mxgraph.aws4.s3" in output
    assert "mxgraph.aws4.user" in output
    assert 'value="Storage"' in output
    assert output.count("edge=\"1\"") == 2


def test_drawio_export_skipped_for_sequence_diagrams():
    output = generate_architecture_diagram(
        title="Checkout Flow",
        nodes=[{"id": "client", "label": "Client"}, {"id": "api", "label": "API"}],
        edges=[{"source": "client", "target": "api", "label": "POST /checkout"}],
        diagram_type="sequence",
    )
    assert "<mxfile" not in output


def test_sequence_diagram():
    output = generate_architecture_diagram(
        title="Checkout Flow",
        nodes=[{"id": "client", "label": "Client"}, {"id": "api", "label": "API"}],
        edges=[{"source": "client", "target": "api", "label": "POST /checkout"}],
        diagram_type="sequence",
    )
    assert "sequenceDiagram" in output
    assert "participant client as Client" in output


def test_invalid_diagram_type_raises():
    try:
        generate_architecture_diagram(
            title="Bad", nodes=[{"id": "a", "label": "A"}], edges=[], diagram_type="pie"
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_empty_nodes_raises():
    try:
        generate_architecture_diagram(title="Empty", nodes=[], edges=[])
        assert False, "expected ValueError"
    except ValueError:
        pass
