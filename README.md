# FrameIt
FrameIt is a simple Python Flask application which displays Movie posters

This code was written as part of a DYI project of mine, and it meant to be used in a very specific way

I am putting this here in case it is useful for others to do the same. 

# Example
![My Frame](https://cdn.mudhut.social/media_attachments/files/113/462/298/824/058/125/original/29ae776333bc73b9.jpeg)

# Hardware
## Screen
I am using a 15 inch portable screen for my display, and a custom wood frame. You can find these portable monitors on Amazon for somewhat cheap. 

I used 180 degree HDMI and USB-C adapters to connect the monitor to the Pi. You can find these on Amazon as well.

The screen is powered via a 12v to 5v power supply. I did this becasue I have a long run through a wall, and didn't want voltage drop. The power supply works with 9v-24v so I can power it with basically any handy power supply..

## Raspberry Pi
I am using a Raspbery Pi 3B+, powered by a 12v to 5v power supply. 

I am using a 64GB micro SD card, in case I decide to run the server sepratly on each Frame.

# Installing
## Server
The server can be installed seprate from the client, or you can run all 3 services on the same Raspberry Pi. I will post these later, after I have a working Dockerfile.

## Client
I have setup a small, hopefully functional install script for FrameIt. 

It should be as simple as: 
1. Flash a Raspberry Pi SD card or SSD with Rasbian Lite 64-bit
2. Configure SSH if you have not already. I reccomend the Raspi imager as it can set up SSH and SSH keys for you. 
3. Update your Pi, and install our basic dependeencies. 
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install git python3-venv -y
```
4. Configure Pi to boot into a shell (autologin)
   1. Launch raspi-config
   ```bash
   sudo raspi-config 
   ``` 
   2. Go to "System Options" -> "S5 Boot / Auto Login"
   3. Choose "Console Autologin"
   4. Apply and reboot
5. Clone the repo and run the install script
```bash
git clone https://github.com/muddyland/frameIt.git 
cd frameIt
bash scripts/install.sh
```
6. The Install script will install the server and client if requested, and will make them systemd services at the user level. It will also insttall xorg and the required dependencies for a simple UI, as well as Chromium for the browser.

This is still a work in progress, remember, I have a working install, so I have not tested these fully

# License
The code is MIT licensed, I do not care what you do with it. 

More to come as far as install docs and such, I am simply putting this here to have a place to work on it. 