"""Unit tests for tools.repo_analyzer (pure text analysis, no I/O)."""
from tools.repo_analyzer import analyze_repository


def test_detects_languages_and_manifests():
    files = {
        "app/main.py": "print('hello')\n",
        "requirements.txt": "flask==3.0.0\n",
        "Dockerfile": "FROM python:3.12-slim\n",
    }
    report = analyze_repository(files)

    assert "Python: 1 file(s)" in report
    assert "Python (pip)" in report
    assert "Docker" in report


def test_flags_modernization_keywords():
    files = {"legacy/OrderService.java": "// calls a SOAP endpoint via EJB\nclass X {}\n"}
    report = analyze_repository(files)

    assert "SOAP web services" in report
    assert "EJB (Java EE)" in report


def test_flags_large_files():
    big_content = "\n".join(f"line {i}" for i in range(1000))
    files = {"src/God.java": big_content}
    report = analyze_repository(files)

    assert "src/God.java" in report
    assert "1000 lines" in report


def test_empty_files_raises():
    try:
        analyze_repository({})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_no_iac_found_message():
    files = {"app/main.py": "print(1)\n"}
    report = analyze_repository(files)
    assert "candidate for greenfield IaC generation" in report
