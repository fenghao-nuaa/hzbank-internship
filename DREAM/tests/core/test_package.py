from dream import __version__


def test_package_version_is_explicit() -> None:
    assert __version__ == "0.1.0"
