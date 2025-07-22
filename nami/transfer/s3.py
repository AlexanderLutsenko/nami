from __future__ import annotations

import datetime
from pathlib import Path

from ..connection import SystemSSHConnection as Connection
from ..util import build_exclude_flags_s3, build_exclude_flags_zip


def _remote_path_is_file(instance: str, path: str, cfg: dict | None) -> bool:
    """Return True if *path* is a file on *instance*, False otherwise."""
    try:
        with Connection(instance, cfg) as conn:
            conn.run(f'test -f "{path}"')
        return True
    except RuntimeError:
        return False


def upload_to_s3(*,
                source_instance: str,
                source_path: str,
                dest_path: str,
                aws_profile: str = "default",
                exclude: str = "",
                archive: bool = False,
                operation_id: int | None = None,
                config: dict = None,
                ) -> None:
    with Connection(source_instance, config) as src:
        if archive:
            print("🔼 Uploading to S3 via ZIP archive …")
            zip_exclude_flags = build_exclude_flags_zip(exclude)
            src.run(
                f'''
                cd "{source_path}"
                zip -r -0 - . {zip_exclude_flags} | aws --profile {aws_profile} s3 cp - "{dest_path}"
                '''
            )
        else:
            print("🔼 Uploading to S3 …")
            aws_exclude_flags = build_exclude_flags_s3(exclude)
            # If the source path points to a directory we use `aws s3 sync`, otherwise fall back to `aws s3 cp`
            # This allows transferring both directories and single files.
            src.run(
                f'''
                if [ -d "{source_path}" ]; then
                    aws --profile {aws_profile} s3 sync "{source_path}" "{dest_path}" {aws_exclude_flags}
                elif [ -f "{source_path}" ]; then
                    aws --profile {aws_profile} s3 cp "{source_path}" "{dest_path}"
                else
                    echo "❌ Source path does not exist: {source_path}" >&2
                    exit 1
                fi
                '''
            )
        print("✅ Upload completed!")


def download_from_s3(*,
                    dest_instance: str,
                    source_path: str,
                    dest_path: str,
                    aws_profile: str = "default",
                    exclude: str = "",
                    archive: bool = False,
                    operation_id: int | None = None,
                    config: dict = None,
                    ) -> None:
    with Connection(dest_instance, config) as dest:
        if archive:
            print("🔽 Downloading from S3 via ZIP archive & extracting …")
            tid = operation_id or int(datetime.datetime.utcnow().timestamp())
            remote_zip_path = f"/tmp/xfer_{tid}.zip"
            dest.run(
                f'''
                aws --profile {aws_profile} s3 cp "{source_path}" {remote_zip_path}
                mkdir -p "{dest_path}"
                unzip -o {remote_zip_path} -d "{dest_path}"
                rm {remote_zip_path}
                '''
            )
        else:
            print("🔽 Downloading from S3 …")
            aws_exclude_flags = build_exclude_flags_s3(exclude)
            dest.run(
                f'''
                mkdir -p "$(dirname "{dest_path}")" || true
                # Attempt to copy as a single file first; if that fails, fallback to sync for directories.
                aws --profile {aws_profile} s3 cp "{source_path}" "{dest_path}" || \
                aws --profile {aws_profile} s3 sync "{source_path}" "{dest_path}" {aws_exclude_flags}
                '''
            )
        print("✅ Download completed!")


def transfer_via_s3(*,
                    source_instance: str,
                    dest_instance: str,
                    source_path: str,
                    dest_path: str,
                    s3_bucket: str,
                    aws_profile: str = "default",
                    exclude: str = "",
                    archive: bool = False,
                    operation_id: int | None = None,
                    config: dict = None
                    ) -> None:
    tid = operation_id or int(datetime.datetime.utcnow().timestamp())

    # Decide whether the given source path is a file or directory **on the remote source instance**.
    if archive:
        # Always upload a single zip file in archive mode.
        s3_path = f"s3://{s3_bucket}/transfer/{tid}/xfer.zip"
    else:
        if _remote_path_is_file(source_instance, source_path, config):
            src_basename = Path(source_path).name
            s3_path = f"s3://{s3_bucket}/transfer/{tid}/{src_basename}"
        else:
            s3_path = f"s3://{s3_bucket}/transfer/{tid}/"

    print("──────────── Transfer Context ────────────")
    print(f"🚚 Transfer ID : {tid}")
    print(f"📦 Archive mode: {archive}")
    print(f"🗂️  Exclude     : {exclude}\n")

    upload_to_s3(
        source_instance=source_instance,
        source_path=source_path,
        dest_path=s3_path,
        aws_profile=aws_profile,
        exclude=exclude,
        archive=archive,
        operation_id=tid,
        config=config,
    )

    download_from_s3(
        dest_instance=dest_instance,
        source_path=s3_path,
        dest_path=dest_path,
        aws_profile=aws_profile,
        exclude=exclude,
        archive=archive,
        operation_id=tid,
        config=config,
    )

    if archive:
        print("🧹 Cleaning up S3 temporary data …")
        with Connection(dest_instance, config) as dest:
            dest.run(
                f'aws --profile {aws_profile} s3 rm "{s3_path}" --recursive'
            )
    print("✅ Transfer completed!") 