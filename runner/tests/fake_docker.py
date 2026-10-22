"""An in-memory stand-in for DockerOps, recording what the runner asked for."""

import itertools
from typing import Any

_ids = itertools.count(1)


class FakeOps:
    def __init__(self) -> None:
        self.volumes_: dict[str, dict[str, str]] = {}
        self.containers_: dict[str, dict[str, Any]] = {}
        self.archives: list[tuple[str, bytes]] = []
        self.helpers: list[tuple[str, list[str], dict]] = []
        self.helper_result: tuple[int, bytes, bytes] = (0, b"diff --git a/x b/x\n", b"")
        self.execs: list[tuple[str, list[str]]] = []
        self.proxies = [{"Id": "proxy1"}]
        self.alive = True
        self.runtimes = {"runc": {}}
        self.logs_: dict[str, list[tuple[str, str]]] = {}

    def ping(self) -> bool:
        return self.alive

    def info(self) -> dict:
        return {"Runtimes": self.runtimes}

    def create_volume(self, name, labels) -> None:
        self.volumes_[name] = labels

    def volume_exists(self, name) -> bool:
        return name in self.volumes_

    def remove_volume(self, name) -> None:
        self.volumes_.pop(name, None)

    def volumes(self, label) -> list:
        return [(n, lab) for n, lab in self.volumes_.items() if label in lab]

    def put_archive(self, image, volume, data) -> None:
        self.archives.append((volume, data.read()))

    def run_helper(self, image, volume, command, env, *, max_bytes, timeout_s=300):
        self.helpers.append((volume, command, env))
        return self.helper_result

    def create_and_start(self, cfg, name) -> str:
        cid = f"c{next(_ids)}"
        self.containers_[cid] = {"Id": cid, "Names": [f"/{name}"], "Labels": cfg["labels"], "cfg": cfg,
                                 "State": {"Running": True, "StartedAt": "2026-10-22T10:00:00.123456789Z"}}  # fmt: skip
        return cid

    def containers(self, label, *, all=True) -> list:
        key, _, value = label.partition("=")
        return [c for c in self.containers_.values() if key in c["Labels"] and (not value or c["Labels"][key] == value)]

    def inspect(self, cid):
        c = self.containers_.get(cid)
        return {"State": c["State"]} if c else None

    def remove(self, cid) -> None:
        self.containers_.pop(cid, None)

    def kill(self, cid) -> None:
        if cid in self.containers_:
            self.containers_[cid]["State"] = {"Running": False, "ExitCode": 137, "FinishedAt": "2026-10-22T10:05:00Z"}

    def logs(self, cid, *, follow):
        yield from self.logs_.get(cid, [])

    def exec_in(self, container, command) -> int:
        self.execs.append((container, command))
        return 0

    def network_ips(self, network) -> set[str]:
        ips = {"172.31.0.2"}
        for c in self.containers_.values():
            if c["State"].get("Running"):
                ips.add(c["cfg"]["endpoint"][network]["ipv4_address"])
        return ips

    def find_service(self, service) -> list:
        return self.proxies if service == "egress-proxy" else []
