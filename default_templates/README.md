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

Templates use `${variable_name}` syntax for variable substitution. Variables can be:
- Defined globally in `config.yaml` under the `variables` section
- Defined in `personal.yaml` (overrides global config)
- Passed via `--var key=value` when running templates (highest priority)

## Custom Templates

To add custom templates, place `.bash` files in your `~/.nami/templates/` directory. 

Templates with the same name as defaults will override the built-in versions. The templates in this directory serve as fallbacks when no custom template is found. 