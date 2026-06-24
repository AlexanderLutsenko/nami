#!/bin/bash
# Consolidate a node onto the fleet storage convention:
#
#   1. Copy data from ${workspace} and ${big_dst} (currently sitting on the small
#      root disk) onto the big disk (currently mounted at ${big_src}).
#   2. Move the big disk from ${big_src} to ${big_dst} (the fleet convention).
#   3. Bind-mount ${big_dst} onto ${workspace} for ease of use, like the other nodes.
#
# Why this exists:
#   On most nodes the 16 TB NVMe pool is mounted at /media/16TBNVME and bind-mounted
#   onto /workspace.  A drifted node may instead have the pool mounted at /data, with
#   real data stranded on the root disk under /media/16TBNVME and /workspace — silently
#   filling the boot drive while the big pool sits nearly empty.  This script folds the
#   stranded data onto the pool and re-points the mounts to match the convention.
#
# Required template vars (all have fleet-default values):
#   big_src    — where the big disk is CURRENTLY mounted        (default /data)
#   big_dst    — where it SHOULD be mounted, per convention      (default /media/16TBNVME)
#   workspace  — directory to bind onto big_dst for ease of use  (default /workspace)
#
# Safety:
#   - Runs read-only and prints a PLAN unless CONFIRM=1 is set.
#   - Copies first, verifies the copy with a second rsync pass, and only THEN empties
#     the source directories — so data always exists on the big disk before removal.
#   - Pre-existing files that would be overwritten during the merge are preserved under
#     a timestamped backup dir on the big disk (never silently clobbered).
#   - Idempotent: re-running after success detects the desired state and exits cleanly.
#
# Usage (on the node):
#   sudo CONFIRM=1 big_src=/data big_dst=/media/16TBNVME workspace=/workspace \
#       bash consolidate_bigdisk.bash
#
#   # Dry analysis only (no changes):
#   sudo bash consolidate_bigdisk.bash
#
# Notes:
#   - Must run as root or a user with passwordless sudo.
#   - Run inside tmux/screen; copying hundreds of GB takes a while.
#   - If this node serves NFS, the server is stopped during the swap and restarted
#     (with `exportfs -ra`) at the end, so /workspace exports keep working.

set -euo pipefail

big_src="${big_src:-/data}"
big_dst="${big_dst:-/media/16TBNVME}"
workspace="${workspace:-/workspace}"
CONFIRM="${CONFIRM:-0}"

# Strip trailing slashes for consistent comparisons.
big_src="${big_src%/}"
big_dst="${big_dst%/}"
workspace="${workspace%/}"

log()  { echo "── $*"; }
warn() { echo "⚠️  $*" >&2; }
die()  { echo "❌ $*" >&2; exit 1; }

is_mountpoint() { mountpoint -q "$1" 2>/dev/null; }

# Same-filesystem test by (device:inode) of the directory roots — also detects an
# active bind mount (bind makes the two roots share device+inode).
same_root() {
    [ -d "$1" ] && [ -d "$2" ] &&
    [ "$(stat -c '%d:%i' "$1" 2>/dev/null)" = "$(stat -c '%d:%i' "$2" 2>/dev/null)" ]
}

# ── Pre-flight ─────────────────────────────────────────────────────────
command -v rsync   >/dev/null || die "rsync is required."
command -v findmnt >/dev/null || die "findmnt (util-linux) is required."

[ -d "$workspace" ] || die "workspace '$workspace' does not exist."

# Locate the big disk: prefer big_dst if it's already the mount, else big_src.
if is_mountpoint "$big_dst"; then
    BIG_MNT="$big_dst"
elif is_mountpoint "$big_src"; then
    BIG_MNT="$big_src"
else
    die "Big disk is not mounted at '$big_src' or '$big_dst'. Mount it first."
fi

BIG_DEV=$(findmnt -no SOURCE "$BIG_MNT")
BIG_FSTYPE=$(findmnt -no FSTYPE "$BIG_MNT")
BIG_UUID=$(findmnt -no UUID "$BIG_MNT" 2>/dev/null || true)
[ -n "$BIG_UUID" ] || BIG_UUID=$(sudo blkid -s UUID -o value "$BIG_DEV" 2>/dev/null || true)
[ -n "$BIG_UUID" ] || die "Could not determine UUID of big disk device '$BIG_DEV'."

# ── Idempotency ────────────────────────────────────────────────────────
if is_mountpoint "$big_dst" \
   && [ "$(findmnt -no SOURCE "$big_dst")" = "$BIG_DEV" ] \
   && same_root "$workspace" "$big_dst"; then
    log "Already consolidated: '$big_dst' is the big disk and '$workspace' is bound to it."
    findmnt "$big_dst" || true
    findmnt "$workspace" || true
    exit 0
fi

# ── Plan ───────────────────────────────────────────────────────────────
echo "════════════ Big-disk consolidation plan ════════════"
echo "Big disk device : $BIG_DEV ($BIG_FSTYPE, UUID=$BIG_UUID)"
echo "Currently at    : $BIG_MNT"
echo "Target mount    : $big_dst   (fleet convention)"
echo "Workspace bind  : $big_dst → $workspace"
echo

# What will be folded onto the big disk (data living on the *root* disk).
SRC_BIGDST=""   # stranded data sitting at big_dst on the root fs (only if big_dst is NOT a mount)
SRC_WS=""       # data sitting at workspace on the root fs (only if workspace is NOT already the big disk)

if ! is_mountpoint "$big_dst" && [ -d "$big_dst" ]; then
    SRC_BIGDST="$big_dst"
fi
if ! same_root "$workspace" "$BIG_MNT"; then
    SRC_WS="$workspace"
fi

need_kb=0
for d in $SRC_BIGDST $SRC_WS; do
    sz=$(sudo du -sk --one-file-system --exclude=tmp --exclude=lost+found "$d" 2>/dev/null | cut -f1 || echo 0)
    echo "  fold in: $d  (~$(numfmt --to=iec --from-unit=1024 "${sz:-0}"))"
    need_kb=$((need_kb + ${sz:-0}))
done
[ -n "$SRC_BIGDST$SRC_WS" ] || echo "  (nothing stranded on the root disk to fold in)"

avail_kb=$(df --output=avail -k "$BIG_MNT" | tail -1 | tr -d ' ')
echo
echo "  need on big disk : ~$(numfmt --to=iec --from-unit=1024 "$need_kb")"
echo "  free on big disk : ~$(numfmt --to=iec --from-unit=1024 "$avail_kb")"
echo "══════════════════════════════════════════════════════"

[ "$avail_kb" -ge $((need_kb + need_kb / 20 + 1)) ] || \
    die "Not enough free space on the big disk ($BIG_MNT) for the merge."

if [ "$CONFIRM" != "1" ]; then
    echo
    echo "This was a DRY analysis — no changes made."
    echo "Re-run with CONFIRM=1 to execute, e.g.:"
    echo "  sudo CONFIRM=1 bash consolidate_bigdisk.bash"
    echo "  (or via nami:  nami template <instance> consolidate_bigdisk CONFIRM=1)"
    exit 0
fi

BACKUP_DIR="$BIG_MNT/_consolidate_conflicts_$(date +%Y%m%d-%H%M%S)"

# ── 0. Quiesce NFS (if this node serves it) ────────────────────────────
NFS_WAS_ACTIVE=0
if systemctl is-active --quiet nfs-kernel-server 2>/dev/null; then
    NFS_WAS_ACTIVE=1
    log "Stopping nfs-kernel-server during the swap…"
    sudo systemctl stop nfs-kernel-server || true
fi

# ── 1. Detach stale binds that point INTO big_dst (e.g. /tmp ← big_dst/tmp) ──
log "Detaching any stale binds rooted under $big_dst…"
findmnt -rn -o TARGET,SOURCE 2>/dev/null | while read -r _tgt _src; do
    case "$_src" in
        *"[$big_dst/"*|*"[$big_dst]"*)
            echo "  umount stale bind: $_tgt  (source $_src)"
            sudo umount -l "$_tgt" 2>/dev/null || true
            ;;
    esac
done || true
# Drop their fstab lines (field-1 under big_dst, marked as a bind).
sudo sed -i "\\|^[[:space:]]*${big_dst}/[^[:space:]]*[[:space:]].*bind|d" /etc/fstab || true

# ── 2. Copy stranded data → big disk, then VERIFY before deleting anything ──
copy_and_verify() {
    local src="$1" dst="$2"
    log "Copying $src/ → $dst/ (conflicts backed up to $BACKUP_DIR)…"
    sudo rsync -aHAX --info=progress2 \
        --exclude=tmp --exclude=lost+found \
        --backup --backup-dir="$BACKUP_DIR" \
        "$src/" "$dst/"

    log "Verifying $src/ is fully present on the big disk…"
    local pending
    pending=$(sudo rsync -aHAXn --itemize-changes \
        --exclude=tmp --exclude=lost+found \
        "$src/" "$dst/" 2>/dev/null | grep -E '^>f' || true)
    if [ -n "$pending" ]; then
        echo "$pending" | head -n 20 >&2
        die "Verification failed for $src — NOT deleting anything. Investigate above."
    fi
    log "Verified: $src is safely on the big disk."
}

[ -n "$SRC_BIGDST" ] && copy_and_verify "$SRC_BIGDST" "$BIG_MNT"
[ -n "$SRC_WS" ]     && copy_and_verify "$SRC_WS" "$BIG_MNT"

# ── 3. Empty the source dirs (now safely copied) so we don't shadow them ──
#     Done while they are still plain root-disk directories, BEFORE mounting
#     the big disk over them — so the freed space is actually reclaimed on root.
if [ -n "$SRC_BIGDST" ]; then
    log "Reclaiming root space: emptying $big_dst…"
    sudo find "$big_dst" -mindepth 1 -delete 2>/dev/null || \
        warn "Some entries under $big_dst could not be removed (will be shadowed)."
fi
if [ -n "$SRC_WS" ]; then
    log "Reclaiming root space: emptying $workspace…"
    sudo find "$workspace" -mindepth 1 -delete 2>/dev/null || \
        warn "Some entries under $workspace could not be removed (will be shadowed)."
fi

# ── 4. Move the big disk mount: big_src → big_dst ───────────────────────
sudo mkdir -p "$big_dst"
if ! is_mountpoint "$big_dst"; then
    log "Mounting big disk at $big_dst…"
    sudo mount "$BIG_DEV" "$big_dst"
fi
# Detach the old location (safe: the same filesystem is now mounted at big_dst).
if is_mountpoint "$big_src" && [ "$big_src" != "$big_dst" ]; then
    log "Detaching old mount $big_src…"
    sudo umount "$big_src" 2>/dev/null || sudo umount -l "$big_src" 2>/dev/null || \
        warn "Could not unmount $big_src (still busy); it shares the disk now mounted at $big_dst."
fi

# ── 5. Bind big_dst → workspace ────────────────────────────────────────
sudo mkdir -p "$workspace"
if ! same_root "$workspace" "$big_dst"; then
    log "Bind-mounting $big_dst → $workspace…"
    sudo mount --bind "$big_dst" "$workspace"
fi

# ── 6. fstab persistence ───────────────────────────────────────────────
log "Updating /etc/fstab…"
sudo cp -a /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d-%H%M%S)"
# Remove stale/active entries for this disk and for the old/new mountpoints.
sudo sed -i "\\|UUID=${BIG_UUID}|d" /etc/fstab
sudo sed -i "\\|^[[:space:]]*${BIG_DEV}[[:space:]]|d" /etc/fstab
sudo sed -i "\\|[[:space:]]${big_src}[[:space:]]|d" /etc/fstab
sudo sed -i "\\|[[:space:]]${big_dst}[[:space:]]|d" /etc/fstab
sudo sed -i "\\|[[:space:]]${workspace}[[:space:]]|d" /etc/fstab
# Also drop any dead commented entries referencing the convention path.
sudo sed -i "\\|^#.*[[:space:]]${big_dst}[[:space:]]|d" /etc/fstab
# Append canonical entries: disk first, then the workspace bind.
echo "UUID=${BIG_UUID} ${big_dst} ${BIG_FSTYPE} defaults,nofail 0 2" | sudo tee -a /etc/fstab >/dev/null
echo "${big_dst} ${workspace} none bind,nofail 0 0"                  | sudo tee -a /etc/fstab >/dev/null
sudo systemctl daemon-reload 2>/dev/null || true

# ── 7. Compatibility symlink for anything still referencing big_src ─────
if [ "$big_src" != "$big_dst" ]; then
    # If the old mountpoint is now an empty, unmounted directory, replace it
    # with a symlink so legacy references to big_src keep resolving.
    if [ -d "$big_src" ] && ! is_mountpoint "$big_src" && [ -z "$(ls -A "$big_src" 2>/dev/null)" ]; then
        sudo rmdir "$big_src" 2>/dev/null || true
    fi
    if [ ! -e "$big_src" ]; then
        sudo ln -s "$big_dst" "$big_src"
        log "Created compatibility symlink: $big_src → $big_dst"
    fi
fi

# ── 8. Restore NFS ─────────────────────────────────────────────────────
if [ "$NFS_WAS_ACTIVE" = "1" ]; then
    log "Restarting nfs-kernel-server and refreshing exports…"
    sudo systemctl start nfs-kernel-server || true
    sudo exportfs -ra 2>/dev/null || true
fi

# ── 9. Verify & summarize ──────────────────────────────────────────────
echo
echo "════════════════════ Result ════════════════════"
findmnt "$big_dst"   || warn "Big disk not mounted at $big_dst!"
findmnt "$workspace" || warn "Workspace not bound at $workspace!"
echo
df -h "$big_dst" "$workspace" /
if [ -d "$BACKUP_DIR" ]; then
    echo
    warn "Some files collided during the merge and were preserved here:"
    echo "     $BACKUP_DIR"
    echo "     Review and delete once you've confirmed nothing important was overwritten."
fi
echo
log "Done. '$big_dst' is the big disk and '$workspace' is bound to it (convention restored)."
