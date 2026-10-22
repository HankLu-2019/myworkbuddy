"""The few Docker operations the runner needs, over docker-py. Kept narrow so tests can swap in a fake and so
nothing here can start a container other than from `presets.container_config` or a fixed helper."""

import threading
from collections.abc import Iterator
from typing import Any

import docker
from docker.errors import APIError, NotFound

HELPER_LIMITS = {"mem_limit": "1g", "nano_cpus": 1_000_000_000, "pids_limit": 256}


class DockerOps:
    def __init__(self, client: "docker.DockerClient | None" = None) -> None:
        self.client = client or docker.from_env()
        self.api = self.client.api

    # ---------- daemon ----------

    def ping(self) -> bool:
        try:
            return bool(self.api.ping())
        except Exception:  # noqa: BLE001 - any failure means "not reachable"
            return False

    def info(self) -> dict[str, Any]:
        return self.api.info()

    # ---------- volumes ----------

    def create_volume(self, name: str, labels: dict[str, str]) -> None:
        self.client.volumes.create(name, labels=labels)

    def volume_exists(self, name: str) -> bool:
        try:
            self.client.volumes.get(name)
            return True
        except NotFound:
            return False

    def remove_volume(self, name: str) -> None:
        try:
            self.client.volumes.get(name).remove(force=True)
        except NotFound:
            pass

    def volumes(self, label: str) -> list[tuple[str, dict[str, str]]]:
        return [(v.name, v.attrs.get("Labels") or {}) for v in self.client.volumes.list(filters={"label": label})]

    # ---------- helper containers (no network, uid 1000, read-only root) ----------

    def _helper(self, image: str, volume: str, command: list[str], env: dict[str, str] | None, *, mode: str) -> str:
        host = self.api.create_host_config(
            binds={volume: {"bind": "/workspace", "mode": mode}}, network_mode="none", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges"], tmpfs={"/tmp": "size=512m"}, **HELPER_LIMITS,
        )  # fmt: skip
        created = self.api.create_container(
            image, command=command, user="1000:1000", environment=env or {}, working_dir="/workspace",
            host_config=host, labels={"mwb.helper": "1"}, network_disabled=True,
        )  # fmt: skip
        return created["Id"]

    def put_archive(self, image: str, volume: str, data: Any) -> None:
        """Extract a (sanitised) tar into the volume through a created, never-started helper container."""
        cid = self._helper(image, volume, ["true"], None, mode="rw")
        try:
            if not self.api.put_archive(cid, "/workspace", data):
                raise APIError("put_archive failed")
        finally:
            self.api.remove_container(cid, force=True)

    def run_helper(
        self, image: str, volume: str, command: list[str], env: dict[str, str], *, max_bytes: int, timeout_s: int = 300
    ) -> tuple[int, bytes, bytes]:
        """Run a helper to completion; stdout is cut at max_bytes (the caller decides whether that's an error)."""
        cid = self._helper(image, volume, command, env, mode="rw")
        try:
            self.api.start(cid)
            code = self.api.wait(cid, timeout=timeout_s)["StatusCode"]
            out = b""
            for chunk in self.api.logs(cid, stdout=True, stderr=False, stream=True):
                out += chunk
                if len(out) > max_bytes:
                    break
            err = self.api.logs(cid, stdout=False, stderr=True, tail=50)
            return code, out, err
        finally:
            self.api.remove_container(cid, force=True)

    # ---------- runs ----------

    def create_and_start(self, cfg: dict[str, Any], name: str) -> str:
        host = self.api.create_host_config(**cfg["host_config"])
        ((network, endpoint),) = cfg["endpoint"].items()
        networking = self.api.create_networking_config({network: self.api.create_endpoint_config(**endpoint)})
        created = self.api.create_container(
            cfg["image"], command=cfg["command"], user=cfg["user"], environment=cfg["environment"],
            working_dir=cfg["working_dir"], labels=cfg["labels"], host_config=host, networking_config=networking,
            stop_timeout=cfg["stop_timeout"], name=name,
        )  # fmt: skip
        self.api.start(created["Id"])
        return created["Id"]

    def containers(self, label: str, *, all: bool = True) -> list[dict[str, Any]]:
        return self.api.containers(all=all, filters={"label": label})

    def inspect(self, cid: str) -> dict[str, Any] | None:
        try:
            return self.api.inspect_container(cid)
        except NotFound:
            return None

    def remove(self, cid: str) -> None:
        try:
            self.api.remove_container(cid, force=True)
        except NotFound:
            pass

    def kill(self, cid: str) -> None:
        try:
            self.api.kill(cid)
        except (NotFound, APIError):
            pass

    def logs(self, cid: str, *, follow: bool) -> Iterator[tuple[str, str]]:
        """(stream, line) pairs from stdout and stderr, merged as they arrive."""
        import queue

        q: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=1000)

        def pump(stream: str) -> None:
            try:
                kw = {"stdout": stream == "stdout", "stderr": stream == "stderr"}
                buf = b""
                for chunk in self.api.logs(cid, stream=True, follow=follow, **kw):
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        q.put((stream, line.decode(errors="replace")))
                if buf:
                    q.put((stream, buf.decode(errors="replace")))
            except NotFound:
                pass
            finally:
                q.put(None)

        threads = [threading.Thread(target=pump, args=(s,), daemon=True) for s in ("stdout", "stderr")]
        for t in threads:
            t.start()
        done = 0
        while done < len(threads):
            item = q.get()
            if item is None:
                done += 1
                continue
            yield item

    def exec_in(self, container: str, command: list[str]) -> tuple[int, str]:
        """Run a command in a container; its exit code and the tail of its output."""
        exec_id = self.api.exec_create(container, command, stdout=True, stderr=True)["Id"]
        out = self.api.exec_start(exec_id) or b""
        text = out.decode(errors="replace") if isinstance(out, bytes) else str(out)
        return int(self.api.exec_inspect(exec_id).get("ExitCode") or 0), text[-2000:]

    def signal(self, cid: str, sig: str) -> None:
        self.api.kill(cid, signal=sig)

    def network_ips(self, network: str) -> set[str]:
        try:
            net = self.api.inspect_network(network)
        except NotFound:
            return set()
        return {c["IPv4Address"].split("/")[0] for c in (net.get("Containers") or {}).values() if c.get("IPv4Address")}

    def find_service(self, service: str) -> list[dict[str, Any]]:
        return self.api.containers(filters={"label": f"com.docker.compose.service={service}"})
