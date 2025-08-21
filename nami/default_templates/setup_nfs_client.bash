# Required variable: NFS_SERVER - server IP or hostname
if [ -z "$NFS_SERVER" ]; then
    echo "Error: NFS_SERVER variable required. Example: --NFS_SERVER=192.168.1.100"
    exit 1
fi

# Optional variables
# MOUNT_DIR default: /mnt/nfs_shared
MOUNT_DIR=${MOUNT_DIR:-/mnt/nfs_shared}
# EXPORT_DIR default: /nfs_shared (must match server export path)
EXPORT_DIR=${EXPORT_DIR:-/nfs_shared}

# If real dir is non-empty and not a mountpoint → do nothing to avoid masking data
if [ -d "$MOUNT_DIR" ] && [ "$(ls -A "$MOUNT_DIR" 2>/dev/null)" ] && ! mountpoint -q "$MOUNT_DIR"; then
    echo "Skip: $MOUNT_DIR exists and is not empty"
    exit 0
fi

# Install NFS client
sudo apt update && sudo apt install -y nfs-common

# Create mount point
sudo mkdir -p "$MOUNT_DIR"
sudo chown $USER:$USER "$MOUNT_DIR"

# Mount the share with NFSv4; if already mounted, attempt remount
if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
    sudo mount -o remount "$MOUNT_DIR" || sudo mount -t nfs -o vers=4 "$NFS_SERVER:$EXPORT_DIR" "$MOUNT_DIR"
else
    sudo mount -t nfs -o vers=4 "$NFS_SERVER:$EXPORT_DIR" "$MOUNT_DIR"
fi

# Ensure no duplicate fstab line before appending
if ! grep -qsE "^[^#].*${NFS_SERVER}:${EXPORT_DIR}[[:space:]]+${MOUNT_DIR}[[:space:]]+nfs" /etc/fstab; then
    echo "$NFS_SERVER:$EXPORT_DIR $MOUNT_DIR nfs defaults 0 0" | sudo tee -a /etc/fstab >/dev/null
fi

# Reload systemd units (avoids hint about changed fstab)
sudo systemctl daemon-reload || true

# Verify mount without failing on unrelated stale mounts
if mountpoint -q "$MOUNT_DIR"; then
    echo "Mounted at $MOUNT_DIR"
    df -h "$MOUNT_DIR" || true
else
    echo "Mount verification failed for $MOUNT_DIR" >&2
    exit 1
fi
