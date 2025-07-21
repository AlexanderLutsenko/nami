# Sync to S3
aws --profile "${aws_profile}" s3 sync ${source_path} s3://${bucket_name}${s3_path} ${flags} 