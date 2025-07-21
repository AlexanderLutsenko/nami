import os
import sys
import yaml
import argparse
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from string import Template
from .transfer import s3, rsync
from .connection import SystemSSHConnection as Connection


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
        gpu_info_lines = self.get_gpu_info(name)

        # Determine online/offline status from the first returned line.
        first_line = gpu_info_lines[0] if gpu_info_lines else "❌ Error"
        if first_line.startswith("❌"):
            status = "❌ Offline"
        else:
            status = "✅ Online"

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
            print(f"   SSH: {config['user']}@{config['host']}:{config['port']}, local port: {config.get('local_port', 'N/A')}")
            if config.get('local_port', None):
                local_port = f"-L {config['local_port']}:localhost:{config['local_port']}"
            else:
                local_port = ""
            print(f"   Command: ssh -p {config['port']} {config['user']}@{config['host']} {local_port}")
            if config.get('description'):
                print(f"   Description: {config['description']}")
            print("   GPUs:")
            for gpu_line in gpu_info_lines:
                print(gpu_line)
            print()

    def get_template(self, template_name):
        """Load a command template from user templates or default templates."""
        template_filename = f"{template_name}.bash"
        
        # First check user's custom templates directory
        user_template_path = self.templates_dir / template_filename
        if user_template_path.exists():
            with open(user_template_path, 'r') as f:
                return f.read()
        
        # If not found, check default templates directory within the package
        script_dir = Path(__file__).parent  # nami package directory
        default_template_path = script_dir / "default_templates" / template_filename
        if default_template_path.exists():
            with open(default_template_path, 'r') as f:
                return f.read()
        
        raise FileNotFoundError(f"Template '{template_name}' not found in user templates ({user_template_path}) or default templates ({default_template_path})")

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
        if instance_name not in self.config.get("instances", {}):
            print(f"❌ Instance '{instance_name}' not found.")
            return False

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

    def run_ssh_command(self, instance_name, command):
        """Execute a command on an instance via SSH."""
        with Connection(instance_name, self.config) as ssh:
            ssh.run(command)

    def connect_ssh(self, instance_name, command=None):
        """Connect to an instance via SSH."""
        with Connection(instance_name, self.config) as ssh:
            ssh.run_interactive(command)

    def get_gpu_info(self, name):
        """Get GPU information for an instance."""
        if name not in self.config.get("instances", {}):
            return ["❌ Not configured"]
        
        config = self.config["instances"][name]
        try:
            # Run nvidia-smi to get GPU information
            result = subprocess.run([
                "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                f"-p{config['port']}", f"{config['user']}@{config['host']}", 
                "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null || echo 'NO_GPU'"
            ], capture_output=True, text=True, timeout=10)
            
            if result.returncode != 0:
                return ["❌ SSH Failed"]
            
            output = result.stdout.strip()
            if not output or output == "NO_GPU":
                return ["🔘 No GPU"]
            
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
                            
                            gpu_info.append(f"     {util_color} GPU{gpu_idx}: {gpu_util:3d}% | Mem: {mem_percent:3d}% | {gpu_name}")
                        except (ValueError, ZeroDivisionError):
                            gpu_info.append(f"     🔘 GPU{gpu_idx}: Error parsing")
            
            return gpu_info or ["🔘 No GPU data"]
        
        except Exception:
            return ["❌ Error"]


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
    
    config_parser = subparsers.add_parser("config", help="Manage personal configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_action", help="Config actions")
    
    config_set_parser = config_subparsers.add_parser("set", help="Set config value")
    config_set_parser.add_argument("key", help="Configuration key")
    config_set_parser.add_argument("value", help="Configuration value")
    
    config_show_parser = config_subparsers.add_parser("show", help="Show configuration")
    config_show_parser.add_argument("key", nargs="?", help="Specific key to show (optional)")
    
    # Unified transfer command
    transfer_parser = subparsers.add_parser("transfer", help="Transfer data between instances")
    transfer_parser.add_argument("--method", choices=["rsync", "s3"], default="rsync", help="Transfer method")
    transfer_parser.add_argument("--source_instance", required=True, help="Source instance name")
    transfer_parser.add_argument("--dest_instance", required=True, help="Destination instance name")
    transfer_parser.add_argument("--source_path", required=True, help="Path on source instance to copy")
    transfer_parser.add_argument("--dest_path", help="Destination path on destination instance (defaults to --source_path)")
    transfer_parser.add_argument("--exclude", dest="exclude_patterns", help="Comma-separated patterns to exclude when syncing")
    transfer_parser.add_argument("--archive", action="store_true", help="Archive mode (ZIP)")
    transfer_parser.add_argument("--rsync_opts", default="-avz --progress", help="Extra rsync options")

    # Download from S3
    from_s3_parser = subparsers.add_parser("from_s3", help="Download files/folders from S3 to an instance")
    from_s3_parser.add_argument("--dest_instance", required=True)
    from_s3_parser.add_argument("--source_path", required=True)
    from_s3_parser.add_argument("--dest_path", required=True)
    from_s3_parser.add_argument("--exclude", dest="exclude_patterns")
    from_s3_parser.add_argument("--archive", action="store_true")
    from_s3_parser.add_argument("--aws_profile")

    # Upload to S3
    to_s3_parser = subparsers.add_parser("to_s3", help="Upload files/folders from an instance to S3")
    to_s3_parser.add_argument("--source_instance", required=True)
    to_s3_parser.add_argument("--source_path", required=True)
    to_s3_parser.add_argument("--dest_path", required=True)
    to_s3_parser.add_argument("--exclude", dest="exclude_patterns")
    to_s3_parser.add_argument("--archive", action="store_true")
    to_s3_parser.add_argument("--aws_profile")

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
            vm.run_ssh_command(args.instance, args.ssh_command)
        else:
            vm.connect_ssh(args.instance)
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
    elif args.command == "transfer":
        dest_path = args.dest_path or args.source_path
        if args.method == "rsync":
            rsync.transfer_via_rsync(
                source_instance=args.source_instance,
                dest_instance=args.dest_instance,
                source_path=args.source_path,
                dest_path=dest_path,
                exclude=args.exclude_patterns or "",
                rsync_opts=args.rsync_opts,
                archive=args.archive,
                config=vm.config,
            )
        elif args.method == "s3":
            s3.transfer_via_s3(
                source_instance=args.source_instance,
                dest_instance=args.dest_instance,
                source_path=args.source_path,
                dest_path=dest_path,
                s3_bucket=vm.personal_config["s3_bucket"],
                aws_profile=vm.personal_config.get("aws_profile", "default"),
                exclude=args.exclude_patterns or "",
                archive=args.archive,
                config=vm.config,
            )
    elif args.command == "from_s3":
        s3.download_from_s3(
            dest_instance=args.dest_instance,
            source_path=args.source_path,
            dest_path=args.dest_path,
            aws_profile=args.aws_profile or vm.personal_config.get("aws_profile", "default"),
            exclude=args.exclude_patterns or "",
            archive=args.archive,
            config=vm.config,
        )
    elif args.command == "to_s3":
        s3.upload_to_s3(
            source_instance=args.source_instance,
            source_path=args.source_path,
            dest_path=args.dest_path,
            aws_profile=args.aws_profile or vm.personal_config.get("aws_profile", "default"),
            exclude=args.exclude_patterns or "",
            archive=args.archive,
            config=vm.config,
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