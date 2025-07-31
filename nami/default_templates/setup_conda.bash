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

echo "✅ Conda installed successfully." 
