# Install NFS server - assumes Ubuntu or Debian
sudo apt update && sudo apt install -y nfs-kernel-server

# Optional variables
# EXPORT_DIR default /nfs_shared
EXPORT_DIR=${EXPORT_DIR:-/nfs_shared}
# ALLOW_EXISTING default false (set to true to export an existing non-empty directory)
ALLOW_EXISTING=${ALLOW_EXISTING:-false}

# Handle export directory
if [ -d "$EXPORT_DIR" ]; then
    if [ "$(ls -A "$EXPORT_DIR" 2>/dev/null)" ]; then
        if [ "$ALLOW_EXISTING" = "true" ] || [ "$ALLOW_EXISTING" = "1" ]; then
            echo "Using existing non-empty directory: $EXPORT_DIR"
        else
            echo "Error: Directory $EXPORT_DIR exists and is not empty. Re-run with --ALLOW_EXISTING=true to export it as-is, or choose a different path."
    exit 1
fi
    else
        echo "Using existing empty directory: $EXPORT_DIR"
    fi
else
    # Create the shared directory and set open permissions by default
    sudo mkdir -p "$EXPORT_DIR"
    sudo chmod 777 "$EXPORT_DIR"  # adjust permissions as needed
fi

# Configure exports - replace * with specific CIDRs for security
# Remove any existing line for this export directory, then append a fresh line
if [ -f /etc/exports ]; then
    sudo sed -i "\|^$EXPORT_DIR\s|d" /etc/exports
fi
echo "$EXPORT_DIR *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee -a /etc/exports > /dev/null

# Export and restart
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server

# Optional firewall rules (ufw)
# sudo ufw allow 2049/tcp
# sudo ufw allow 2049/udp
# sudo ufw allow 111/tcp
# sudo ufw allow 111/udp
# sudo ufw reload

echo "NFS server setup complete. Shared directory: $EXPORT_DIR" 
