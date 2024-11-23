#!/bin/bash

# Handle errors correctly
set -e

# Check if running as root user
if [ $(id -u) != "0" ]; then
  echo "Running as non-root user. Using Sudo..."
  sudo su -
fi

# Install required packages
sudo apt update
sudo apt install python3-dev xserver-xorg x11-xserver-utils xinit openbox chromium-browser

# Populate ~/.bash_profile with startx command
echo "# Start X-server if no DISPLAY variable is set and vt1 is active" >> ~/.bash_profile
echo "[[ -z $DISPLAY && $XDG_VTNR -eq 1 ]] && startx -- -nocursor" >> ~/.bash_profile

# Create venv at .venv
python3 -m venv .venv
source .venv/bin/activate

# Install Python dependencies using pip
pip install -r requirements.txt

# Run the installation script
./scripts/install_services.py

echo "Installation complete!"
read -p "Would you like to reboot now? (y/n): " REBOOT_CHOICE
if [ "$REBOOT_CHOICE" = "y" ]; then
    echo "Rebooting..."
    sudo shutdown -r now
else
    echo "Exiting without reboot..."
fi