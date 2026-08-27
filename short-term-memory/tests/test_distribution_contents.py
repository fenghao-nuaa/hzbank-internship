from pathlib import Path
from zipfile import ZipFile


def test_built_wheel_contains_only_short_term_package() -> None:
    wheels = sorted(Path("dist").glob("short_term_memory-*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as archive:
        names = archive.namelist()
    assert any(name.startswith("short_term_memory/") for name in names)
    assert not any(name.startswith("dream/") for name in names)
    forbidden = ("curator", "persona", "decision_card", "retrieval", "wiki")
    assert not any(
        term in name.casefold() for term in forbidden for name in names
    )
