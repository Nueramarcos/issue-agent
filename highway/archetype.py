"""Issue archetype detection — shared by router, factory, and solvability."""

from __future__ import annotations


def detect_archetype(title: str, body: str = "") -> str:
    text = f"{title} {body}".lower()
    if "junk" in text or "accidental" in text:
        return "junk"
    if "license" in text and "file" in text or text.strip().startswith("ensure license"):
        return "license"
    if "license" in text and ".md" not in text:
        return "license"
    if "contributing" in text:
        return "contributing"
    if "security.md" in text or "security policy" in text or "vulnerability" in text:
        return "security"
    if "changelog" in text:
        return "changelog"
    if "codeowners" in text or "code owners" in text:
        return "codeowners"
    if ".gitignore" in text or "gitignore" in text:
        return "gitignore"
    if "py.typed" in text:
        return "py_typed"
    if "pyproject.toml" in text and (
        "metadata" in text or "requires-python" in text or "project metadata" in text
    ):
        return "pyproject_meta"
    if "rustfmt.toml" in text or ("rustfmt" in text and "cargo fmt" in text):
        return "rustfmt"
    if "cargo.toml" in text and ("description" in text or "crate version" in text):
        return "cargo_meta"
    if "lib.rs" in text and ("#[test]" in text or "unit test" in text):
        return "rust_unit_test"
    if "docstring" in text and "__init__.py" in text:
        return "docstring"
    if "__version__" in text or "version constant" in text or "version export" in text:
        return "version"
    if "smoke" in text or ("pytest" in text and "test_" in text):
        return "smoke_tests"
    if "requirements-dev" in text or "requirements_dev" in text:
        return "requirements_dev"
    if "readme" in text or "badge" in text or "shield" in text:
        return "readme"
    if "template" in text or "issue and pr" in text:
        return "templates"
    if "ci" in text or "workflow" in text or "github actions" in text:
        return "ci_workflow"
    return "other"


LANE0_ARCHETYPES = frozenset(
    {
        "license",
        "contributing",
        "security",
        "changelog",
        "codeowners",
        "gitignore",
        "py_typed",
        "pyproject_meta",
        "rustfmt",
        "cargo_meta",
        "rust_unit_test",
        "docstring",
        "junk",
        "version",
        "requirements_dev",
        "smoke_tests",
        "ci_workflow",
        "templates",
    }
)


def is_lane0_candidate(title: str, body: str = "") -> bool:
    return detect_archetype(title, body) in LANE0_ARCHETYPES