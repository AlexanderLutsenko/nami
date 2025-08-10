# Upload data to remote instance
rsync -avz --progress ${rsync_flags} -e "ssh ${ssh_port:+-p ${ssh_port}}" ${local_path} ${ssh_user}@${ssh_host}:${remote_path}