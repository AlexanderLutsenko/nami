#!/bin/bash
# Bind-mounts a directory (dst_dir) to a new location (src_dir) on a different
# filesystem, preserving existing contents and persisting the mount across reboots.
#
# Use case: relocating high-traffic directories like /var or /home from a small
# disk to a larger one without changing any paths seen by the OS or processes.
#
# Steps performed:
#   1. Snapshot top-level symlinks in dst_dir (e.g. /var/run -> ../run)
#   2. rsync dst_dir to src_dir (abort on error)
#   3. Restore symlinks that rsync materialized as real directories
#   4. Bind-mount src_dir over dst_dir
#   5. Add the bind-mount to /etc/fstab for persistence across reboots
#   6. Verify the migration
#
# Rollback mode (ROLLBACK=1):
#   Unmounts the bind, removes the fstab entry, restoring the original
#   directory.  The copy on the new disk is left for manual removal.
#
# Cleanup mode (CLEANUP=1):
#   Removes the original data hidden behind the bind-mount using a mount
#   namespace (the live mount is not affected).
#
# Required template vars:
#   src_dir  — new location to store the data (on the large disk)
#   dst_dir  — directory to redirect (e.g. /var)
#
# Usage:
#   src_dir=/media/16TBNVME/var dst_dir=/var ./bind_dir.sh
#
#   # Undo the migration (unmount, restore original):
#   ROLLBACK=1 src_dir=/media/16TBNVME/var dst_dir=/var ./bind_dir.sh
#
#   # After verifying the migration is healthy, free old disk space:
#   CLEANUP=1 src_dir=/media/16TBNVME/var dst_dir=/var ./bind_dir.sh
#
# Notes:
#   - Must be run as a user with passwordless sudo, or as root
#   - Stop services that write heavily to dst_dir before running (e.g. Docker)

set -euo pipefail

# ── Pre-flight checks ──────────────────────────────────────────────────
if [ -z "${src_dir:-}" ] || [ -z "${dst_dir:-}" ]; then
    echo "ERROR: both src_dir and dst_dir must be set." >&2
    echo "Usage: src_dir=/mnt/big/var dst_dir=/var $0" >&2
    exit 1
fi

if [ ! -d "${dst_dir}" ]; then
    echo "ERROR: dst_dir '${dst_dir}' does not exist or is not a directory." >&2
    exit 1
fi

is_bind_active() {
    [ -d "${src_dir}" ] &&
    [ "$(stat -c '%d:%i' "${src_dir}")" = "$(stat -c '%d:%i' "${dst_dir}")" ]
}

# ── Rollback mode ─────────────────────────────────────────────────────
if [ "${ROLLBACK:-0}" = "1" ]; then
    if ! is_bind_active; then
        echo "${dst_dir} is not bind-mounted from ${src_dir}, nothing to undo."
        exit 0
    fi

    echo "Unmounting bind-mount..."
    sudo umount "${dst_dir}"

    if grep -q "${src_dir}" /etc/fstab; then
        sudo sed -i "\|${src_dir}|d" /etc/fstab
        echo "fstab entry removed."
    fi

    echo "Original data at ${dst_dir} is back."
    df -h "${dst_dir}"

    if [ -d "${src_dir}" ]; then
        copy_size=$(sudo du -sh "${src_dir}" | cut -f1)
        echo "Copy still at ${src_dir} (${copy_size}). Remove manually if no longer needed:"
        echo "  sudo rm -rf ${src_dir}"
    fi

    echo "Rollback complete."
    exit 0
fi

# ── Cleanup mode ───────────────────────────────────────────────────────
if [ "${CLEANUP:-0}" = "1" ]; then
    if ! is_bind_active; then
        echo "ERROR: ${dst_dir} is not bind-mounted from ${src_dir}."
        echo "Run the migration first, then re-run with CLEANUP=1." >&2
        exit 1
    fi

    echo "Measuring old data hidden behind the bind-mount..."
    old_size=$(sudo unshare -m bash -c "
        umount '${dst_dir}' 2>/dev/null
        du -sh '${dst_dir}' 2>/dev/null | cut -f1
    " || echo "unknown")
    echo "Old data size: ${old_size}"

    echo "Removing old data behind the bind-mount (live mount is unaffected)..."
    sudo unshare -m bash -c "
        umount '${dst_dir}'
        find '${dst_dir}' -mindepth 1 -delete
    "
    echo "Old data removed."

    df -h "${dst_dir}"
    echo "Cleanup complete."
    exit 0
fi

# ── Idempotency ────────────────────────────────────────────────────────
if is_bind_active; then
    echo "${dst_dir} is already bind-mounted from ${src_dir}, nothing to do."
    exit 0
fi

# ── Same-filesystem guard ─────────────────────────────────────────────
src_ancestor="${src_dir}"
while [ ! -d "${src_ancestor}" ]; do
    src_ancestor=$(dirname "${src_ancestor}")
done

src_dev=$(stat -c '%d' "${src_ancestor}")
dst_dev=$(stat -c '%d' "${dst_dir}")
if [ "${src_dev}" = "${dst_dev}" ]; then
    echo "ERROR: ${src_ancestor} and ${dst_dir} are on the same filesystem." >&2
    echo "  src resolves to: $(df --output=source "${src_ancestor}" | tail -1 | tr -d ' ')" >&2
    echo "  dst resolves to: $(df --output=source "${dst_dir}" | tail -1 | tr -d ' ')" >&2
    echo "  Bind-mounting within the same disk is pointless. Check your mount points." >&2
    exit 1
fi

# ── Disk space check ──────────────────────────────────────────────────
needed_kb=$(sudo du -sk "${dst_dir}" | cut -f1)
avail_kb=$(df --output=avail "${src_ancestor}" | tail -1 | tr -d ' ')
if [ "${avail_kb}" -lt "${needed_kb}" ]; then
    needed_h=$(numfmt --to=iec --from-unit=1024 "${needed_kb}")
    avail_h=$(numfmt --to=iec --from-unit=1024 "${avail_kb}")
    echo "ERROR: not enough space on $(df --output=target "${src_ancestor}" | tail -1 | tr -d ' ')." >&2
    echo "  Need ~${needed_h}, available ${avail_h}." >&2
    exit 1
fi

# ── Snapshot top-level symlinks ────────────────────────────────────────
symlink_snapshot=$(mktemp)
sudo find "${dst_dir}" -maxdepth 1 -type l -printf '%f\t%l\n' > "$symlink_snapshot"

# ── Copy data ─────────────────────────────────────────────────────────
echo "Copying ${dst_dir} → ${src_dir} ..."
sudo mkdir -p "${src_dir}"
sudo chmod --reference="${dst_dir}" "${src_dir}"
sudo chown --reference="${dst_dir}" "${src_dir}"
sudo rsync -a "${dst_dir}/" "${src_dir}/"
echo "Copy complete."

# ── Restore symlinks ─────────────────────────────────────────────────
while IFS=$'\t' read -r name target; do
    dest="${src_dir}/${name}"
    if [ -d "$dest" ] && [ ! -L "$dest" ]; then
        echo "Restoring symlink ${dest} → ${target}"
        sudo rm -rf "$dest"
        sudo ln -sf "$target" "$dest"
    fi
done < "$symlink_snapshot"
rm -f "$symlink_snapshot"

# ── Bind-mount ────────────────────────────────────────────────────────
sudo mount --bind "${src_dir}" "${dst_dir}"

# ── fstab persistence ────────────────────────────────────────────────
if grep -q "${src_dir}" /etc/fstab; then
    echo "fstab entry already exists, skipping."
else
    echo "${src_dir}  ${dst_dir}  none  bind  0  0" | sudo tee -a /etc/fstab
    echo "fstab entry added."
fi

# ── Verification ──────────────────────────────────────────────────────
echo ""
echo "Verifying migration..."
src_count=$(sudo find "${src_dir}" -mindepth 1 | wc -l)
dst_count=$(sudo find "${dst_dir}" -mindepth 1 | wc -l)

if [ "${dst_count}" -eq "${src_count}" ]; then
    echo "OK: bind-mount is consistent (${dst_count} entries)."
else
    echo "WARNING: entry count mismatch — src=${src_count}, dst(mounted)=${dst_count}" >&2
fi

echo ""
echo "Done. ${dst_dir} is now bind-mounted from ${src_dir}."
df -h "${dst_dir}"
echo ""
echo "Original data on the old disk is untouched. Once verified, free the old space with:"
echo "  CLEANUP=1 src_dir=${src_dir} dst_dir=${dst_dir} $0"
