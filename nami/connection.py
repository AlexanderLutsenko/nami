from __future__ import annotations
import textwrap
from sty import fg, rs, bg
import subprocess
import os
import sys

CHUNK_SIZE = 32*1024


class SystemSSHConnection:
    """Lightweight SSH helper that shells out to the local ``ssh`` binary.

    Parameters
    ----------
    instance_name: str
        Name of the instance to connect to.
    config: dict
        Full Nami configuration (``config.yaml``) so the connection can look up
        instance details.
    enable_port_forwarding: bool | int, optional
        Controls local port forwarding behaviour.

        * ``False`` (default) – no port forwarding.
        * ``True`` – forward the port configured for the instance in
          ``config.yaml`` (``local_port`` field).
        * ``<int>`` – forward **this** port instead of the one from the
          instance configuration.  This is handy when you want to temporarily
          override the default port from the CLI via ``--forward 9000`` for
          example.

        Port forwarding is disabled by default so that non-interactive
        operations (rsync, S3 sync, etc.) don’t hang when the local port is
        already occupied.
    """

    def __init__(self, instance_name: str, config: dict, *, enable_port_forwarding: bool | int = False, personal_config: dict | None = None):
        instance_conf = config.get("instances", {}).get(instance_name, {})
        self.is_local = instance_name == "local" or instance_conf.get("host") in {"127.0.0.1", "localhost"}
        self.instance_name = instance_name

        if self.is_local:
            self.host = "127.0.0.1"
            self.port = 0
            self.user = os.getenv("USER", "local")
            self.local_port = None
            self._base_cmd: list[str] = []
            return

        if not instance_conf:
            print(f"❌ Instance not found: {instance_name}")
            raise KeyError(f"Instance not found: {instance_name}")

        self.host = instance_conf["host"]
        # Only use an explicit SSH port when provided. If missing/None, omit -p.
        self.port = instance_conf.get("port")
        self.user = instance_conf.get("user", "root")

        # Determine which (if any) local port should be forwarded.
        if isinstance(enable_port_forwarding, int):
            # An explicit port was provided via the CLI (``--forward 9000``).
            self.local_port = enable_port_forwarding
            _forward_requested = True
        elif enable_port_forwarding is False:
            # No forwarding requested.
            self.local_port = instance_conf.get("local_port", None)
            _forward_requested = False
        else:
            # ``True`` or ``None`` – forward the port from the configuration.
            self.local_port = instance_conf.get("local_port", None)
            _forward_requested = True

        self._base_cmd: list[str] = ["ssh"]
        self._base_cmd.extend(["-o", "StrictHostKeyChecking=no"])  # Auto-accept unknown hosts
        ssh_key = None
        if personal_config:
            ssh_key = personal_config.get('ssh_keys', {}).get(instance_name) or personal_config.get('ssh_key')
            if ssh_key:
                ssh_key = os.path.expanduser(ssh_key)
        self.ssh_key = ssh_key
        if self.ssh_key:
            self._base_cmd.extend(["-i", self.ssh_key])
        if self.port is not None:
            self._base_cmd.append(f"-p{self.port}")
        # Add port-forwarding only when explicitly requested and a port is available.
        if self.local_port and _forward_requested:
            self._base_cmd.extend(["-L", f"{self.local_port}:localhost:{self.local_port}"])
        self._base_cmd.append("-A")
        self._base_cmd.append(f"{self.user}@{self.host}")

    def run_interactive(self, command: str | None = None) -> None:
        cmd = list(self._base_cmd)
        if command:
            cmd.append(command)
        print(f"🔗 Executing: {' '.join(cmd)}")
        return subprocess.run(cmd)

    def _run_process(self, popen_command, *, shell: bool, capture: bool, is_remote: bool, print_command: str | None):
        """Execute a process, stream or capture output, and raise informative errors.

        popen_command can be a command list (preferred) or a string (when shell=True).
        """
        if print_command and not capture:
            print(f"{fg.cyan}{print_command}{rs.all}")

        proc = subprocess.Popen(
            popen_command,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        output_chunks: list[str] = []
        try:
            for chunk in iter(lambda: proc.stdout.read(CHUNK_SIZE), b""):
                if not capture:
                    text_chunk = chunk.decode(errors="replace")
                    if hasattr(sys.stdout, "buffer"):
                        sys.stdout.buffer.write(chunk)
                    else:
                        sys.stdout.write(text_chunk)
                    sys.stdout.flush()
                output_chunks.append(chunk.decode(errors="replace"))
        except KeyboardInterrupt:
            proc.terminate()
            raise
        proc.wait()
        exit_status = proc.returncode
        stdout = ''.join(output_chunks)
        if exit_status != 0:
            label = "Remote" if is_remote else "Local"
            raise RuntimeError(f"{label} command failed with exit {exit_status}. Output: {stdout}")
        return subprocess.CompletedProcess(args=popen_command, returncode=exit_status, stdout=stdout, stderr="")

    def _build_remote_command(self, command: str, capture: bool):
        cmd_clean = textwrap.dedent(command).strip()
        remote_cmd = (
            f"bash -c 'set -e -o pipefail; {cmd_clean}'" if capture
            else f"bash -i -c 'set +m; set -e -o pipefail; {cmd_clean}'"
        )
        ssh_tty_flag = "-T" if capture else "-tt"
        base_without_ssh = self._base_cmd[1:-1]  # drop leading 'ssh' and trailing 'user@host'
        host = self._base_cmd[-1]
        extra_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"] if capture else []
        full_cmd = ["ssh", ssh_tty_flag] + base_without_ssh + extra_opts + [host, remote_cmd]
        return full_cmd, remote_cmd

    def run(self, command: str, capture: bool = False) -> subprocess.CompletedProcess:
        if self.is_local:
            # Wrap in interactive Bash to source .bashrc (consistent with remote behavior)
            popen_command, shell, print_command, is_remote = f"bash -i -c 'set -e -o pipefail; {command}'", True, command, False
        else:
            port_display = f":{self.port}" if self.port is not None else ""
            print(f"🔗 Establishing SSH connection to {self.instance_name} ({self.user}@{self.host}{port_display}) …")
            full_cmd, remote_cmd = self._build_remote_command(command, capture)
            popen_command, shell, print_command, is_remote = full_cmd, False, remote_cmd, True

        return self._run_process(
            popen_command,
            shell=shell,
            capture=capture,
            is_remote=is_remote,
            print_command=print_command,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False 