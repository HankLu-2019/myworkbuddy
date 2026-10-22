"""What the runner does (design 04 §4): workspaces, runs, logs, patches, per-job proxy ACLs, and the orphan sweep.

Everything that reaches Docker is built from `presets` or the fixed helpers in `docker_ops`; requests only pick a
preset, allowlisted env, limits at or below the maxima, and extra proxy hosts.
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from mwb_runner import tarsafe
from mwb_runner.config import RunnerSettings
from mwb_runner.presets import RunRejected, RunRequest, check, container_config, volume_name

log = logging.getLogger("mwb_runner")
SHA = re.compile(r"^[0-9a-f]{7,64}$")
RUN_ID = re.compile(r"^[0-9a-f]{16}$")
MWB_NAME = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
IP_RANGE = range(10, 251)
GRACE_S = 60  # 04 §4: a run is killed timeout_s + 60 s after it started


class NotFound(Exception):
    pass


def _parse_time(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-"):
        return None
    # Docker gives nanoseconds; fromisoformat takes up to microseconds.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    return datetime.fromisoformat(value)


class Runner:
    def __init__(self, ops: Any, settings: RunnerSettings) -> None:
        self.ops = ops
        self.s = settings
        self._lock = threading.Lock()
        self._reserved: set[str] = set()
        self.killed: set[str] = set()  # run ids the runner killed (timeouts, disk limit and DELETE)
        self.kill_reason: dict[str, str] = {}  # run id → "timeout" | "disk_limit" | "requested"

    # ---------- workspaces ----------

    def create_workspace(self, job_id: uuid.UUID) -> None:
        name = volume_name(job_id)
        if not self.ops.volume_exists(name):
            self.ops.create_volume(name, {"mwb.job": str(job_id)})

    def put_content(self, job_id: uuid.UUID, body: BinaryIO) -> int:
        """Sanitise the tar (tarsafe) into a temp file, then extract it into the volume."""
        name = volume_name(job_id)
        if not self.ops.volume_exists(name):
            raise NotFound(f"No workspace for job {job_id}.")
        with tempfile.TemporaryFile() as clean:
            count = tarsafe.sanitize(body, clean)
            clean.seek(0)
            self.ops.put_archive(self.s.sandbox_image, name, clean)
        return count

    def patch(self, job_id: uuid.UUID, base_sha: str) -> bytes:
        if not SHA.fullmatch(base_sha):
            raise RunRejected("bad_sha", "base_sha must be a hex commit id.")
        name = volume_name(job_id)
        if not self.ops.volume_exists(name):
            raise NotFound(f"No workspace for job {job_id}.")
        script = 'git -C /workspace add -A && exec git -C /workspace diff --cached --binary "$BASE_SHA"'
        code, out, err = self.ops.run_helper(
            self.s.sandbox_image, name, ["sh", "-c", script], {"BASE_SHA": base_sha, "HOME": "/tmp"},
            max_bytes=self.s.patch_max_bytes,
        )  # fmt: skip
        if len(out) > self.s.patch_max_bytes:
            raise RunRejected("patch_too_large", f"The patch is over {self.s.patch_max_bytes // 1024**2} MB.")
        if code != 0:
            raise RunRejected("patch_failed", f"git diff failed: {err.decode(errors='replace')[-300:]}")
        return out

    def read_mwb(self, job_id: uuid.UUID, name: str) -> bytes:
        """A file under the workspace's .mwb/ (verify results, logs, the agent's notes), at most 1 MB."""
        if not MWB_NAME.fullmatch(name) or ".." in name.split("/"):
            raise RunRejected("bad_path", "Only files under .mwb/ can be read.")
        vol = volume_name(job_id)
        if not self.ops.volume_exists(vol):
            raise NotFound(f"No workspace for job {job_id}.")
        script = 'f="/workspace/.mwb/$MWB_FILE"; [ -f "$f" ] && [ ! -L "$f" ] || exit 44; exec head -c 1048576 "$f"'
        code, out, _err = self.ops.run_helper(
            self.s.sandbox_image, vol, ["sh", "-c", script], {"MWB_FILE": name}, max_bytes=1_048_576
        )
        if code == 44:
            raise NotFound(f".mwb/{name} doesn't exist.")
        if code != 0:
            raise RunRejected("read_failed", f"Couldn't read .mwb/{name}.")
        return out

    def delete_mwb(self, job_id: uuid.UUID, name: str) -> None:
        """Remove one file under .mwb/ (the agent's answered question, 08 §F3). Missing is fine."""
        if not MWB_NAME.fullmatch(name) or ".." in name.split("/"):
            raise RunRejected("bad_path", "Only files under .mwb/ can be removed.")
        vol = volume_name(job_id)
        if not self.ops.volume_exists(vol):
            raise NotFound(f"No workspace for job {job_id}.")
        script = 'f="/workspace/.mwb/$MWB_FILE"; [ -L "$f" ] && exit 45; rm -f -- "$f"'
        code, _out, _err = self.ops.run_helper(self.s.sandbox_image, vol, ["sh", "-c", script], {"MWB_FILE": name},
                                               max_bytes=1024)  # fmt: skip
        if code != 0:
            raise RunRejected("delete_failed", f"Couldn't remove .mwb/{name}.")

    def delete_workspace(self, job_id: uuid.UUID) -> None:
        for c in self.ops.containers(f"mwb.job={job_id}"):
            self._forget(c["Labels"].get("mwb.run", ""))
            self.ops.remove(c["Id"])
        self.ops.remove_volume(volume_name(job_id))

    # ---------- runs ----------

    def _disk_ok(self) -> bool:
        try:
            st = os.statvfs(self.s.docker_root)
        except OSError:
            return True  # not mounted (dev): nothing to check against
        return st.f_bavail / max(st.f_blocks, 1) >= self.s.disk_min_free_ratio

    def _allocate_ip(self) -> str:
        used = self.ops.network_ips(self.s.sandbox_network) | self._reserved
        for n in IP_RANGE:
            ip = f"{self.s.sandbox_prefix}.{n}"
            if ip not in used:
                self._reserved.add(ip)
                return ip
        raise RunRejected("no_capacity", "No free sandbox address; too many runs at once.")

    def start_run(self, req: RunRequest) -> str:
        check(req, self.s)
        if not self.ops.volume_exists(volume_name(req.job_id)):
            raise NotFound(f"No workspace for job {req.job_id}.")
        if not self._disk_ok():
            raise RunRejected("disk_full", "Docker's disk is too full to start a sandbox.")
        run_id = uuid.uuid4().hex[:16]
        with self._lock:
            ip = self._allocate_ip()
        try:
            cfg = container_config(req, run_id=run_id, ip=ip, settings=self.s)
            if req.extra_hosts_acl:
                self._write_acl(run_id, ip, req.preset, req.extra_hosts_acl)
            self.ops.create_and_start(cfg, f"mwb-run-{run_id}")
        except Exception:
            self._forget(run_id)
            raise
        finally:
            self._reserved.discard(ip)
        return run_id

    def _container(self, run_id: str) -> dict[str, Any]:
        if not RUN_ID.fullmatch(run_id):
            raise NotFound("Unknown run.")
        found = self.ops.containers(f"mwb.run={run_id}")
        if not found:
            raise NotFound("Unknown run.")
        return found[0]

    def run_status(self, run_id: str) -> dict[str, Any]:
        c = self._container(run_id)
        info = self.ops.inspect(c["Id"]) or {}
        state = info.get("State", {})
        running = bool(state.get("Running"))
        if not running:
            self._remove_acl(run_id)
        status = "running" if running else ("killed" if run_id in self.killed else "exited")
        return {
            "run_id": run_id,
            "state": status,
            "exit_code": None if running else state.get("ExitCode"),
            "oom_killed": bool(state.get("OOMKilled")),
            "reason": self.kill_reason.get(run_id) if status == "killed" else None,
            "started_at": state.get("StartedAt"),
            "finished_at": None if running else state.get("FinishedAt"),
        }

    def run_logs(self, run_id: str, *, follow: bool) -> Iterator[bytes]:
        c = self._container(run_id)
        for stream, line in self.ops.logs(c["Id"], follow=follow):
            yield (json.dumps({"stream": stream, "line": line}) + "\n").encode()

    def kill_run(self, run_id: str) -> None:
        c = self._container(run_id)
        self.killed.add(run_id)
        self.kill_reason.setdefault(run_id, "requested")
        self.ops.kill(c["Id"])
        self._remove_acl(run_id)

    def remove_run(self, run_id: str) -> None:
        c = self._container(run_id)
        self._forget(run_id)
        self.ops.remove(c["Id"])

    def _forget(self, run_id: str) -> None:
        if run_id:
            self._remove_acl(run_id)

    # ---------- per-run proxy ACLs (04 §1) ----------

    def _acl_path(self, run_id: str) -> Path:
        return Path(self.s.proxy_jobs_dir) / f"run-{run_id}.conf"

    def _write_acl(self, run_id: str, ip: str, preset: str, hosts: list[str]) -> None:
        port = "agent_port" if preset == "agent" else "verify_port"
        name = f"mwbrun_{run_id}"
        text = (
            f"acl {name} src {ip}/32\n"
            f"acl {name}_hosts dstdomain {' '.join(hosts)}\n"
            f"http_access allow {port} {name} {name}_hosts\n"
        )
        path = self._acl_path(run_id)
        path.write_text(text)
        path.chmod(0o644)
        self._reconfigure()

    def _remove_acl(self, run_id: str) -> None:
        path = self._acl_path(run_id)
        if path.exists():
            path.unlink()
            self._reconfigure()

    def proxy_container(self) -> str | None:
        if self.s.proxy_container:
            return self.s.proxy_container
        found = self.ops.find_service("egress-proxy")
        return found[0]["Id"] if len(found) == 1 else None

    def _reconfigure(self) -> None:
        proxy = self.proxy_container()
        if proxy is None:
            raise RuntimeError("Can't find the egress proxy container to reload its ACLs.")
        # Check the config first: Squid exits on a bad one when it reloads. Then SIGHUP to Squid, which is the
        # proxy container's PID 1 (`exec squid -N`), rather than `squid -k reconfigure`, which needs a PID file
        # that Squid 5 doesn't write in no-daemon mode.
        code, out = self.ops.exec_in(proxy, ["squid", "-k", "parse", "-f", "/etc/squid/squid.conf"])
        if code != 0:
            errors = [ln for ln in out.splitlines() if "FATAL" in ln or "ERROR" in ln][-3:]
            raise RuntimeError(f"Squid rejected the run's ACL (exit {code}): {' / '.join(errors) or out[-300:]}")
        self.ops.signal(proxy, "HUP")
        time.sleep(self.s.reconfigure_wait_s)  # Squid applies the new config asynchronously

    # ---------- orphans and timeouts (04 §4) ----------

    def sweep(self, active_jobs: set[str] | None, now: datetime | None = None) -> dict[str, int]:
        """Kill runs past timeout + grace; with the API's answer, remove containers, volumes and ACL files of jobs
        that aren't active. Without an answer (API down), only timeouts are enforced: never delete on a guess."""
        now = now or datetime.now(UTC)
        report = {"timed_out": 0, "too_large": 0, "containers": 0, "volumes": 0, "acls": 0}
        live_runs: set[str] = set()
        for c in self.ops.containers("mwb.job"):
            labels = c.get("Labels") or {}
            run_id, job = labels.get("mwb.run", ""), labels.get("mwb.job", "")
            info = self.ops.inspect(c["Id"]) or {}
            state = info.get("State", {})
            if state.get("Running"):
                live_runs.add(run_id)
                started = _parse_time(state.get("StartedAt"))
                limit = int(labels.get("mwb.timeout_s", "0") or 0) + GRACE_S
                if started and (now - started).total_seconds() > limit:
                    self.killed.add(run_id)
                    self.kill_reason[run_id] = "timeout"
                    self.ops.kill(c["Id"])
                    self._remove_acl(run_id)
                    report["timed_out"] += 1
                elif job and self._workspace_bytes(job, stop_at=self._disk_limit()) > self._disk_limit():
                    self.killed.add(run_id)
                    self.kill_reason[run_id] = "disk_limit"
                    self.ops.kill(c["Id"])
                    self._remove_acl(run_id)
                    report["too_large"] += 1
            if active_jobs is not None and job not in active_jobs:
                self.ops.remove(c["Id"])
                self._remove_acl(run_id)
                live_runs.discard(run_id)
                report["containers"] += 1
        if active_jobs is not None:
            for name, labels in self.ops.volumes("mwb.job"):
                if labels.get("mwb.job") not in active_jobs:
                    self.ops.remove_volume(name)
                    report["volumes"] += 1
        jobs_dir = Path(self.s.proxy_jobs_dir)
        if jobs_dir.is_dir():
            for f in jobs_dir.glob("run-*.conf"):
                if f.stem.removeprefix("run-") not in live_runs:
                    f.unlink(missing_ok=True)
                    report["acls"] += 1
            if report["acls"]:
                try:
                    self._reconfigure()
                except RuntimeError as exc:
                    log.warning("sweep: %s", exc)
        return report

    def _disk_limit(self) -> int:
        return int(self.s.job_disk_gb * 1024**3)

    def _workspace_bytes(self, job_id: str, stop_at: int) -> int:
        """Size of a job's volume, read through the read-only Docker root mount; stops counting past `stop_at`."""
        try:
            data = Path(self.s.docker_root) / "volumes" / volume_name(uuid.UUID(job_id)) / "_data"
        except ValueError:
            return 0
        total = 0
        stack = [data]
        while stack:
            try:
                entries = list(os.scandir(stack.pop()))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_blocks * 512
                except OSError:
                    continue
                if total > stop_at:
                    return total
        return total

    # ---------- health ----------

    def health(self) -> dict[str, Any]:
        checks: dict[str, str] = {}
        if not self.ops.ping():
            return {"ok": False, "checks": {"docker": "Docker isn't reachable"}}
        checks["docker"] = "ok"
        runtimes = set((self.ops.info().get("Runtimes") or {}).keys())
        checks["runtime"] = (
            "ok" if self.s.sandbox_runtime in runtimes else f"Runtime {self.s.sandbox_runtime} isn't installed"
        )
        jobs_dir = Path(self.s.proxy_jobs_dir)
        checks["proxy_config"] = (
            "ok" if jobs_dir.is_dir() and os.access(jobs_dir, os.W_OK) else f"{jobs_dir} isn't writable"
        )
        checks["proxy"] = "ok" if self.proxy_container() else "The egress proxy isn't running"
        checks["disk"] = "ok" if self._disk_ok() else "Docker's disk has less than the minimum free"
        return {"ok": all(v == "ok" for v in checks.values()), "checks": checks, "runtime": self.s.sandbox_runtime}
