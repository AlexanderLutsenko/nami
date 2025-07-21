# Sync from S3
aws --profile "${aws_profile}" s3 sync s3://${bucket_name}${s3_path} ${dest_path} ${flags} 