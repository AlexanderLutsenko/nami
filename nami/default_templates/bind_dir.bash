#!/bin/bash
# Bind-mounts a directory (dst_dir) to a new location (src_dir) on a different
# filesystem, preserving existing contents and persisting the mount across reboots.
#
# Use case: relocating high-traffic directories like /tmp from a small disk to a
# larger one without changing any paths seen by the OS or running processes.
#
# Steps performed:
#   1. Copy existing contents of dst_dir to src_dir (skipping unreadable system dirs)
#   2. Bind-mount src_dir over dst_dir so all writes go to the new location
#   3. Add the bind-mount to /etc/fstab for persistence across reboots
#   4. Mount the original underlying device separately and wipe the now-hidden
#      contents of dst_dir on it, freeing space on the old disk
#
# Required env vars (no defaults — script will abort if not set):
#   src_dir  — new location to store the data (on the large disk)
#   dst_dir  — directory to redirect (e.g. /tmp)
#
# Usage:
#   src_dir=/media/16TBNVME/tmp dst_dir=/tmp ./bind_dir.sh
#
# Notes:
#   - Must be run as a user with passwordless sudo, or as root
#   - rsync skips unreadable systemd-private dirs (they are recreated by systemd)
#   - The original data on the old disk is deleted after the bind-mount is in place

set -e
# Copy contents to new location
mkdir -p "${src_dir}"
rsync -a --ignore-errors --quiet "${dst_dir}/" "${src_dir}/" || true

# Bind-mount new location over dst
sudo mount --bind "${src_dir}" "${dst_dir}"

# Make it persistent
if grep -q "${src_dir}" /etc/fstab; then
    echo "fstab entry already exists, skipping"
else
    echo "${src_dir}  ${dst_dir}  none  bind  0  0" | sudo tee -a /etc/fstab
    echo "fstab entry added"
fi

# Clean up original contents on the old device
# Query / since dst_dir was on the root filesystem before the bind-mount
root_dev=$(findmnt -n -o SOURCE --target / --first-only)
bind_root=$(mktemp -d)
sudo mount "$root_dev" "$bind_root"
sudo rm -rf "$bind_root/${dst_dir}"/*
sudo umount "$bind_root"
rmdir "$bind_root"

echo "Done. ${dst_dir} is now bind-mounted from ${src_dir}:"
df -h "${dst_dir}"