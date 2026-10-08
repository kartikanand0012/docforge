"""The running demo stack (`.deploytest`), driven only through what a client or an operator
has: HTTPS, the admin container's commands, and `docker compose`.

Each run makes two fresh organisations with a key of every role and an administrator who
signs in with a PIN, so runs never share data and nothing needs cleaning up.
"""

import json
import os
import re
import secrets
import ssl
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / ".deploytest"
BASE_URL = os.environ.get("DOCFORGE_SYSTEM_URL", "https://localhost")
ROLES = ("integrator", "reviewer", "admin", "reader")
FINAL_STATUSES = {"extracted", "failed"}

# Run inside the admin container, as the database owner: the organisation, a key of each role
# and an administrator. The keys come back on standard output once, as JSON.
_SETUP = """
import json, sys
from docforge.admin import create_tenant
from docforge.config import get_settings
from docforge.wiring import build_authenticator, build_review

spec = json.loads(sys.stdin.readline())
settings = get_settings()
tenant_id = create_tenant(settings, spec["name"])
auth = build_authenticator(settings)
keys = {role: auth.create_api_key(tenant_id, name=f"system {role}", role=role)
        for role in spec["roles"]}
build_review(settings).add_reviewer(
    tenant_id, name="System admin", email=spec["email"], pin=spec["pin"], role="admin"
)
print(json.dumps({"tenant_id": str(tenant_id), "keys": keys}))
"""


def compose(*args: str, stdin: str | None = None, timeout: float = 300) -> str:
    """`docker compose` on the demo stack; its output, or an error naming the command."""
    result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        ["docker", "compose", *args],  # noqa: S607 - the operator's own docker
        cwd=STACK,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        # The command, not its input: the input can carry a PIN.
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {result.stderr[-2000:]}")
    return result.stdout


_PSQL = ("docker", "compose", "exec", "-T", "postgres", "psql", "-U", "docforge", "-d", "docforge")
_LOCK_KEY = re.compile(r"^[a-z]+:[0-9a-f-]{36}$")  # e.g. index:<document id>


@contextmanager
def advisory_lock(key: str) -> Iterator[None]:
    """Hold the database's advisory lock on `hashtext(key)`, as the service would, so a worker
    that needs it waits at that point; released when the block ends."""
    if not _LOCK_KEY.match(key):
        raise ValueError(f"not a lock key: {key!r}")
    name = f"system_hold_{secrets.token_hex(4)}"  # both checked: safe to put in the SQL
    hold = (
        f"SET application_name = '{name}'; "
        f"SELECT pg_advisory_lock(hashtext('{key}')); SELECT pg_sleep(900);"
    )
    by_name = f"FROM pg_stat_activity WHERE application_name = '{name}'"
    holder = subprocess.Popen(  # noqa: S603 - fixed arguments, no shell
        [*_PSQL, "-c", hold], cwd=STACK, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 30
        while _psql(f"SELECT count(*) {by_name} AND wait_event = 'PgSleep'") != "1":
            if time.monotonic() > deadline:
                raise TimeoutError("the lock was not taken")
            time.sleep(0.2)
        yield
    finally:
        _psql(f"SELECT pg_terminate_backend(pid) {by_name}")
        holder.wait(timeout=30)


def _psql(sql: str) -> str:
    return compose(*_PSQL[2:], "-Atc", sql).strip()


def caddy_root_certificate(directory: Path) -> Path:
    """The stack's own certificate authority (Caddy's, for `localhost`), so TLS is verified."""
    path = directory / "caddy-root.crt"
    path.write_text(
        compose("exec", "-T", "caddy", "cat", "/data/caddy/pki/authorities/local/root.crt")
    )
    return path


@dataclass
class Organisation:
    name: str
    tenant_id: str
    # Never in a test's output: a failing assertion prints the organisation.
    keys: dict[str, str] = field(repr=False)
    email: str
    pin: str = field(repr=False)
    verify: Path
    _clients: dict[str, httpx.Client] = field(default_factory=dict, repr=False)

    def tls(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.verify))

    def client(self, role: str) -> httpx.Client:
        """A client sending the key of that role."""
        if role not in self._clients:
            self._clients[role] = httpx.Client(
                base_url=BASE_URL,
                verify=self.tls(),
                headers={"Authorization": f"Bearer {self.keys[role]}"},
                timeout=60,
            )
        return self._clients[role]

    def session(self) -> httpx.Client:
        """A client signed in as the organisation's administrator (a person, with a PIN)."""
        client = httpx.Client(base_url=BASE_URL, verify=self.tls(), timeout=60)
        reply = client.post(
            "/v1/sessions", json={"tenant": self.name, "email": self.email, "pin": self.pin}
        )
        reply.raise_for_status()
        client.headers["Authorization"] = f"Bearer {reply.json()['token']}"
        self._clients[f"session-{len(self._clients)}"] = client
        return client

    def close(self) -> None:
        for client in self._clients.values():
            client.close()

    # --- documents ---------------------------------------------------------------------

    def upload(
        self, path: Path, doc_type: str = "invoice", *, content: bytes | None = None,
        filename: str | None = None, role: str = "integrator",
    ) -> httpx.Response:  # fmt: skip
        data = path.read_bytes() if content is None else content
        return self.client(role).post(
            "/v1/documents",
            files={"file": (filename or path.name, data)},
            data={"doc_type": doc_type},
        )

    def wait(
        self, document_id: str, until: Callable[[Mapping[str, Any]], bool] | None = None,
        timeout: float = 180, every: float = 1,
    ) -> dict[str, Any]:  # fmt: skip
        """The document once `until` holds (by default: processed or failed for good)."""
        done = until or (lambda d: d["status"] in FINAL_STATUSES and d["stage"] != "retrying")
        deadline = time.monotonic() + timeout
        while True:
            reply = self.client("integrator").get(f"/v1/documents/{document_id}")
            reply.raise_for_status()
            document: dict[str, Any] = reply.json()["document"]
            if done(document):
                return document
            if time.monotonic() > deadline:
                where = f"{document['status']}/{document['stage']}"
                raise TimeoutError(f"document {document_id} still {where}")
            time.sleep(every)


def make_organisation(verify: Path) -> Organisation:
    name = f"system-{secrets.token_hex(4)}"
    email = f"admin@{name}.test"
    pin = "".join(secrets.choice("0123456789") for _ in range(8))
    spec = json.dumps({"name": name, "roles": ROLES, "email": email, "pin": pin})
    out = compose("run", "--rm", "-T", "admin", "python", "-c", _SETUP, stdin=spec + "\n")
    made = json.loads(out.strip().splitlines()[-1])
    return Organisation(name, made["tenant_id"], made["keys"], email, pin, verify)
