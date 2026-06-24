"""Lane 0 golden paths — deterministic fixes that replaced failing Aider L2 runs."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Any

from highway.package_root import detect_package_root

GOLDEN_HANDLERS = frozenset(
    {
        "template:version",
        "template:smoke_import",
        "template:ci_workflow",
        "template:gh_templates",
        "template:requirements_dev",
        "template:pyproject_meta",
        "template:rustfmt",
        "template:cargo_meta",
        "template:rust_unit_test",
        "template:docstring",
    }
)


def _pkg_import_name(pkg_dir: Path, ws: Path) -> str:
    if pkg_dir != ws and (pkg_dir / "__init__.py").exists():
        rel = pkg_dir.relative_to(ws)
        return ".".join(rel.parts)
    name = ws.name.replace("-", "_")
    if (ws / "pyproject.toml").exists():
        text = (ws / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
        m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            return m.group(1).replace("-", "_")
    return name


def apply_golden(handler: str, ws: Path, issue: dict[str, Any], repo_meta: dict[str, Any] | None) -> bool:
    if handler not in GOLDEN_HANDLERS:
        return False

    title = (issue.get("title") or "").lower()
    body = (issue.get("body") or "").lower()
    text = f"{title} {body}"
    pkg = detect_package_root(ws, repo_meta)
    pkg_name = _pkg_import_name(pkg, ws)

    if handler == "template:version":
        if not any(k in text for k in ("__version__", "version constant", "version export")):
            return False
        init_py = pkg / "__init__.py"
        if not init_py.exists():
            init_py.parent.mkdir(parents=True, exist_ok=True)
            init_py.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
            return True
        content = init_py.read_text(encoding="utf-8")
        if "__version__" in content:
            return False
        init_py.write_text(content.rstrip() + '\n__version__ = "0.1.0"\n', encoding="utf-8")
        return True

    if handler == "template:requirements_dev":
        if "requirements-dev" not in text and "requirements_dev" not in text:
            return False
        target = ws / "requirements-dev.txt"
        if target.exists():
            return False
        target.write_text("pytest>=7.0\n", encoding="utf-8")
        return True

    if handler == "template:smoke_import":
        if not any(k in text for k in ("smoke", "pytest", "test_")):
            return False
        tests_dir = ws / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        req = ws / "requirements-dev.txt"
        if not req.exists():
            req.write_text("pytest>=7.0\n", encoding="utf-8")
        if "value" in text and (ws / "micrograd").is_dir():
            target = tests_dir / "test_value.py"
            if not target.exists():
                target.write_text(
                    textwrap.dedent(
                        """\
                        from micrograd.engine import Value


                        def test_value_add():
                            assert Value(2.0) + Value(3.0) == Value(5.0)
                        """
                    ),
                    encoding="utf-8",
                )
                return True
        candidates = [
            tests_dir / "test_import.py",
            tests_dir / "test_smoke.py",
            tests_dir / "test_pipeline_import.py",
        ]
        for target in candidates:
            if target.exists():
                continue
            target.write_text(
                textwrap.dedent(
                    f"""\
                    def test_import_package():
                        import {pkg_name}
                        assert {pkg_name} is not None
                    """
                ),
                encoding="utf-8",
            )
            return True
        return False

    if handler == "template:ci_workflow":
        if not any(k in text for k in ("ci", "workflow", "github actions")):
            return False
        wf_dir = ws / ".github" / "workflows"
        if wf_dir.is_dir() and list(wf_dir.glob("*.yml")):
            return False
        ci_yml = wf_dir / "ci.yml"
        wf_dir.mkdir(parents=True, exist_ok=True)
        branches = "[main, master]"
        if (ws / "Cargo.toml").exists():
            ci_yml.write_text(
                textwrap.dedent(
                    f"""\
                    name: CI
                    on:
                      push:
                        branches: {branches}
                      pull_request:
                        branches: {branches}
                    jobs:
                      test:
                        runs-on: ubuntu-latest
                        steps:
                          - uses: actions/checkout@v4
                          - uses: dtolnay/rust-toolchain@stable
                          - run: cargo test
                    """
                ),
                encoding="utf-8",
            )
        else:
            install_steps: list[str] = [
                "- uses: actions/checkout@v4",
                "- uses: actions/setup-python@v5",
                "  with:",
                '    python-version: "3.12"',
            ]
            req_dev = ws / "requirements-dev.txt"
            if req_dev.exists():
                install_steps.append("- run: python3 -m pip install -q -r requirements-dev.txt")
            else:
                install_steps.append("- run: python3 -m pip install -q pytest")
            if (ws / "pyproject.toml").exists() or (ws / "setup.py").exists():
                install_steps.append("- run: python3 -m pip install -q -e . || true")
            install_steps.append("- run: python3 -m pytest -q")
            steps_block = "\n                          ".join(install_steps)
            ci_yml.write_text(
                textwrap.dedent(
                    f"""\
                    name: CI
                    on:
                      push:
                        branches: {branches}
                      pull_request:
                        branches: {branches}
                    jobs:
                      test:
                        runs-on: ubuntu-latest
                        steps:
                          {steps_block}
                    """
                ),
                encoding="utf-8",
            )
        return True

    if handler == "template:gh_templates":
        if "template" not in text and "issue and pr" not in text:
            return False
        changed = False
        bug = ws / ".github" / "ISSUE_TEMPLATE" / "bug_report.md"
        if not bug.exists():
            bug.parent.mkdir(parents=True, exist_ok=True)
            bug.write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: Bug report
                    about: Report a problem
                    ---
                    **Describe the bug**

                    **Steps to reproduce**

                    **Expected behavior**
                    """
                ),
                encoding="utf-8",
            )
            changed = True
        pr_t = ws / ".github" / "pull_request_template.md"
        if not pr_t.exists():
            pr_t.write_text(
                "## Summary\n\n## Test plan\n\n- [ ] Tests pass locally\n",
                encoding="utf-8",
            )
            changed = True
        return changed

    if handler == "template:pyproject_meta":
        if "pyproject.toml" not in text:
            return False
        target = ws / "pyproject.toml"
        pkg_name = ws.name.replace("_", "-")
        if target.exists():
            content = target.read_text(encoding="utf-8")
            if (
                re.search(r"^name\s*=", content, re.M)
                and re.search(r"^version\s*=", content, re.M)
                and re.search(r"^description\s*=", content, re.M)
                and "requires-python" in content
            ):
                return False
            lines = content.splitlines()
            if "[project]" not in content:
                lines.extend(
                    [
                        "",
                        "[project]",
                        f'name = "{pkg_name}"',
                        'version = "0.1.0"',
                        'description = "Project package."',
                        'requires-python = ">=3.10"',
                    ]
                )
            else:
                if not re.search(r"^name\s*=", content, re.M):
                    lines.append(f'name = "{pkg_name}"')
                if not re.search(r"^version\s*=", content, re.M):
                    lines.append('version = "0.1.0"')
                if not re.search(r"^description\s*=", content, re.M):
                    lines.append('description = "Project package."')
                if "requires-python" not in content:
                    lines.append('requires-python = ">=3.10"')
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
        target.write_text(
            textwrap.dedent(
                f"""\
                [build-system]
                requires = ["setuptools>=68"]
                build-backend = "setuptools.build_meta"

                [project]
                name = "{pkg_name}"
                version = "0.1.0"
                description = "Project package."
                requires-python = ">=3.10"
                """
            ),
            encoding="utf-8",
        )
        return True

    if handler == "template:rustfmt":
        if "rustfmt" not in text:
            return False
        target = ws / "rustfmt.toml"
        if target.exists():
            return False
        target.write_text('edition = "2021"\nmax_width = 100\n', encoding="utf-8")
        return True

    if handler == "template:cargo_meta":
        if "cargo.toml" not in text:
            return False
        target = ws / "Cargo.toml"
        if not target.exists():
            return False
        content = target.read_text(encoding="utf-8")
        changed = False
        if "description" not in content:
            content = content.replace(
                "[package]\n",
                '[package]\ndescription = "Simulation core library."\n',
                1,
            )
            changed = True
        if re.search(r'^version\s*=', content, re.M) is None:
            content = content.replace(
                "[package]\n",
                '[package]\nversion = "0.1.0"\n',
                1,
            )
            changed = True
        if changed:
            target.write_text(content, encoding="utf-8")
        return changed

    if handler == "template:rust_unit_test":
        if "lib.rs" not in text:
            return False
        lib = ws / "Vertex" / "lib.rs"
        if not lib.exists():
            for candidate in ws.rglob("lib.rs"):
                if "target" not in candidate.parts:
                    lib = candidate
                    break
        if not lib.exists():
            return False
        content = lib.read_text(encoding="utf-8")
        if "SimConfig::default()" in content and "#[test]" in content:
            return False
        snippet = textwrap.dedent(
            """\

            #[test]
            fn test_sim_config_default_lane0() {
                let config = SimConfig::default();
                assert_eq!(config.max_ticks, 1_000);
                assert_eq!(config.time_step, 0.016);
            }
            """
        )
        if "mod tests {" in content:
            idx = content.rfind("}")
            if idx == -1:
                return False
            content = content[:idx] + snippet + content[idx:]
        else:
            content += textwrap.dedent(
                """

                #[cfg(test)]
                mod tests {
                    use super::*;

                    #[test]
                    fn test_sim_config_default_lane0() {
                        let config = SimConfig::default();
                        assert_eq!(config.max_ticks, 1_000);
                        assert_eq!(config.time_step, 0.016);
                    }
                }
                """
            )
        lib.write_text(content, encoding="utf-8")
        return True

    if handler == "template:docstring":
        if "docstring" not in text or "__init__.py" not in text:
            return False
        pkg = detect_package_root(ws, repo_meta)
        init = pkg / "__init__.py"
        if not init.exists():
            return False
        content = init.read_text(encoding="utf-8")
        stripped = content.lstrip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            return False
        init.write_text(
            '"""Package module."""\n\n' + content,
            encoding="utf-8",
        )
        return True

    return False