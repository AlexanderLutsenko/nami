from __future__ import annotations

import concurrent.futures
import random
import re
import time
from typing import Dict, Tuple, List

from ..connection import SystemSSHConnection as Connection

_SSH_RETRIES = 4
_SSH_RETRY_BASE_DELAY = 3


def _sanitize_name_for_path(name: str) -> str:
    """Return a filesystem-safe name derived from an instance name/host.

    Replaces any character that is not alphanumeric or dash with a dash.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "-", name)


def _run_with_ssh_retry(func):
    """Retry on transient SSH failures (e.g. MaxStartups exceeded)."""
    for attempt in range(_SSH_RETRIES):
        try:
            return func()
        except Exception as e:
            if attempt < _SSH_RETRIES - 1 and "kex_exchange_identification" in str(e):
                time.sleep(_SSH_RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 2))
                continue
            raise


def _mount_peer(
    *,
    client_instance: str,
    peer_ip: str,
    peer_label: str,
    export_dir: str,
    mount_base: str,
    nfs_version: str,
    config: dict,
    personal_config: dict | None,
) -> Tuple[str, str, bool, str, str]:
    """Mount a single peer's export on a specific client instance.

    Returns a tuple: (client_instance, mount_dir, success, message)
    """
    mount_dir = f"{mount_base.rstrip('/')}/{_sanitize_name_for_path(peer_label)}"
    nfs_opts = f"vers={nfs_version},soft,timeo=10,retrans=3"

    script = f'''
        set -euo pipefail
        MOUNT_DIR="{mount_dir}"
        PEER_SPEC="{peer_ip}:{export_dir}"

        # If real dir is non-empty and not a mountpoint → do nothing to avoid masking data
        if [ -d "$MOUNT_DIR" ] && [ "$(ls -A \"$MOUNT_DIR\" 2>/dev/null)" ] && ! mountpoint -q "$MOUNT_DIR"; then
            echo "Skip: $MOUNT_DIR exists and is not empty";
            exit 0
        fi

        sudo mkdir -p "$MOUNT_DIR"

        # Always do a full unmount + mount cycle to pick up server-side export changes
        if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
            sudo umount -f "$MOUNT_DIR" 2>/dev/null || sudo umount -l "$MOUNT_DIR" 2>/dev/null || true
            sleep 1
        fi
        sudo mount -t nfs -o {nfs_opts} "$PEER_SPEC" "$MOUNT_DIR"

        # Update fstab: remove any line for this mountpoint, then append the current spec
        if [ -f /etc/fstab ]; then
            sudo sed -i "\\| {mount_dir} nfs |d" /etc/fstab
        fi
        if ! grep -qsF "{peer_ip}:{export_dir} {mount_dir} nfs" /etc/fstab 2>/dev/null; then
            echo "{peer_ip}:{export_dir} {mount_dir} nfs {nfs_opts},_netdev 0 0" | sudo tee -a /etc/fstab >/dev/null
        fi
        sudo systemctl daemon-reload || true

        # Verify
        if mountpoint -q "$MOUNT_DIR"; then
            df -h "$MOUNT_DIR" || true
            exit 0
        fi
        echo "❌ Mount verification failed for $MOUNT_DIR" >&2
        exit 1
    '''

    try:
        def do_mount():
            with Connection(client_instance, config, personal_config=personal_config) as conn:
                return conn.run(script, capture=True)
        result = _run_with_ssh_retry(do_mount)
        return (client_instance, mount_dir, True, "mounted", (result.stdout or ""))
    except Exception as e:
        return (client_instance, mount_dir, False, str(e), "")


def _setup_server_export(
    *,
    instance_name: str,
    export_dir: str,
    peer_ips: List[str],
    config: dict,
    personal_config: dict | None,
) -> Tuple[str, bool, str, str]:
    """Ensure NFS server is installed and export_dir is exported on instance.

    Also opens TCP port 2049 in ufw (if active) for each peer IP so that
    cross-instance NFS mounts are not blocked by host firewalls.
    """
    peer_ips_str = " ".join(peer_ips)
    script = f'''
        set -euo pipefail
        EXPORT_DIR="{export_dir}"

        sudo apt update && sudo apt install -y nfs-kernel-server

        if [ -d "$EXPORT_DIR" ]; then
            if [ "$(ls -A \"$EXPORT_DIR\" 2>/dev/null)" ]; then
                echo "Using existing non-empty directory: $EXPORT_DIR"
            else
                echo "Using existing empty directory: $EXPORT_DIR"
            fi
        else
            sudo mkdir -p "$EXPORT_DIR"
            sudo chmod 777 "$EXPORT_DIR"
        fi

        # NFS cannot follow absolute symlinks that point to other filesystems;
        # the client resolves them locally and fails.  Replace such symlinks
        # with bind mounts so NFS can serve the content properly.
        EXPORT_DEV=$(stat -c %d "$EXPORT_DIR")
        for LINK in $(find "$EXPORT_DIR" -maxdepth 1 -type l 2>/dev/null); do
            TARGET=$(readlink -f "$LINK" 2>/dev/null || true)
            if [ -d "$TARGET" ]; then
                TARGET_DEV=$(stat -c %d "$TARGET" 2>/dev/null || echo "$EXPORT_DEV")
                if [ "$EXPORT_DEV" != "$TARGET_DEV" ] && ! mountpoint -q "$LINK" 2>/dev/null; then
                    sudo rm "$LINK"
                    sudo mkdir -p "$LINK"
                    sudo mount --bind "$TARGET" "$LINK"
                    if ! grep -qsF "$TARGET $LINK none bind" /etc/fstab; then
                        echo "$TARGET $LINK none bind 0 0" | sudo tee -a /etc/fstab >/dev/null
                    fi
                    echo "NFS fix: converted symlink $LINK to bind mount of $TARGET"
                fi
            fi
        done

        # Build /etc/exports:
        #   - Root export gets fsid=0 (required for NFSv4 pseudo-root).
        #   - Every block-device-backed sub-mount gets an explicit export with a
        #     unique fsid.  This is necessary because bind mounts on the same
        #     device receive duplicate auto-assigned fsids from the kernel,
        #     which causes stale-handle errors on the client.
        #   - Only /dev/* sources are exported; this automatically excludes
        #     virtual filesystems (proc, sysfs, tmpfs, hugetlbfs, …) and
        #     NFS client mounts (which would create circular re-exports).
        sudo sed -i "\|^${{EXPORT_DIR}}[[:space:]]|d" /etc/exports
        sudo sed -i "/# nami-nfs/d" /etc/exports
        echo "$EXPORT_DIR *(rw,sync,no_subtree_check,no_root_squash,crossmnt,fsid=0) # nami-nfs" | sudo tee -a /etc/exports >/dev/null

        FSID_COUNTER=1
        while read MNT SRC; do
            case "$SRC" in /dev/*) ;; *) continue ;; esac
            echo "$MNT *(rw,sync,no_subtree_check,no_root_squash,fsid=$FSID_COUNTER) # nami-nfs" | sudo tee -a /etc/exports >/dev/null
            echo "NFS: exporting sub-mount $MNT (fsid=$FSID_COUNTER)"
            FSID_COUNTER=$((FSID_COUNTER + 1))
        done < <(findmnt -Rrn -o TARGET,SOURCE "$EXPORT_DIR" | tail -n +2)

        sudo systemctl stop nfs-kernel-server
        sudo exportfs -f
        sudo exportfs -ra
        sudo systemctl start nfs-kernel-server

        # Open NFS port for each peer if ufw is active
        if command -v ufw >/dev/null 2>&1 && sudo ufw status | head -1 | grep -q active; then
            for PEER_IP in {peer_ips_str}; do
                sudo ufw allow from "$PEER_IP" to any port 2049 proto tcp >/dev/null 2>&1 || true
            done
            echo "Firewall: allowed port 2049/tcp for {len(peer_ips)} peer(s)"
        fi
    '''

    try:
        def do_export():
            with Connection(instance_name, config, personal_config=personal_config) as conn:
                return conn.run(script, capture=True)
        result = _run_with_ssh_retry(do_export)
        return (instance_name, True, "exported", (result.stdout or ""))
    except Exception as e:
        return (instance_name, False, str(e), "")


def mount_full_mesh(
    *,
    instances: List[str],
    export_dir: str = "/",
    mount_base: str = "/mnt/peers",
    nfs_version: str = "4",
    max_workers: int = 24,
    config: dict | None = None,
    personal_config: dict | None = None,
) -> None:
    """Mount a selected set of instances to each other (full mesh) via NFS.

    For each client instance I in the provided list, mounts each peer J (also in
    the provided list, excluding I) at:
        mount_base/safe_label(J)

    - instances: list of instance names to include
    - export_dir: remote path exported on each server (e.g. /nfs_shared or /workspace)
    - mount_base: base directory on the client where peer mounts are created
    - nfs_version: NFS protocol version (default 4)
    - max_workers: concurrency for mounting operations
    """
    if config is None:
        config = {}

    all_instances: Dict[str, Dict[str, object]] = config.get("instances", {})
    if not all_instances:
        print("No instances configured.")
        return

    # Validate the requested subset
    unknown = [n for n in instances if n not in all_instances]
    if unknown:
        raise ValueError(f"Unknown instance names: {', '.join(unknown)}")

    # Build a mapping of instance name -> (ip/host, label) for the selection
    name_to_target: Dict[str, Tuple[str, str]] = {}
    for name in instances:
        conf = all_instances[name]
        host = str(conf.get("host", ""))
        if not host:
            continue
        # Prefer the friendly instance name as label
        name_to_target[name] = (host, name)

    # Prepare tasks: for each client, mount all other peers
    tasks: List[Tuple[str, str, str]] = []  # (client, peer_ip, peer_label)
    for client in name_to_target.keys():
        for peer, (peer_ip, peer_label) in name_to_target.items():
            # Include self as well (loopback NFS). This will also ensure the folder exists.
            tasks.append((client, peer_ip, peer_label))

    if not tasks:
        print("Nothing to do.")
        return

    print("──────────── NFS Full-Mesh Mount ────────────")
    print(f"Export dir : {export_dir}")
    print(f"Mount base : {mount_base}")
    print(f"NFS vers   : {nfs_version}")
    print(f"Clients    : {len(instances)}")
    print(f"Operations : {len(tasks)}")

    successes = 0
    failures = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_desc = {
            pool.submit(
                _mount_peer,
                client_instance=client,
                peer_ip=peer_ip,
                peer_label=peer_label,
                export_dir=export_dir,
                mount_base=mount_base,
                nfs_version=nfs_version,
                config=config,
                personal_config=personal_config,
            ): f"{client} <= {peer_label}({peer_ip})"
            for (client, peer_ip, peer_label) in tasks
        }

        for fut, desc in future_to_desc.items():
            try:
                client, mount_dir, ok, msg, log = fut.result()
                if ok:
                    print(f"✅ {desc} → {mount_dir}")
                    if log and log.strip():
                        # Print remote log output as a block to avoid interleaving
                        print(log.strip())
                        print()
                    successes += 1
                else:
                    print(f"❌ {desc} → {mount_dir}: {msg}")
                    failures += 1
            except Exception as e:  # pragma: no cover - defensive
                print(f"❌ {desc}: {e}")
                failures += 1

    print("──────────── Summary ────────────")
    print(f"✅ Successes: {successes}")
    print(f"❌ Failures : {failures}")
    if failures:
        raise RuntimeError("Some NFS mounts failed. See logs above.")



def setup_and_mount_full_mesh(
    *,
    instances: List[str],
    export_dir: str = "/",
    mount_base: str = "/mnt/peers",
    nfs_version: str = "4",
    max_workers: int = 24,
    config: dict | None = None,
    personal_config: dict | None = None,
) -> None:
    """End-to-end: export on selected servers and mount full mesh among them.

    - instances: names of instances (list)
    - export_dir: path to export on each server
    - mount_base: base directory on clients for peer mounts
    - nfs_version: NFS protocol version for client mounts
    - max_workers: parallelism
    """
    if config is None:
        config = {}

    all_instances: Dict[str, Dict[str, object]] = config.get("instances", {})
    if not all_instances:
        print("No instances configured.")
        return

    # Validate the requested subset
    unknown = [n for n in instances if n not in all_instances]
    if unknown:
        raise ValueError(f"Unknown instance names: {', '.join(unknown)}")

    # Collect all peer IPs so each server can open its firewall for them
    peer_ips: List[str] = []
    for name in instances:
        host = str(all_instances[name].get("host", ""))
        if host:
            peer_ips.append(host)

    print("──────────── NFS Server Exports ────────────")
    print(f"Export dir : {export_dir}")
    print(f"Instances  : {len(instances)} → {', '.join(instances)}")

    srv_success = 0
    srv_fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _setup_server_export,
                instance_name=name,
                export_dir=export_dir,
                peer_ips=peer_ips,
                config=config,
                personal_config=personal_config,
            ): name for name in instances
        }
        for fut, name in futures.items():
            ok_name, ok, msg, log = fut.result()
            if ok:
                print(f"✅ {name}: {msg}")
                if log and log.strip():
                    print(log.strip())
                    print()
                srv_success += 1
            else:
                print(f"❌ {name}: {msg}")
                srv_fail += 1

    print(f"Exports OK : {srv_success}, Failed: {srv_fail}")

    # Proceed to mounting regardless; mounts will fail for non-exporting servers
    mount_full_mesh(
        instances=instances,
        export_dir=export_dir,
        mount_base=mount_base,
        nfs_version=nfs_version,
        max_workers=max_workers,
        config=config,
        personal_config=personal_config,
    )
