from pathlib import Path
import subprocess
import sys
import tomllib


PROJECT_ROOT = Path(__file__).parents[2]


def test_package_exposes_version_and_semantica_target() -> None:
    import semantica_adapter

    assert semantica_adapter.__version__ == "0.1.0"
    assert semantica_adapter.SEMANTICA_COMPAT_VERSION == "0.6.6"


def test_base_package_does_not_import_semantica() -> None:
    script = f"""
import importlib.abc
import sys

sys.path.insert(0, {str(PROJECT_ROOT / 'src')!r})

class BlockSemantica(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'semantica' or fullname.startswith('semantica.'):
            raise RuntimeError('base package imported Semantica')
        return None

sys.meta_path.insert(0, BlockSemantica())
import semantica_adapter
print(semantica_adapter.__version__)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.0"


def test_packaging_has_no_local_semantica_source_override() -> None:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "sources" not in config.get("tool", {}).get("uv", {})
    assert config["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]
    assert all(
        not dependency.startswith("semantica")
        for dependency in config["project"]["dependencies"]
    )
    assert config["project"]["optional-dependencies"]["semantica"] == [
        "semantica==0.6.6"
    ]
    assert "semantica==0.6.6" in config["project"]["optional-dependencies"]["server"]
    assert "/legacy" in config["tool"]["hatch"]["build"]["exclude"]
