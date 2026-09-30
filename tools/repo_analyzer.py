"""Lightweight repository / codebase analyzer for the Analysis phase.

Replaces the deprecated `code-doc-gen-mcp-server` (no longer published in
https://github.com/awslabs/mcp/tree/main/src). By design this tool never
reaches out to the network or an arbitrary filesystem path on the server -
callers pass in the file contents they want analyzed (e.g. pasted by the
user, or read locally by the coding assistant driving this agent), which
keeps the tool safe to run inside a shared, multi-tenant runtime.
"""
from __future__ import annotations

from collections import Counter

from strands import tool

_EXTENSION_LANGUAGE = {
    ".py": "Python", ".java": "Java", ".cs": "C#", ".js": "JavaScript",
    ".ts": "TypeScript", ".go": "Go", ".rb": "Ruby", ".php": "PHP",
    ".cbl": "COBOL", ".cob": "COBOL", ".jcl": "JCL", ".c": "C", ".cpp": "C++",
    ".sql": "SQL", ".sh": "Shell", ".yaml": "YAML", ".yml": "YAML",
}

_IAC_MARKERS = {
    "Dockerfile": "Docker",
    "docker-compose.yml": "Docker Compose",
    "serverless.yml": "Serverless Framework",
    "template.yaml": "SAM/CloudFormation",
    "cloudformation.yaml": "CloudFormation",
    "main.tf": "Terraform",
}
_IAC_SUFFIXES = {".tf": "Terraform", ".bicep": "Bicep"}

_MANIFEST_FILES = {
    "package.json": "Node.js (npm)", "requirements.txt": "Python (pip)",
    "pyproject.toml": "Python (pep517)", "pom.xml": "Java (Maven)",
    "build.gradle": "Java/Kotlin (Gradle)", "go.mod": "Go modules",
    "gemfile": "Ruby (Bundler)", "composer.json": "PHP (Composer)",
}

_MODERNIZATION_KEYWORDS = {
    "soap": "SOAP web services", "ejb": "EJB (Java EE)", "corba": "CORBA",
    "cobol": "COBOL / mainframe", "mainframe": "Mainframe references",
    "websphere": "IBM WebSphere", "weblogic": "Oracle WebLogic",
    "vmware": "VMware virtualization", "on-premise": "On-premises references",
    "on-prem": "On-premises references",
}

_LARGE_FILE_LINE_THRESHOLD = 800


@tool
def analyze_repository(files: dict[str, str]) -> str:
    """Analyze a set of source files and summarize the existing architecture.

    Use this during the Analysis phase to produce the kind of "current
    state" documentation a senior architect would otherwise write by hand:
    language/framework breakdown, existing infrastructure-as-code, and
    signals of legacy/modernization-worthy technology.

    Args:
        files: Mapping of relative file path -> full file text content.
            Pass in the subset of a repository relevant to the assessment
            (e.g. manifest files, Dockerfiles, and a representative sample
            of source files). Binary files should be omitted by the caller.

    Returns:
        A Markdown report: file/language stats, detected dependency
        manifests, detected infrastructure-as-code, modernization flags,
        and the largest files (possible decomposition candidates).
    """
    if not files:
        raise ValueError("files must contain at least one entry")

    language_counter: Counter[str] = Counter()
    manifests_found: set[str] = set()
    iac_found: set[str] = set()
    modernization_flags: set[str] = set()
    large_files: list[tuple[str, int]] = []

    for path, content in files.items():
        lower_path = path.lower()
        base_name = lower_path.rsplit("/", 1)[-1]

        for suffix, lang in _EXTENSION_LANGUAGE.items():
            if lower_path.endswith(suffix):
                language_counter[lang] += 1
                break

        for marker, kind in _IAC_MARKERS.items():
            if base_name == marker.lower():
                iac_found.add(kind)
        for suffix, kind in _IAC_SUFFIXES.items():
            if lower_path.endswith(suffix):
                iac_found.add(kind)

        if base_name in _MANIFEST_FILES:
            manifests_found.add(_MANIFEST_FILES[base_name])

        content_lower = content.lower()
        for keyword, label in _MODERNIZATION_KEYWORDS.items():
            if keyword in content_lower:
                modernization_flags.add(label)

        line_count = content.count("\n") + 1
        if line_count >= _LARGE_FILE_LINE_THRESHOLD:
            large_files.append((path, line_count))

    large_files.sort(key=lambda item: item[1], reverse=True)

    lines = ["# Repository Analysis", "", f"**Files analyzed:** {len(files)}", ""]

    lines.append("## Language Breakdown")
    if language_counter:
        for lang, count in language_counter.most_common():
            lines.append(f"- {lang}: {count} file(s)")
    else:
        lines.append("- No recognized source file extensions found")

    lines.append("\n## Dependency Manifests Detected")
    if manifests_found:
        lines.extend(f"- {m}" for m in sorted(manifests_found))
    else:
        lines.append("- None found")

    lines.append("\n## Existing Infrastructure as Code")
    if iac_found:
        lines.extend(f"- {i}" for i in sorted(iac_found))
    else:
        lines.append("- None found (candidate for greenfield IaC generation)")

    lines.append("\n## Modernization Signals")
    if modernization_flags:
        lines.extend(f"- \u26a0\ufe0f {f}" for f in sorted(modernization_flags))
    else:
        lines.append("- No obvious legacy-technology keywords detected")

    lines.append("\n## Largest Files (decomposition candidates)")
    if large_files:
        for path, count in large_files[:10]:
            lines.append(f"- {path}: {count} lines")
    else:
        lines.append(f"- No files over {_LARGE_FILE_LINE_THRESHOLD} lines")

    return "\n".join(lines)
