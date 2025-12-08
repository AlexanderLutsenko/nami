# AWS CLI Installation and Setup
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -o awscliv2.zip
sudo ./aws/install --update
rm -rf awscliv2.zip aws/
# aws --profile "${aws_profile}" configure set aws_access_key_id "${aws_access_key_id}"
# aws --profile "${aws_profile}" configure set aws_secret_access_key "${aws_secret_access_key}"
# aws --profile "${aws_profile}" configure set endpoint_url "${aws_endpoint_url}"
echo "✅ AWS CLI setup completed" 
