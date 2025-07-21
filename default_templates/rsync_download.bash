# Download data from remote instance  
rsync -avz --progress ${rsync_flags} -e "ssh -p ${ssh_port}" ${ssh_user}@${ssh_host}:${remote_path} ${local_path} 