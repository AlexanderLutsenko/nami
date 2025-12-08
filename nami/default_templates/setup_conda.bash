# Conda installation

# Only install Anaconda if conda is not already available
if which conda; then
    echo "✅ Conda is already installed. Skipping Anaconda installation."
    exit 0
fi 

installer="Anaconda3-2024.10-1-Linux-x86_64.sh"
wget https://repo.anaconda.com/archive/$installer
bash $installer -b -u
rm $installer

# # Explicitly activate Conda for this session (updates PATH)
# . ~/anaconda3/etc/profile.d/conda.sh

# # Now initialize for future shells
# conda init bash

echo "✅ Conda installed successfully." 
