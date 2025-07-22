from __future__ import annotations
import paramiko
import textwrap
from sty import fg, rs, bg
import subprocess
import os
import sys

CHUNK_SIZE = 32*1024


class SSHConnection:

    def __init__(self, instance_name: str, config: dict):
        instance_conf = config.get("instances", {}).get(instance_name, None)
        if not instance_conf:
            print(f"❌ Instance not found: {instance_name}")
            raise KeyError(f"Instance not found: {instance_name}")

        self.instance_name = instance_name

        self.host = instance_conf["host"]
        self.port = instance_conf.get("port", 22)
        self.user = instance_conf.get("user", "root")
        self.key_filename = instance_conf.get("ssh_key", None)

        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        print(f"🔗 Establishing SSH connection to {instance_name} ({self.user}@{self.host}:{self.port}) …")
        self.connect()

    def connect(self):
        self.client.connect(
            hostname=self.host,
            username=self.user,
            port=self.port,
            key_filename=self.key_filename,
            allow_agent=True,    # Enable SSH agent authentication
        )

    def run(self, cmd: str):
        """Execute *cmd* on *ssh*, stream stdout+stderr to console."""

        # get_pty=True is crucial for seeing real-time output from commands
        # that buffer their output when not run in an interactive terminal.
        cmd = textwrap.dedent(cmd).strip()
        # Nicely colored command and status output
        print(f"{fg.cyan}{cmd}{rs.all}")

        # Use 'bash -c' to execute the command string.
        cmd = f"bash -c 'set +m; set -e -o pipefail; {cmd}'"
        stdin, stdout, stderr = self.client.exec_command(cmd, get_pty=True)

        import time

        channel = stdout.channel
        output_chunks: list[str] = []

        while True:
            received_any = False

            if channel.recv_ready():
                data: bytes = channel.recv(CHUNK_SIZE)
                if data:
                    text = data.decode(errors="replace")
                    print(text, end="", flush=True)
                    output_chunks.append(text)
                    received_any = True

            if channel.recv_stderr_ready():
                data: bytes = channel.recv_stderr(CHUNK_SIZE)
                if data:
                    text = data.decode(errors="replace")
                    print(text, end="", flush=True)
                    output_chunks.append(text)
                    received_any = True

            if channel.exit_status_ready() and not (channel.recv_ready() or channel.recv_stderr_ready()):
                break

            if not received_any:
                time.sleep(0.01)

        exit_status = channel.recv_exit_status()
        if exit_status != 0:
            raise RuntimeError(f"Remote command failed with exit {exit_status}.")

    def run_interactive(self):
        import termios
        import tty
        import select

        chan = self.client.invoke_shell()
        old_tty = termios.tcgetattr(sys.stdin)

        try:
            tty.setraw(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
            chan.settimeout(0.0)

            while True:
                r, _, _ = select.select([chan, sys.stdin], [], [])

                if chan in r:
                    try:
                        data = chan.recv(CHUNK_SIZE)
                    except Exception:
                        break

                    if not data:
                        break

                    sys.stdout.write(data.decode(errors="replace"))
                    sys.stdout.flush()

                if sys.stdin in r:
                    data = sys.stdin.read(1)
                    if not data:
                        break
                    chan.send(data)

        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
            chan.close()

    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self.client.close()


class SystemSSHConnection:
    """Lightweight SSH helper that shells out to the local ``ssh`` binary."""

    def __init__(self, instance_name: str, config: dict):
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
        self.port = instance_conf.get("port", 22)
        self.user = instance_conf.get("user", "root")
        self.local_port = instance_conf.get("local_port", None)

        self._base_cmd: list[str] = ["ssh", f"-p{self.port}"]
        if self.local_port:
            self._base_cmd.extend(["-L", f"{self.local_port}:localhost:{self.local_port}"])
        self._base_cmd.append("-A")
        self._base_cmd.append(f"{self.user}@{self.host}")

    def run_interactive(self, command: str | None = None) -> None:
        cmd = list(self._base_cmd)
        if command:
            cmd.append(command)
        print(f"🔗 Executing: {' '.join(cmd)}")
        subprocess.run(cmd)

    def run(self, command: str) -> subprocess.CompletedProcess:
        if self.is_local:
            print(f"{fg.cyan}{command}{rs.all}")
            result = subprocess.run(command, shell=True)
            if result.returncode != 0:
                raise RuntimeError(f"Local command failed with exit {result.returncode}.")
            return result

        print(f"🔗 Establishing SSH connection to {self.instance_name} ({self.user}@{self.host}:{self.port}) …")
        import signal
        cmd_clean = textwrap.dedent(command).strip()
        remote_cmd = f"bash -c 'set +m; set -e -o pipefail; {cmd_clean}'"
        full_cmd = ["ssh", "-tt"] + self._base_cmd[1:] + [remote_cmd]
        print(f"{fg.cyan}{remote_cmd}{rs.all}")
        proc = subprocess.Popen(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        output_chunks: list[str] = []
        interrupted = False
        try:
            for chunk in iter(lambda: proc.stdout.read(CHUNK_SIZE), b""):
                sys.stdout.buffer.write(chunk)
                sys.stdout.flush()
                output_chunks.append(chunk.decode(errors="replace"))
        except KeyboardInterrupt:
            if not interrupted:
                interrupted = True
                proc.send_signal(signal.SIGINT)
                print("⚠️  Sent SIGINT to remote. Press Ctrl-C again to terminate locally.")
                try:
                    proc.wait()
                except KeyboardInterrupt:
                    proc.kill()
                    raise
            else:
                proc.kill()
                raise
        proc.wait()
        exit_status = proc.returncode
        if exit_status != 0:
            raise RuntimeError(f"Remote command failed with exit {exit_status}.")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False 