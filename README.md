# Nami 🌊

**N**ode **A**ccess & **M**anipulation **I**nterface is a simple tool for managing connections to multiple remote instances (particularly GPU servers), with built-in GPU monitoring, file transfer capabilities via rsync/S3, and a template system for common tasks.

### Features

- **🔗 Multi-instance SSH management** - Add, list, and connect to remote servers
- **🌐 Heterogeneous environments** - Works across different Linux distros and cloud providers (Vast, AWS, Runpod, etc.)
- **📊 GPU monitoring** - GPU utilization and memory tracking
- **📁 File transfer** - Transfer files between instances directly via rsync or using S3 as intermediary
- **📜 Template system** - Execute pre-configured bash script templates on remote instances  
- **⚙️ Configuration management** - Personal and global configuration storage

### Installation <img src="https://img.shields.io/pypi/v/nami-surf?color=blue&style=flat-square">

```bash
pip install -U nami-surf
```

### 🚀 Quick Start

```bash
# Add a remote instance
nami add gpu-box 192.168.1.100 22 --user ubuntu --description "Main GPU server"

# List all instances with GPU status
nami list

# Connect to an instance via SSH  
nami ssh gpu-box

# Run a command on an instance
nami ssh gpu-box "nvidia-smi"

# Forward an instance’s configured port (e.g. Jupyter on 8888) to localhost
nami ssh gpu-box --forward

# Forward an arbitrary local port (override the one in config)
nami ssh gpu-box --forward 9000

# Transfer files between instances
nami transfer --source_instance local --dest_instance gpu-box --source_path ./data --dest_path ~/data

# Upload files to S3 from an instance
nami to_s3 --source_instance gpu-box --source_path ~/results --dest_path s3://bucket/experiment1/

# Download files from S3 to an instance  
nami from_s3 --dest_instance gpu-box --source_path s3://bucket/dataset/ --dest_path ~/data/

# Execute a template on an instance
nami template gpu-box setup_conda
```

#### Example output
```text
$ nami list

📋 Configured Instances:
-----------------------------------------------------------------
🖥️ training-box (✅ Online)
   SSH: ubuntu@203.0.113.10:2222, local port: 8080
   Description: Primary training server
   GPUs:
     🟢 GPU0:   0% | Mem:   2% | NVIDIA A100 80GB
     🔴 GPU1: 100% | Mem:  94% | NVIDIA A100 80GB
     🟠 GPU2:   0% | Mem:  51% | NVIDIA A100 80GB

🖥️ idle-node (✅ Online)
   SSH: admin@203.0.113.11:2222
   Description: Spare capacity node
   GPUs:
     🟢 GPU0:   0% | Mem:   0% | NVIDIA H100

🖥️ backup-box (❌ Offline)
   SSH: root@203.0.113.12:2222
   Description: Cold backup server
```

### 🔧 Commands

#### Instance Management
```bash
# List all instances with GPU status
nami list

# Connect via SSH or run a command
nami ssh <instance> [command] [--forward [PORT]]

# Add a new instance
nami add <name> <host> <port> [--user USER] [--local-port PORT] [--description DESC]

# Remove an instance
nami remove <name>

```

#### Configuration
```bash
# Set personal config value
nami config set <key> <value>

# Show configuration (all or specific key)
nami config show [key]
```

#### File Transfer

Nami supports two strategies for moving data between machines:

- **rsync** – Files are copied directly between the two instances over SSH. This is ideal for smaller transfers and, thanks to rsync’s synchronization logic, it will only transmit files that are new or have changed on the source, saving both time and bandwidth.
- **s3** – Data are first uploaded from the source instance to an S3 bucket and then downloaded to the destination instance. Despite the extra hop, this approach is usually the fastest for large datasets because the upload/download steps can fully saturate network bandwidth and run in parallel.

```bash
# Transfer files between instances
nami transfer --source_instance SRC \
    --dest_instance DEST \
    --source_path PATH \
    [--dest_path PATH] \
    [--method rsync|s3] \
    [--exclude PATTERNS] \
    [--archive] \
    [--rsync_opts "OPTIONS"]

# Upload to S3
nami to_s3 \
    --source_instance INSTANCE \
    --source_path PATH \
    --dest_path S3_PATH \
    [--exclude PATTERNS] \
    [--archive] \
    [--aws_profile PROFILE]

# Download from S3  
nami from_s3 
    --dest_instance INSTANCE \
    --source_path S3_PATH \
    --dest_path PATH \
    [--exclude PATTERNS] \
    [--archive] \
    [--aws_profile PROFILE]
```

#### Templates
```bash
# Execute a template with variables
nami template <instance> <template_name> \
    [--var1 value1 --var2 value2 ...]
```

### ⚙️ Configuration

Nami stores its configuration in `~/.nami/`:

- `config.yaml` - Instance definitions and global settings
- `personal.yaml` - User-specific configurations (S3 bucket, AWS profile, etc.)
- `templates/` - Custom bash script templates

#### Configuration File Structure

**`~/.nami/config.yaml`** - Main configuration file:
```yaml
instances:
  gpu-box:
    host: "192.168.1.100"
    port: 22
    user: "ubuntu"
    description: "Main GPU server"
    local_port: 8888  # optional - for SSH tunneling
  
  cloud-instance:
    host: "ec2-xxx.compute.amazonaws.com"
    port: 22
    user: "ec2-user"
    description: "AWS EC2 instance"

variables:
  # Global template variables available to all templates
  # var1: value1
  # ...
```

**`~/.nami/personal.yaml`** - User-specific settings:
```yaml
home_dir: "/workspace/<username>"

s3_bucket: "<username>"

aws_profile: "my-profile"
aws_access_key_id: XXXX
aws_secret_access_key: XXXX
aws_endpoint_url: https://XXXX.com

# Other personal settings
# ...
```

#### Variable Priority
Template variables are resolved in this order (highest priority first):
1. Command-line variables (`--var key=value`)
2. Personal config (`personal.yaml`)
3. Global config (`config.yaml` variables section)
