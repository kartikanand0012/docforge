"""System tests: the running demo stack, checked end to end (`make verify`).

Every test here is marked `system`, so `make test` never needs the stack. When the stack is
not up, the run stops with what to do, rather than skipping: a skipped check proves nothing.
"""

import ssl
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from system.stack import BASE_URL, Organisation, caddy_root_certificate, make_organisation

HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.system)


@pytest.fixture(scope="session")
def verify(tmp_path_factory: pytest.TempPathFactory) -> Path:
    try:
        path = caddy_root_certificate(tmp_path_factory.mktemp("tls"))
        tls = ssl.create_default_context(cafile=str(path))
        httpx.get(f"{BASE_URL}/healthz", verify=tls, timeout=10).raise_for_status()
    except (RuntimeError, httpx.HTTPError) as error:
        pytest.exit(
            f"The demo stack is not answering at {BASE_URL} ({error}). Start it with "
            "`docker compose up -d --wait` in .deploytest, then run `make verify` again.",
            returncode=2,
        )
    return path


@pytest.fixture(scope="session")
def org(verify: Path) -> Iterator[Organisation]:
    organisation = make_organisation(verify)
    yield organisation
    organisation.close()


@pytest.fixture(scope="session")
def other_org(verify: Path) -> Iterator[Organisation]:
    organisation = make_organisation(verify)
    yield organisation
    organisation.close()
