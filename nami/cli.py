import os
import sys
import yaml
import argparse
import subprocess
import tempfile
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from string import Template
from .transfer import s3, rsync
from .connection import SystemSSHConnection as Connection
import base64


class Nami():
    def __init__(self, config_dir="~/.nami"):
        self.config_dir = Path(config_dir).expanduser()
        self.config_dir.mkdir(exist_ok=True)
        self.config_file = self.config_dir / "config.yaml"
        self.personal_config_file = self.config_dir / "personal.yaml"
        self.templates_dir = self.config_dir / "templates"
        self.templates_dir.mkdir(exist_ok=True)
        self.config = self.load_config() or {"instances": {}, "variables": {}}
        self.personal_config = self.load_personal_config()

    def load_config(self):
        """Load configuration from YAML file."""
        if self.config_file.exists():
            with open(self.config_file, 'r') as f:
                return yaml.safe_load(f) or {}
        # Initialise default structure if missing
        return {"instances": {}, "variables": {}}

    def load_personal_config(self):
        """Load personal configuration from YAML file."""
        if self.personal_config_file.exists():
            with open(self.personal_config_file, 'r') as f:
                return yaml.safe_load(f) or {}
        return {}

    def save_personal_config(self):
        """Save personal configuration to YAML file."""
        with open(self.personal_config_file, 'w') as f:
            yaml.dump(self.personal_config, f, default_flow_style=False, indent=2)

    def get_personal_config(self, key=None):
        """Get personal configuration value(s)."""
        if key:
            return self.personal_config.get(key)
        return self.personal_config

    def set_personal_config(self, key, value):
        """Set a personal configuration value. If value is empty, delete the key."""
        if value == "":
            if key in self.personal_config:
                del self.personal_config[key]
                self.save_personal_config()
                print(f"✅ Deleted personal config '{key}'")
            else:
                print(f"❌ Key '{key}' not found in personal config")
        else:
            self.personal_config[key] = value
            self.save_personal_config()
            print(f"✅ Set personal config '{key}' = '{value}'")

    def show_personal_config(self):
        """Show all personal configuration."""
        if not self.personal_config:
            print("No personal configuration set.")
            return

        print("\n🔒 Personal Configuration:")
        print("-" * 40)
        for key, value in self.personal_config.items():
            print(f"  {key}: {value}")
        print()
    
    def save_config(self):
        """Save configuration to YAML file."""
        with open(self.config_file, 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False, indent=2)

    def add_instance(self, name, host, port, user="root", local_port=None, description=""):
        """Add a new instance to the configuration."""
        instance_config = {
            "host": host,
            "port": port,
            "user": user,
            "description": description
        }
        if local_port:
            instance_config["local_port"] = local_port

        self.config.setdefault("instances", {})[name] = instance_config
        self.save_config()
        print(f"✅ Added instance '{name}': {user}@{host}:{port}")

    def remove_instance(self, name):
        """Remove an instance from the configuration."""
        if name not in self.config.get("instances", {}):
            print(f"❌ Instance '{name}' not found.")
            return
        
        del self.config["instances"][name]
        self.save_config()
        print(f"✅ Removed instance '{name}'.")

    def _get_instance_info(self, name):
        """Get all information for a single instance (status + GPU info)."""
        config = self.config["instances"][name]
        # Fetch GPU info first; this SSH call is sufficient to decide if the host is reachable.
        gpu_info_lines, status = self.get_gpu_info(name)
        return name, config, status, gpu_info_lines

    def list_instances(self):
        """List all configured instances with GPU information (parallel checks)."""
        if not self.config.get("instances"):
            print("No instances configured.")
            return
        
        print("\n📋 Configured Instances:")
        print("-" * 80)
        print("🔄 Checking instances...")
        
        start_time = time.time()
        
        # Collect all instance information in parallel
        instance_results = {}
        with ThreadPoolExecutor(max_workers=30) as executor:
            # Submit all tasks
            future_to_name = {
                executor.submit(self._get_instance_info, name): name 
                for name in self.config["instances"].keys()
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_name):
                name, config, status, gpu_info_lines = future.result()
                instance_results[name] = (config, status, gpu_info_lines)
        
        elapsed_time = time.time() - start_time
        print(f"✅ All checks completed in {elapsed_time:.1f}s\n")
        
        # Display results in original order
        for name in self.config["instances"].keys():
            config, status, gpu_info_lines = instance_results[name]
            
            print(f"🖥️  {name} ({status})")
            port_display = config.get('port')
            port_str = f":{port_display}" if port_display is not None else ""
            print(f"   SSH: {config['user']}@{config['host']}{port_str}, local port: {config.get('local_port', 'N/A')}")
            if config.get('local_port', None):
                local_port = f"-L {config['local_port']}:localhost:{config['local_port']}"
            else:
                local_port = ""
            if port_display is not None:
                print(f"   Command: ssh -p {port_display} {config['user']}@{config['host']} {local_port}")
            else:
                print(f"   Command: ssh {config['user']}@{config['host']} {local_port}")
            if config.get('description'):
                print(f"   Description: {config['description']}")
            if gpu_info_lines is not None:
                print("   GPUs:")
                for gpu_line in gpu_info_lines:
                    print('     ' + gpu_line)
            print()

    def get_template(self, template_name):
        """Load a command template from user templates or default templates."""
        template_filename = f"{template_name}.bash"
        
        # First check user's custom templates directory
        user_template_path = self.templates_dir / template_filename
        if user_template_path.exists():
            with open(user_template_path, 'r') as f:
                return f.read()
        
        # If not found, check default templates directory that ships with the
        # *installed* package (inside ``nami/default_templates``).
        script_dir = Path(__file__).parent  # .../nami
        pkg_default_path = script_dir / "default_templates" / template_filename
        if pkg_default_path.exists():
            with open(pkg_default_path, 'r') as f:
                return f.read()

        raise FileNotFoundError(
            f"Template '{template_name}' not found in user templates ({user_template_path}) or "
            f"package defaults ({pkg_default_path})"
        )

    def render_template(self, template_content, variables):
        """Render a template with variables."""
        all_vars = {
            **self.config.get("variables", {}),
            **self.personal_config,
            **(variables or {})
        }

        try:
            template = Template(template_content)
            return template.safe_substitute(all_vars)
        except KeyError as e:
            raise ValueError(f"Missing variable in template: {e}")

    def execute_template(self, instance_name, template_name, variables=None):
        # Ensure the instance exists before attempting to connect.
        variables = variables or {}
        template_content = self.get_template(template_name)
        
        import re
        placeholder_pattern = re.compile(r"\$\{?([_a-zA-Z][_a-zA-Z0-9]*)\}?")
        placeholders = set(placeholder_pattern.findall(template_content))

        # Error on unused variables
        unused = set(variables.keys()) - placeholders
        if unused:
            raise ValueError(f"Unused template variables: {', '.join(sorted(unused))}")

        rendered_script = self.render_template(template_content, variables)

        # Warn if placeholders remain unfilled after rendering
        remaining = set(placeholder_pattern.findall(rendered_script))
        if remaining:
            print(f"⚠️  Warning: unfilled placeholders -> {', '.join(sorted(remaining))}")

        print(f"🔧 Executing template '{template_name}' on {instance_name}...")
        try:
            self.run_ssh_command(instance_name, rendered_script)
            print(f"✅ Template '{template_name}' executed successfully on {instance_name}")
            return True
        except Exception as e:
            print(f"❌ Template execution failed on {instance_name}")
            print(e)
            return False

    def run_ssh_command(self, instance_name, command, forward=False):
        """Execute a command on an instance via SSH.

        Parameters
        ----------
        instance_name: str
            Target instance name as configured in ``config.yaml``.
        command: str
            Shell command to execute remotely.  If *None*, an interactive shell
            will be opened (see ``connect_ssh``).
        forward: bool, optional
            When ``True`` the instance's ``local_port`` value is forwarded via
            ``ssh -L``.  By default no port forwarding is performed.
        """
        with Connection(instance_name, self.config, enable_port_forwarding=forward, personal_config=self.personal_config) as ssh:
            ssh.run(command)

    def connect_ssh(self, instance_name, forward=False):
        """Open an interactive SSH session to *instance_name*.

        If *forward* is ``True`` the configured ``local_port`` will be
        forwarded.
        """
        with Connection(instance_name, self.config, enable_port_forwarding=forward, personal_config=self.personal_config) as ssh:
            ssh.run_interactive()

    def get_gpu_info(self, name):
        """Get GPU information for an instance."""
        if name not in self.config.get("instances", {}):
            return ["❌ Not configured"]
        
        try:
            with Connection(name, self.config, personal_config=self.personal_config) as conn:
                result = conn.run(
                    "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null || echo 'NO_GPU'", 
                    capture=True
                )
                output = result.stdout.strip()
                if not output or output == "NO_GPU":
                    return ["🔘 No GPU"], "✅ Online"
                
                # Parse GPU information
                gpu_lines = output.split('\n')
                gpu_info = []
                for line in gpu_lines:
                    if line.strip():
                        parts = [p.strip() for p in line.split(',')]
                        if len(parts) >= 5:
                            gpu_idx, gpu_name, gpu_util, mem_used, mem_total = parts[:5]
                            try:
                                gpu_util = int(gpu_util)
                                mem_used = int(mem_used)
                                mem_total = int(mem_total)
                                mem_percent = int((mem_used / mem_total) * 100) if mem_total > 0 else 0
                                
                                # Color code based on utilisation
                                if gpu_util >= 50:
                                    util_color = "🔴"
                                elif mem_percent >= 50:
                                    util_color = "🟠"
                                elif gpu_util >= 10:
                                    util_color = "🟡"
                                else:
                                    util_color = "🟢"
                                
                                gpu_info.append(f"{util_color} GPU{gpu_idx}: {gpu_util:3d}% | Mem: {mem_percent:3d}% | {gpu_name}")
                            except (ValueError, ZeroDivisionError):
                                gpu_info.append(f"🔘 GPU{gpu_idx}: Error parsing")
                
                return gpu_info or ["🔘 No GPU data"], "✅ Online"
        
        except Exception as e:
            error_str = str(e).lower()
            # Extract output from the error message if present
            if "output:" in error_str:
                _, output_part = error_str.split("output:", 1)
                output_str = output_part.strip().lower()
            else:
                output_str = ""

            full_error = error_str + " " + output_str

            if any(keyword in full_error for keyword in ['connection refused', 'connection rejected', 'connection denied']):
                return None, "⚠️  Unavailable"
            elif any(keyword in full_error for keyword in ['unreachable', 'no route']):
                return None, "❌ Network Error"
            elif any(keyword in full_error for keyword in ['timeout', 'timed out']):
                return None, "⏱️  Timeout"
            else:
                return None, "❌ Error: " + str(e)

    def _add_key_to_instance(self, name, key_file):
        config = self.config.get("instances", {}).get(name)
        if not config:
            return (name, False, "Instance not found")
        
        host = config["host"]
        user = config["user"]
        port = config.get("port")
        
        cmd = ["ssh-copy-id", "-f", "-i", key_file]
        if port is not None:
            cmd.extend(["-p", str(port)])
        cmd.append(f"{user}@{host}")
        
        try:
            subprocess.check_call(cmd)
            return (name, True, None)
        except subprocess.CalledProcessError as e:
            return (name, False, f"ssh-copy-id failed with exit code {e.returncode}")
        except FileNotFoundError:
            return (name, False, "ssh-copy-id command not found")
        except Exception as e:
            return (name, False, str(e))

    def _remove_key_from_instance(self, name, pattern):
        """Remove SSH key(s) matching pattern from an instance's authorized_keys."""
        config = self.config.get("instances", {}).get(name)
        if not config:
            return (name, False, "Instance not found", 0)
        
        host = config["host"]
        user = config["user"]
        port = config.get("port")
        
        # Build SSH command
        ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
        if port is not None:
            ssh_cmd.extend(["-p", str(port)])
        ssh_cmd.append(f"{user}@{host}")
        
        # Use sed to remove lines matching the pattern from authorized_keys
        # First, count matching lines, then remove them
        escaped_pattern = pattern.replace("'", "'\\''")
        remote_cmd = textwrap.dedent(f'''
            count=$(grep -c '{escaped_pattern}' ~/.ssh/authorized_keys 2>/dev/null || echo 0)
            if [ "$count" -gt 0 ]; then
                sed -i '/{escaped_pattern}/d' ~/.ssh/authorized_keys
                echo "REMOVED:$count"
            else
                echo "REMOVED:0"
            fi
        ''')
        
        try:
            result = subprocess.run(
                ssh_cmd + [remote_cmd],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode != 0:
                return (name, False, f"SSH command failed: {result.stderr.strip()}", 0)
            
            # Parse the count from output
            for line in result.stdout.strip().split('\n'):
                if line.startswith("REMOVED:"):
                    count = int(line.split(":")[1])
                    return (name, True, None, count)
            return (name, True, None, 0)
        except subprocess.TimeoutExpired:
            return (name, False, "Connection timed out", 0)
        except Exception as e:
            return (name, False, str(e), 0)

    def remove_ssh_key(self, pattern, instance_name=None):
        """Remove SSH key(s) matching a pattern from instance(s).
        
        The pattern is matched against the entire key line, so you can match by:
        - Email (e.g., "user@example.com")
        - Username/comment (e.g., "john")
        - Key type (e.g., "ssh-ed25519")
        - Part of the key itself
        """
        if instance_name is None:
            instances = list(self.config["instances"].keys())
        else:
            instances = [instance_name]
        
        if not instances:
            print("No instances configured.")
            return
        
        results = {}
        with ThreadPoolExecutor(max_workers=30) as executor:
            future_to_name = {
                executor.submit(self._remove_key_from_instance, n, pattern): n 
                for n in instances
            }
            for future in as_completed(future_to_name):
                name, success, error, count = future.result()
                results[name] = (success, error, count)
        
        total_removed = 0
        for name in instances:
            success, error, count = results.get(name, (False, "Unknown error", 0))
            if success:
                if count > 0:
                    print(f"✅ Removed {count} key(s) from {name}")
                    total_removed += count
                else:
                    print(f"ℹ️  No matching keys found on {name}")
            else:
                print(f"❌ Failed to remove key from {name}: {error}")
        
        if total_removed > 0:
            print(f"\n🔑 Total: Removed {total_removed} key(s) across {len(instances)} instance(s)")

    def add_ssh_key(self, public_key, instance_name=None):
        if instance_name is None:
            instances = list(self.config["instances"].keys())
        else:
            instances = [instance_name]
        
        if not instances:
            print("No instances configured.")
            return
        
        import tempfile
        import os
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pub') as tmp:
            tmp.write(public_key.strip() + '\n')
            tmp_filename = tmp.name
        
        results = {}
        with ThreadPoolExecutor(max_workers=30) as executor:
            future_to_name = {
                executor.submit(self._add_key_to_instance, n, tmp_filename): n 
                for n in instances
            }
            for future in as_completed(future_to_name):
                name, success, error = future.result()
                results[name] = (success, error)
        
        os.unlink(tmp_filename)
        
        for name in instances:
            success, error = results.get(name, (False, "Unknown error"))
            if success:
                print(f"✅ Added SSH key to {name}")
            else:
                print(f"❌ Failed to add SSH key to {name}: {error}")


# -----------------------------------------------------------------------------
# CLI entry point
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="NAMI - Node Access & Manipulation Interface")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Add instance management commands
    add_parser = subparsers.add_parser("add", help="Add a new instance")
    add_parser.add_argument("name", help="Instance name")
    add_parser.add_argument("host", help="Host IP address")
    add_parser.add_argument("port", type=int, help="SSH port")
    add_parser.add_argument("--user", default="root", help="SSH user (default: root)")
    add_parser.add_argument("--local-port", type=int, help="Local port for SSH tunnel")
    add_parser.add_argument("--description", help="Instance description")
    
    subparsers.add_parser("list", help="List all instances")
    
    remove_parser = subparsers.add_parser("remove", help="Remove an instance")
    remove_parser.add_argument("name", help="Instance name to remove")
    
    ssh_parser = subparsers.add_parser("ssh", help="Run SSH command on instance")
    ssh_parser.add_argument("instance", help="Instance name")
    ssh_parser.add_argument("ssh_command", nargs="?", help="Command to run on the remote host (if not provided, opens interactive shell)")
    ssh_parser.add_argument(
        "--forward",
        nargs="?",               # optional value
        const=None,               # flag present without value ⇒ use configured port
        default=False,            # flag absent ⇒ no forwarding
        type=int,                 # when provided, treat as int port
        help="Enable port forwarding (optionally specify local port number, e.g. --forward 9000)",
    )
    
    config_parser = subparsers.add_parser("config", help="Manage personal configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_action", help="Config actions")
    
    config_set_parser = config_subparsers.add_parser("set", help="Set config value")
    config_set_parser.add_argument("key", help="Configuration key")
    config_set_parser.add_argument("value", help="Configuration value")
    
    config_show_parser = config_subparsers.add_parser("show", help="Show configuration")
    config_show_parser.add_argument("key", nargs="?", help="Specific key to show (optional)")
    
    # SSH key management
    ssh_key_parser = subparsers.add_parser("ssh-key", help="Manage SSH keys on instances")
    ssh_key_subparsers = ssh_key_parser.add_subparsers(dest="ssh_key_action", help="SSH key actions")
    
    ssh_key_add_parser = ssh_key_subparsers.add_parser("add", help="Add SSH public key to instance(s)")
    ssh_key_add_parser.add_argument("public_key", help="The public key string to add")
    ssh_key_add_parser.add_argument("--instance", help="Specific instance name (if not provided, add to all)")
    
    ssh_key_remove_parser = ssh_key_subparsers.add_parser("remove", help="Remove SSH key(s) matching a pattern from instance(s)")
    ssh_key_remove_parser.add_argument("pattern", help="Pattern to match (e.g., email, username, or part of the key)")
    ssh_key_remove_parser.add_argument("--instance", help="Specific instance name (if not provided, remove from all)")
    
    # Unified transfer command
    transfer_parser = subparsers.add_parser("transfer", help="Transfer data between instances")
    transfer_parser.add_argument("--method", choices=["rsync", "s3"], default="rsync", help="Transfer method")
    transfer_parser.add_argument("--source_instance", required=True, help="Source instance name")
    transfer_parser.add_argument("--dest_instance", required=True, help="Destination instance name")
    transfer_parser.add_argument("--source_path", required=True, help="Path on source instance to copy")
    transfer_parser.add_argument("--dest_path", help="Destination path on destination instance (defaults to --source_path)")
    transfer_parser.add_argument("--exclude", dest="exclude_patterns", help="Comma-separated patterns to exclude when syncing")
    transfer_parser.add_argument("--include", dest="include_patterns", help="Comma-separated patterns to include when syncing")
    transfer_parser.add_argument("--archive", action="store_true", help="Archive mode (ZIP)")
    transfer_parser.add_argument("--rsync_opts", default="-avz --progress", help="Extra rsync options")
    transfer_parser.add_argument("--endpoint", help="Custom S3 endpoint URL")

    # NFS mesh mounting command
    nfs_parser = subparsers.add_parser("nfs", help="NFS mesh mounting operations")
    nfs_subparsers = nfs_parser.add_subparsers(dest="nfs_action", required=True, help="NFS actions")

    mesh_parser = nfs_subparsers.add_parser("mount-mesh", help="Setup and mount full NFS mesh among instances")
    mesh_parser.add_argument("--instances", nargs="+", help="Instance names to include in the mesh")
    mesh_parser.add_argument("--export_dir", default="/", help="Path to export on each server")
    mesh_parser.add_argument("--mount_base", default="/mnt/peers", help="Base directory on clients for peer mounts")
    mesh_parser.add_argument("--nfs_version", default="4", help="NFS protocol version for mounts")
    mesh_parser.add_argument("--max_workers", type=int, default=24, help="Concurrency for operations")

    # Download from S3
    from_s3_parser = subparsers.add_parser("from_s3", help="Download files/folders from S3 to an instance")
    from_s3_parser.add_argument("--dest_instance", required=True)
    from_s3_parser.add_argument("--source_path", required=True)
    from_s3_parser.add_argument("--dest_path", required=True)
    from_s3_parser.add_argument("--exclude", dest="exclude_patterns")
    from_s3_parser.add_argument("--include", dest="include_patterns")
    from_s3_parser.add_argument("--archive", action="store_true")
    from_s3_parser.add_argument("--aws_profile")
    from_s3_parser.add_argument("--endpoint", help="Custom S3 endpoint URL")

    # Upload to S3
    to_s3_parser = subparsers.add_parser("to_s3", help="Upload files/folders from an instance to S3")
    to_s3_parser.add_argument("--source_instance", required=True)
    to_s3_parser.add_argument("--source_path", required=True)
    to_s3_parser.add_argument("--dest_path", required=True)
    to_s3_parser.add_argument("--exclude", dest="exclude_patterns")
    to_s3_parser.add_argument("--include", dest="include_patterns")
    to_s3_parser.add_argument("--archive", action="store_true")
    to_s3_parser.add_argument("--aws_profile")
    to_s3_parser.add_argument("--endpoint", help="Custom S3 endpoint URL")

    # Template command
    template_parser = subparsers.add_parser("template", help="Execute a template on an instance")
    template_parser.add_argument("instance")
    template_parser.add_argument("template")

    # Parse known args; keep unknowns for template variables.
    args, unknown_args = parser.parse_known_args()

    if not args.command:
        parser.print_help()
        return

    if args.command != "template" and unknown_args:
        print(f"❌ Unknown arguments: {' '.join(unknown_args)}")
        return

    vm = Nami()

    if args.command == "add":
        vm.add_instance(
            args.name, args.host, args.port, args.user, args.local_port, args.description or ""
        )
    elif args.command == "list":
        vm.list_instances()
    elif args.command == "remove":
        vm.remove_instance(args.name)
    elif args.command == "ssh":
        if args.ssh_command:
            vm.run_ssh_command(args.instance, args.ssh_command, forward=args.forward)
        else:
            vm.connect_ssh(args.instance, forward=args.forward)
    elif args.command == "config":
        if args.config_action == "set":
            vm.set_personal_config(args.key, args.value)
        elif args.config_action == "show":
            if args.key:
                value = vm.get_personal_config(args.key)
                print(f"{args.key}: {value}")
            else:
                vm.show_personal_config()
        else:
            print("❌ Please specify 'set' or 'show' for config command")
    elif args.command == "ssh-key":
        if args.ssh_key_action == "add":
            vm.add_ssh_key(args.public_key, args.instance)
        elif args.ssh_key_action == "remove":
            vm.remove_ssh_key(args.pattern, args.instance)
        else:
            print(f"❌ Unknown ssh-key action: {args.ssh_key_action}. Available: add, remove")
    elif args.command == "transfer":
        if args.method == "rsync":
            rsync.transfer_via_rsync(
                source_instance=args.source_instance,
                dest_instance=args.dest_instance,
                source_path=args.source_path,
                dest_path=args.dest_path or args.source_path,
                exclude=args.exclude_patterns or "",
                rsync_opts=args.rsync_opts,
                archive=args.archive,
                config=vm.config,
                personal_config=vm.personal_config
            )
        elif args.method == "s3":
            s3.transfer_via_s3(
                source_instance=args.source_instance,
                dest_instance=args.dest_instance,
                source_path=args.source_path,
                dest_path=args.dest_path or args.source_path,
                s3_bucket=vm.personal_config["s3_bucket"],
                aws_profile=vm.personal_config.get("aws_profile", "default"),
                exclude=args.exclude_patterns or "",
                include=args.include_patterns or "",
                archive=args.archive,
                endpoint=args.endpoint,
                config=vm.config,
                personal_config=vm.personal_config
            )
    elif args.command == "nfs":
        if args.nfs_action == "mount-mesh":
            from .nfs.nfs import setup_and_mount_full_mesh
            setup_and_mount_full_mesh(
                instances=args.instances,
                export_dir=args.export_dir,
                mount_base=args.mount_base,
                nfs_version=args.nfs_version,
                max_workers=args.max_workers,
                config=vm.config,
                personal_config=vm.personal_config
            )
        else:
            print(f"❌ Unknown nfs action: {args.nfs_action}. Available: mount-mesh")
    elif args.command == "from_s3":
        s3.download_from_s3(
            dest_instance=args.dest_instance,
            source_path=args.source_path,
            dest_path=args.dest_path,
            aws_profile=vm.personal_config.get("aws_profile", "default"),
            exclude=args.exclude_patterns or "",
            include=args.include_patterns or "",
            archive=args.archive,
            endpoint=args.endpoint,
            config=vm.config,
            personal_config=vm.personal_config
        )
    elif args.command == "to_s3":
        s3.upload_to_s3(
            source_instance=args.source_instance,
            source_path=args.source_path,
            dest_path=args.dest_path,
            aws_profile=vm.personal_config.get("aws_profile", "default"),
            exclude=args.exclude_patterns or "",
            include=args.include_patterns or "",
            archive=args.archive,
            endpoint=args.endpoint,
            config=vm.config,
            personal_config=vm.personal_config
        )
    elif args.command == "template":
        template_vars: dict[str, str] = {}
        if len(unknown_args) % 2 != 0:
            print("❌ Template variables must be provided as '--key value' pairs.")
            return
        for flag, value in zip(unknown_args[0::2], unknown_args[1::2]):
            if not flag.startswith("--"):
                print(f"⚠️  Ignoring unexpected token '{flag}' (flags should start with --)")
                continue
            key = flag[2:]
            template_vars[key] = value
        vm.execute_template(args.instance, args.template, template_vars)
    else:
        print(f"❌ Unknown command: {args.command}")
        parser.print_help()


if __name__ == "__main__":
    main() 