# Tmux Setup
touch /root/.no_auto_tmux
tmux new-session -d -s ${session_name}
echo "✅ Tmux session '${session_name}' created" 