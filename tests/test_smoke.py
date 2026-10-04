"""Minimal package import smoke test."""


def test_package_can_be_imported() -> None:
    import treasury_flow_radar

    assert treasury_flow_radar.__version__ == "0.1.0"
