# Mayhem Firmware Flasher

A small native GTK 4 desktop app for Linux that works with a HackRF One and
PortaPack running Mayhem firmware. It can download and install official Stable
or Nightly full-update packages, show and control the PortaPack screen, and
flash a local `.bin` firmware file. It does not use a browser or Chromium.

## Features

- Download and install official Stable or Nightly `.ppfw.tar` packages over
  USB serial. Full packages include firmware and the matching SD-card apps.
- View the PortaPack screen and send touch, direction, select, and rotary
  controls from the **Screen & Controls** tab.
- Flash a local `.bin` file with `hackrf_spiflash` as a separate
  firmware-only operation.
- Switch between dark and light appearance. The choice is saved in
  `~/.config/mayhem-flasher/settings.json`.

Screen refresh can be slow because the PortaPack sends its screen over the
serial connection. The current compact screen format also uses a limited color
palette. Stop screen sharing before starting a firmware operation.

## Requirements

- Arch Linux (or another Linux distribution with GTK 4 and PyGObject).
- A HackRF One with a compatible PortaPack and a USB data cable.
- A recent Mayhem firmware version with USB serial-shell support. Keep the
  PortaPack in normal Mayhem mode for full updates, screen sharing, and remote
  controls.
- Internet access to load and download firmware releases.

For the optional local `.bin` flashing feature, install the `hackrf` tools.

## Install and run on Arch Linux

The project currently installs from source; a pacman/AUR package is not
provided yet.

1. Install the app's system dependencies:

   ```sh
   sudo pacman -S --needed git python python-gobject gtk4
   ```

   For local `.bin` flashing, also install HackRF tools:

   ```sh
   sudo pacman -S --needed hackrf
   ```

2. Clone the repository from its GitHub page. Replace `OWNER` and `REPOSITORY`
   with the account and repository name:

   ```sh
   git clone https://github.com/OWNER/REPOSITORY.git
   cd REPOSITORY
   ```

3. Start the app:

   ```sh
   python main.py
   ```

Do not run the app with `sudo`; run it as your normal desktop user.

## Serial-port permissions on Arch

If the app reports `Permission denied` for `/dev/ttyACM*` or
`/dev/serial/by-id/*`, add your user to Arch's `uucp` group:

```sh
sudo usermod -aG uucp "$USER"
```

Then fully log out of your desktop session and log back in. Check that the
group is active with:

```sh
id -nG
```

Reconnect the PortaPack and click **Refresh ports** in the app.

## Install a full Stable or Nightly update

1. Connect and power on the PortaPack. Leave it in normal Mayhem mode; do not
   enter HackRF mode.
2. Start the app and select the PortaPack serial port. The path usually
   contains `PortaPack_Mayhem` under `/dev/serial/by-id/`.
3. Click **Check device**. Confirm the log shows the Mayhem version.
4. Click **Load releases**, choose Stable or Nightly, then click
   **Download & full update**.
5. Confirm the release and keep the device powered and connected while the
   package transfers, installs, and reboots.

The full update uploads the `.ppfw.tar` to the SD card and asks Mayhem to
install it. It updates the firmware and the matching SD-card apps in the
package. Do not disconnect the USB cable or power off the PortaPack during
transfer or installation.

## Screen sharing and remote controls

1. Keep the PortaPack in normal Mayhem mode and select its serial port.
2. Open the **Screen & Controls** tab and click **Start screen sharing**.
3. Click the displayed screen to send touch input, or use the direction,
   **Select**, and rotary buttons.
4. Click **Stop screen sharing** before updating or flashing firmware.

The screen is transferred as a complete frame, so it may take a while to
refresh. Controls are sent over the same serial connection and are processed
between screen frames.

## Flash a local `.bin` file

This is a separate firmware-only path and does not update SD-card apps:

1. Install the `hackrf` package.
2. Put the HackRF/PortaPack into HackRF mode as required by the Mayhem flashing
   instructions.
3. Click **Choose .bin and flash…**, select a compatible `.bin` file, and
   follow the confirmation dialog.

Only use a firmware file intended for your hardware. Keep the device connected
until flashing finishes.

## Troubleshooting

- **No device or no serial response:** Check the USB data cable, select the
  PortaPack's serial port, use normal Mayhem mode, and click **Check device**.
- **Permission denied:** Follow the Arch `uucp` group steps above, then log out
  and back in.
- **Screen sharing is slow:** This is limited by the screen-frame data sent
  over USB serial. Stop screen sharing while transferring firmware.
- **Transfer was interrupted:** Reboot the PortaPack into normal Mayhem mode
  before trying again. An interrupted upload does not mean the firmware was
  installed; the app only runs the install command after the complete package
  has transferred.
- **`.bin` flashing fails:** Confirm `hackrf` is installed and the HackRF is in
  the required HackRF mode.

## Current scope

This is a lightweight desktop utility, not a complete Mayhem Hub replacement.
It currently provides full-package firmware updates, local `.bin` flashing,
basic screen sharing, and remote screen/button/touch controls. It does not yet
provide Mayhem Hub's file browser or every other Hub feature.
