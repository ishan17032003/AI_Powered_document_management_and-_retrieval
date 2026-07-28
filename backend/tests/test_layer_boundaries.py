"""Architecture checks for the router/service/repository/util split."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def python_files(folder: str) -> list[Path]:
    return sorted((APP_DIR / folder).glob("*.py"))


class LayerBoundaryTests(unittest.TestCase):
    def test_expected_layers_exist(self) -> None:
        for folder in ("routers", "services", "repositories", "utils"):
            self.assertTrue((APP_DIR / folder / "__init__.py").is_file())

    def test_repositories_do_not_manage_transactions(self) -> None:
        violations: list[str] = []
        for path in python_files("repositories"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"commit", "rollback"}
                ):
                    violations.append(f"{path.name}:{node.lineno}:{node.func.attr}")
        self.assertEqual([], violations)

    def test_routers_do_not_query_the_database_directly(self) -> None:
        violations: list[str] = []
        for path in python_files("routers"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "db"
                    and node.func.attr in {"add", "delete", "execute", "get", "query"}
                ):
                    continue
                violations.append(f"{path.name}:{node.lineno}:{node.func.attr}")
        self.assertEqual([], violations)

    def test_services_do_not_query_the_database_directly(self) -> None:
        violations: list[str] = []
        for path in python_files("services"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "db"
                    and node.func.attr in {"add", "delete", "execute", "get", "query"}
                ):
                    continue
                violations.append(f"{path.name}:{node.lineno}:{node.func.attr}")
        self.assertEqual([], violations)

    def test_routers_do_not_import_repositories(self) -> None:
        violations: list[str] = []
        for path in python_files("routers"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and "repositories" in (
                    node.module or ""
                ):
                    violations.append(f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if ".repositories" in alias.name:
                            violations.append(f"{path.name}:{node.lineno}")
        self.assertEqual([], violations)

    def test_lower_layers_do_not_import_routers(self) -> None:
        violations: list[str] = []
        for folder in ("services", "repositories", "utils"):
            for path in python_files(folder):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and "routers" in (
                        node.module or ""
                    ):
                        violations.append(f"{path.name}:{node.lineno}")
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if ".routers" in alias.name:
                                violations.append(f"{path.name}:{node.lineno}")
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
