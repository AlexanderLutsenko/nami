# Default Templates

This directory contains the default command templates.

## Available Templates

- **setup_aws.bash** - Install and configure AWS CLI with credentials
- **setup_conda.bash** - Install Anaconda
- **setup_tmux.bash** - Setup tmux sessions
- **sync_to_s3.bash** - Upload data to S3
- **sync_from_s3.bash** - Download data from S3
- **rsync_upload.bash** - Upload data via rsync
- **rsync_download.bash** - Download data via rsync

## Template Format

Templates use `${variable_name}` syntax for variable substitution. Variables are resolved in this priority order (highest priority first):
1. Command-line variables (`--var key=value`) 
2. Personal config (`personal.yaml`)
3. Global config (`config.yaml` variables section)

## Custom Templates

To add custom templates, place `.bash` files in your `~/.nami/templates/` directory. 

Templates with the same name as defaults will override the built-in versions. The templates in this directory serve as fallbacks when no custom template is found. 