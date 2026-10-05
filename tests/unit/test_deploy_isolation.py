"""The deployed stack keeps LibreOffice away from everything: the converter is on an internal
network only, publishes nothing, gets only its token, and writes only to scratch space."""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
STACKS = [ROOT / "deploy" / "compose.yml"]


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("path", STACKS, ids=lambda p: p.parent.name)
def test_the_converter_has_no_route_out_and_nothing_to_read(path: Path) -> None:
    stack = load(path)
    converter = stack["services"]["converter"]

    assert converter["networks"] == ["convert"]
    assert stack["networks"]["convert"]["internal"] is True
    assert "ports" not in converter
    assert converter["env_file"] == ["converter.env"]  # its token, nothing of app.env
    assert converter["read_only"] is True and converter["tmpfs"]
    assert converter["cap_drop"] == ["ALL"]


@pytest.mark.parametrize("path", STACKS, ids=lambda p: p.parent.name)
def test_only_the_worker_can_reach_the_converter(path: Path) -> None:
    services = load(path)["services"]
    reaching = {name for name, s in services.items() if "convert" in (s.get("networks") or [])}
    assert reaching == {"converter", "worker"}
    assert "default" in services["worker"]["networks"]  # and still its database and storage
