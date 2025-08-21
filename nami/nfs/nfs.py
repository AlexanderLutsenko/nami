from __future__ import annotations

import concurrent.futures
import re
from typing import Dict, Tuple, List

from ..connection import SystemSSHConnection as Connection


def _sanitize_name_for_path(name: str) -> str:
    """Return a filesystem-safe name derived from an instance name/host.

    Replaces any character that is not alphanumeric or dash with a dash.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "-", name)


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

    script = f'''
        set -euo pipefail
        MOUNT_DIR="{mount_dir}"
        PEER_SPEC="{peer_ip}:{export_dir}"
        NFS_OPTS="-t nfs -o vers={nfs_version}"
        CURRENT_SPEC="$(findmnt -n -o SOURCE --target "$MOUNT_DIR" 2>/dev/null || true)"

        # If real dir is non-empty and not a mountpoint → do nothing to avoid masking data
        if [ -d "$MOUNT_DIR" ] && [ "$(ls -A \"$MOUNT_DIR\" 2>/dev/null)" ] && ! mountpoint -q "$MOUNT_DIR"; then
            echo "Skip: $MOUNT_DIR exists and is not empty";
            exit 0
        fi

        sudo mkdir -p "$MOUNT_DIR"

        # If already mounted to a different source, unmount first so we can switch exports
        if mountpoint -q "$MOUNT_DIR" 2>/dev/null && [ "x$CURRENT_SPEC" != "x$PEER_SPEC" ]; then
            sudo umount -l "$MOUNT_DIR" || true
        fi

        # Mount (idempotent): if still mounted, attempt remount; else mount fresh
        if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
            sudo mount -o remount "$MOUNT_DIR" || sudo mount $NFS_OPTS "$PEER_SPEC" "$MOUNT_DIR"
        else
            sudo mount $NFS_OPTS "$PEER_SPEC" "$MOUNT_DIR"
        fi

        # Update fstab: remove any line for this mountpoint, then append the current spec
        if [ -f /etc/fstab ]; then
            sudo sed -i "\\| {mount_dir} nfs |d" /etc/fstab
        fi
        if ! grep -qsF "{peer_ip}:{export_dir} {mount_dir} nfs" /etc/fstab 2>/dev/null; then
            echo "{peer_ip}:{export_dir} {mount_dir} nfs defaults 0 0" | sudo tee -a /etc/fstab >/dev/null
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
        with Connection(client_instance, config, personal_config=personal_config) as client_conn:
            result = client_conn.run(script, capture=True)
        return (client_instance, mount_dir, True, "mounted", (result.stdout or ""))
    except Exception as e:
        return (client_instance, mount_dir, False, str(e), "")


def _setup_server_export(
    *,
    instance_name: str,
    export_dir: str,
    config: dict,
    personal_config: dict | None,
) -> Tuple[str, bool, str, str]:
    """Ensure NFS server is installed and export_dir is exported on instance."""
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

        # De-dupe any existing export line for this directory
        if [ -f /etc/exports ]; then
            sudo sed -i "\|^$EXPORT_DIR\\s|d" /etc/exports
        fi
        echo "$EXPORT_DIR *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee -a /etc/exports >/dev/null

        sudo exportfs -ra
        sudo systemctl restart nfs-kernel-server
    '''

    try:
        with Connection(instance_name, config, personal_config=personal_config) as conn:
            result = conn.run(script, capture=True)
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

