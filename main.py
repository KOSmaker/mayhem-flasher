#!/usr/bin/env python3
"""Small GTK front end for the HackRF SPI flasher."""

import os
import json
import glob
import queue
import re
import select
import shutil
import subprocess
import termios
import tempfile
import threading
import time
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf

SCREEN_RGB = tuple(
    bytes((red * 85, green * 85, blue * 85))
    for red in range(4)
    for green in range(4)
    for blue in range(4)
)


class MayhemSerial:
    """Minimal POSIX serial transport for Mayhem's USB shell protocol."""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = termios.B115200
        attrs[5] = termios.B115200
        # With VMIN=0, some USB serial drivers return b"" immediately when
        # there is simply no data yet. That looks like EOF even though the
        # PortaPack is still connected. Let select() wait for at least one byte.
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.rx = bytearray()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _read_until(self, marker, timeout, stop_event=None):
        deadline = time.monotonic() + timeout
        while marker not in self.rx:
            if stop_event and stop_event.is_set():
                raise InterruptedError("Screen sharing stopped")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for device response: {marker!r}")
            ready, _, _ = select.select([self.fd], [], [], min(remaining, 0.5))
            if not ready:
                continue
            try:
                data = os.read(self.fd, 16384)
            except BlockingIOError:
                continue
            if not data:
                raise OSError("PortaPack serial connection closed")
            self.rx.extend(data)
        end = self.rx.index(marker) + len(marker)
        result = bytes(self.rx[:end])
        del self.rx[:end]
        return result

    def screen_frame(self, width, height, timeout=30):
        self.write_all(b"screenframeshort\r")
        response = self._read_until(b"\r\nok\r\nch> ", timeout)
        rows = [
            line for line in response.split(b"\r\n")
            if len(line) == width and all(32 <= pixel <= 95 for pixel in line)
        ]
        if len(rows) != height:
            raise RuntimeError(
                f"Expected {height} screen rows of {width} pixels; received {len(rows)}"
            )
        return b"".join(
            SCREEN_RGB[pixel - 32]
            for row in rows
            for pixel in row
        )

    def command(self, text, timeout=15):
        self.write_all(text.encode("ascii") + b"\r")
        try:
            return self._read_until(b"ch> ", timeout)
        except TimeoutError as exc:
            raise TimeoutError(f"No shell prompt after command {text!r}: {exc}") from exc

    def wait_for_shell(self, timeout=8):
        """Consume the startup banner and synchronize on the ready prompt."""
        try:
            return self._read_until(b"ch> ", timeout)
        except TimeoutError:
            # Some firmware builds print the prompt only after receiving CR.
            self.write_all(b"\r")
            try:
                return self._read_until(b"ch> ", 5)
            except TimeoutError as exc:
                raise TimeoutError(
                    "Mayhem shell did not present a ready prompt after opening the serial port"
                ) from exc

    def write_all(self, data, on_progress=None):
        view = memoryview(data)
        total = len(data)
        written_total = 0
        while view:
            try:
                count = os.write(self.fd, view)
                view = view[count:]
                written_total += count
                if on_progress:
                    on_progress(written_total, total)
            except BlockingIOError:
                ready, _, _ = select.select([], [self.fd], [], 30)
                if not ready:
                    raise TimeoutError(
                        "Timed out writing to PortaPack serial port "
                        f"({written_total} of {total} bytes accepted for this write)"
                    )

    def upload(self, remote_path, local_path, progress):
        total = os.path.getsize(local_path)
        progress(0, total, "Preparing the firmware file on the PortaPack…", False)
        progress(0, total, "Closing any previously opened file…", False)
        self.command("fclose")
        progress(0, total, "Preparing the /FIRMWARE folder…", False)
        self.command("mkdir /FIRMWARE")
        progress(0, total, "Opening the firmware package on the SD card…", False)
        self.command(f"fopen {remote_path}")
        progress(0, total, "Rewinding the destination file…", False)
        self.command("fseek 0")
        sent = 0
        with open(local_path, "rb") as source:
            while True:
                # Keep each fwb transaction below the observed Linux CDC-ACM
                # backpressure point. Firmware consumes binary input in
                # 64-byte pieces; frequent acknowledgements let the shell and
                # SD writer drain between groups of USB packets.
                chunk = source.read(512)
                if not chunk:
                    break
                progress(sent, total, f"Requesting transfer block at {sent:,} bytes…", False)
                self.write_all(f"fwb {len(chunk)}".encode("ascii") + b"\r")
                prompt = self._read_until(b" bytes\r\n", 15)
                if b"send " not in prompt.lower():
                    raise RuntimeError("PortaPack did not enter binary upload mode: " + prompt.decode("utf-8", "replace"))
                progress(sent, total, f"Sending block ({len(chunk):,} bytes)…", False)

                def report_block_progress(block_sent, block_size):
                    overall = sent + block_sent
                    progress(
                        overall,
                        total,
                        f"Sending block: {block_sent:,} of {block_size:,} bytes…",
                        False,
                    )

                # Mayhem Hub uses 64-byte writes with a 2 ms pause on systems
                # where larger serial writes can overrun the PortaPack's USB
                # receive queue. Use the same conservative pacing here, and
                # wait for Linux to drain the tty transmit queue before
                # waiting for the device's acknowledgement.
                for offset in range(0, len(chunk), 64):
                    block = chunk[offset : offset + 64]
                    try:
                        self.write_all(
                            block,
                            lambda block_sent, _block_size, base=offset: report_block_progress(
                                base + block_sent, len(chunk)
                            ),
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"Failed sending package data at offset {sent + offset:,} bytes: {exc}"
                        ) from exc
                    # This conservative interval previously let Linux complete
                    # many blocks without overrunning the PortaPack USB queue.
                    time.sleep(0.002)
                termios.tcdrain(self.fd)
                # The Mayhem shell's fwb handler prints "ok\r\n" after it
                # has consumed and written the requested binary bytes, then
                # returns to the normal shell prompt. It does not print a byte
                # count here.
                block_response = self._read_until(b"ch> ", max(15, len(chunk) / 5000))
                if b"ok\r\n" not in block_response.lower():
                    raise RuntimeError(
                        "PortaPack did not confirm the binary block: "
                        + block_response.decode("utf-8", "replace")
                    )
                sent += len(chunk)
                log_update = sent == total or sent % (64 * 1024) == 0
                progress(sent, total, f"Transferred {sent:,} of {total:,} bytes.", log_update)
        self.command("fclose")


class MayhemFlasher(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="app.mayhem.Flasher")
        self.window = None
        self.device_output = ""
        self.download_temp = None
        self.releases = {}
        self.serial_paths = []
        self.dark_theme = self.load_theme_preference()
        self.screen_stop_event = None
        self.screen_commands = queue.Queue()
        self.screen_connected = False
        self.screen_width = 240
        self.screen_height = 320

    def do_activate(self):
        if self.window:
            self.window.present()
            return

        self.window = Gtk.ApplicationWindow(application=self, title="Mayhem Firmware Flasher")
        self.window.set_default_size(860, 700)
        self.apply_theme()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(18)
        root.set_margin_bottom(18)
        root.set_margin_start(18)
        root.set_margin_end(18)
        self.window.set_child(root)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        root.append(title_row)
        title = Gtk.Label(label="HackRF + PortaPack Mayhem")
        title.add_css_class("title-1")
        title.set_xalign(0)
        title.set_hexpand(True)
        title_row.append(title)

        self.theme_label = Gtk.Label(label="Dark" if self.dark_theme else "Light")
        title_row.append(self.theme_label)
        self.theme_switch = Gtk.Switch(active=self.dark_theme)
        self.theme_switch.set_valign(Gtk.Align.CENTER)
        self.theme_switch.set_tooltip_text("Switch between dark and light appearance")
        self.theme_switch.connect("notify::active", self.on_theme_changed)
        title_row.append(self.theme_switch)

        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)
        root.append(notebook)
        flash_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        flash_page.set_margin_top(12)
        flash_page.set_margin_bottom(8)
        flash_page.set_margin_start(8)
        flash_page.set_margin_end(8)
        notebook.append_page(flash_page, Gtk.Label(label="Flash Utility"))
        remote_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        remote_page.set_margin_top(12)
        remote_page.set_margin_bottom(8)
        remote_page.set_margin_start(8)
        remote_page.set_margin_end(8)
        notebook.append_page(remote_page, Gtk.Label(label="Screen & Controls"))

        intro = Gtk.Label(label=(
            "Download and install a complete Mayhem firmware release, including SD-card apps, over USB.\n"
            "No browser or Chromium is needed."
        ))
        intro.set_xalign(0)
        intro.set_wrap(True)
        flash_page.append(intro)

        warning = Gtk.Label(label=(
            "For a complete update, leave the PortaPack in normal Mayhem mode: the app uploads a "
            ".ppfw.tar over USB serial and asks Mayhem to install it, including external apps. "
            "The local .bin button below is the separate HackRF-mode firmware-only path."
        ))
        warning.set_xalign(0)
        warning.set_wrap(True)
        warning.add_css_class("card")
        flash_page.append(warning)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        flash_page.append(row)
        self.scan_button = Gtk.Button(label="Check device")
        self.scan_button.connect("clicked", self.on_check_device)
        row.append(self.scan_button)
        self.flash_button = Gtk.Button(label="Choose .bin and flash…")
        self.flash_button.add_css_class("suggested-action")
        self.flash_button.connect("clicked", self.on_choose_firmware)
        row.append(self.flash_button)

        release_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        flash_page.append(release_row)
        self.serial_choice = Gtk.ComboBoxText()
        self.serial_choice.set_hexpand(True)
        release_row.append(self.serial_choice)
        self.serial_refresh_button = Gtk.Button(label="Refresh ports")
        self.serial_refresh_button.connect("clicked", self.on_refresh_serial)
        release_row.append(self.serial_refresh_button)
        self.refresh_serial_ports()

        firmware_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        flash_page.append(firmware_row)
        self.release_choice = Gtk.ComboBoxText()
        self.release_choice.set_hexpand(True)
        self.release_choice.append("stable", "Stable release")
        self.release_choice.append("nightly", "Latest nightly")
        self.release_choice.set_active_id("stable")
        firmware_row.append(self.release_choice)
        self.load_button = Gtk.Button(label="Load releases")
        self.load_button.connect("clicked", self.on_load_releases)
        firmware_row.append(self.load_button)
        self.download_button = Gtk.Button(label="Download & full update")
        self.download_button.add_css_class("suggested-action")
        self.download_button.connect("clicked", self.on_full_update)
        firmware_row.append(self.download_button)

        remote_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        remote_row.set_vexpand(True)
        remote_page.append(remote_row)

        self.screen_picture = Gtk.Picture()
        self.screen_picture.set_size_request(240, 320)
        self.screen_picture.set_hexpand(True)
        self.screen_picture.set_vexpand(True)
        self.screen_picture.set_can_shrink(True)
        if hasattr(self.screen_picture, "set_content_fit"):
            self.screen_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.screen_picture.set_tooltip_text("Click the screen to send a touch event")
        self.screen_picture.add_css_class("card")
        remote_row.append(self.screen_picture)
        touch = Gtk.GestureClick()
        touch.connect("released", self.on_screen_touched)
        self.screen_picture.add_controller(touch)

        remote_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        remote_controls.set_valign(Gtk.Align.CENTER)
        remote_row.append(remote_controls)
        self.screen_button = Gtk.Button(label="Start screen sharing")
        self.screen_button.connect("clicked", self.on_screen_toggle)
        remote_controls.append(self.screen_button)
        self.remote_status = Gtk.Label(label="Connect to the selected PortaPack to view and control it.")
        self.remote_status.set_xalign(0)
        self.remote_status.set_wrap(True)
        remote_controls.append(self.remote_status)

        directions = Gtk.Grid(column_spacing=6, row_spacing=6)
        directions.set_halign(Gtk.Align.CENTER)
        remote_controls.append(directions)
        for label, command, column, row_index in (
            ("↑", "button 4", 1, 0),
            ("←", "button 2", 0, 1),
            ("Select", "button 5", 1, 1),
            ("→", "button 1", 2, 1),
            ("↓", "button 3", 1, 2),
        ):
            button = Gtk.Button(label=label)
            button.set_size_request(76, 48)
            button.connect("clicked", self.on_remote_button, command)
            directions.attach(button, column, row_index, 1, 1)

        rotary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rotary.set_halign(Gtk.Align.CENTER)
        remote_controls.append(rotary)
        for label, command in (("Rotary −", "button 7"), ("Rotary +", "button 8")):
            button = Gtk.Button(label=label)
            button.connect("clicked", self.on_remote_button, command)
            rotary.append(button)

        self.status = Gtk.Label(label="Ready. For full updates, use normal Mayhem mode and select its serial port.")
        self.status.set_xalign(0)
        self.status.set_wrap(True)
        flash_page.append(self.status)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        flash_page.append(scroller)
        self.output = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.output.set_top_margin(8)
        self.output.set_bottom_margin(8)
        self.output.set_left_margin(8)
        self.output.set_right_margin(8)
        scroller.set_child(self.output)
        self.append_log("No device operation has been run yet.")

        self.window.present()

    def load_theme_preference(self):
        settings_path = os.path.join(GLib.get_user_config_dir(), "mayhem-flasher", "settings.json")
        try:
            with open(settings_path, "r", encoding="utf-8") as settings_file:
                return bool(json.load(settings_file).get("dark_theme", False))
        except (OSError, ValueError, TypeError):
            return False

    def apply_theme(self):
        settings = Gtk.Settings.get_default()
        if settings:
            try:
                if settings.find_property("gtk-interface-color-scheme"):
                    scheme = Gtk.InterfaceColorScheme.DARK if self.dark_theme else Gtk.InterfaceColorScheme.LIGHT
                    settings.set_property("gtk-interface-color-scheme", scheme)
                else:
                    settings.set_property("gtk-application-prefer-dark-theme", self.dark_theme)
            except (TypeError, AttributeError, ValueError):
                pass

    def on_theme_changed(self, switch, _property):
        self.dark_theme = switch.get_active()
        self.theme_label.set_text("Dark" if self.dark_theme else "Light")
        self.apply_theme()
        settings_dir = os.path.join(GLib.get_user_config_dir(), "mayhem-flasher")
        try:
            os.makedirs(settings_dir, exist_ok=True)
            with open(os.path.join(settings_dir, "settings.json"), "w", encoding="utf-8") as settings_file:
                json.dump({"dark_theme": self.dark_theme}, settings_file)
        except OSError as exc:
            self.append_log(f"Could not save theme preference: {exc}")

    def append_log(self, message):
        buf = self.output.get_buffer()
        end = buf.get_end_iter()
        buf.insert(end, message.rstrip() + "\n")

    def run_command(self, args, on_done):
        def work():
            try:
                result = subprocess.run(args, capture_output=True, text=True, timeout=30)
                output = (result.stdout + result.stderr).strip()
                GLib.idle_add(on_done, result.returncode, output or "Command finished with no output.")
            except FileNotFoundError:
                GLib.idle_add(on_done, 127, "HackRF tools not found. On Arch Linux install them with: sudo pacman -S hackrf")
            except subprocess.TimeoutExpired:
                GLib.idle_add(on_done, 124, "The command timed out. Check USB connection and device mode.")
            except Exception as exc:
                GLib.idle_add(on_done, 1, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def on_check_device(self, _button):
        if self.screen_stop_event and not self.screen_stop_event.is_set():
            self.status.set_text("Stop screen sharing before checking the serial shell.")
            return
        port = self.serial_choice.get_active_id()
        if not port:
            self.status.set_text("No serial port selected. Refresh ports with the PortaPack in normal Mayhem mode.")
            return
        self.status.set_text("Checking the Mayhem serial shell…")

        def probe():
            shell = None
            try:
                shell = MayhemSerial(port)
                shell.wait_for_shell()
                output = shell.command("info").decode("utf-8", "replace")
                if "Mayhem Version:" not in output:
                    raise RuntimeError("The shell prompt responded, but the info command returned no Mayhem version.")
                GLib.idle_add(self.device_checked, 0, output)
            except Exception as exc:
                GLib.idle_add(self.device_checked, 1, str(exc))
            finally:
                if shell:
                    shell.close()

        threading.Thread(target=probe, daemon=True).start()

    def device_checked(self, code, output):
        self.device_output = output
        self.append_log("$ info\n" + output)
        if code == 0 and "ch> " in output:
            version = next((line.strip() for line in output.splitlines() if "Mayhem Version:" in line), "Mayhem shell responded.")
            self.status.set_text(f"PortaPack Mayhem is responding: {version}")
        else:
            self.status.set_text("Mayhem shell did not respond. Reboot into normal Mayhem mode and check serial permissions.")

    def on_screen_toggle(self, _button):
        if self.screen_stop_event and not self.screen_stop_event.is_set():
            self.screen_button.set_sensitive(False)
            self.screen_button.set_label("Stopping…")
            self.screen_stop_event.set()
            return
        if not self.download_button.get_sensitive():
            self.remote_status.set_text("Wait for the current device operation to finish first.")
            return

        port = self.serial_choice.get_active_id()
        if not port:
            self.remote_status.set_text("Select the PortaPack serial port first.")
            return

        self.screen_stop_event = threading.Event()
        self.screen_commands = queue.Queue()
        self.screen_connected = False
        self.screen_button.set_label("Stop screen sharing")
        self.remote_status.set_text("Connecting to Mayhem…")
        threading.Thread(
            target=self.screen_worker,
            args=(port, self.screen_stop_event),
            daemon=True,
        ).start()

    def screen_worker(self, port, stop_event):
        shell = None
        error = None
        try:
            shell = MayhemSerial(port)
            shell.wait_for_shell()
            identity = shell.command("info").decode("utf-8", "replace")
            if "Mayhem Version:" not in identity:
                raise RuntimeError("The connected serial device did not identify as Mayhem.")
            resolution = shell.command("getres").decode("utf-8", "replace")
            match = re.search(r"(\d+)\s*x\s*(\d+)", resolution)
            if not match:
                raise RuntimeError("Could not read the PortaPack screen resolution.")
            width, height = map(int, match.groups())
            GLib.idle_add(self.screen_stream_ready, width, height)

            while not stop_event.is_set():
                try:
                    command = self.screen_commands.get_nowait()
                except queue.Empty:
                    command = None
                if command:
                    response = shell.command(command).decode("utf-8", "replace")
                    GLib.idle_add(self.remote_command_finished, command, response)

                frame = shell.screen_frame(width, height)
                GLib.idle_add(self.show_screen_frame, frame, width, height)
                stop_event.wait(0.08)
        except InterruptedError:
            pass
        except Exception as exc:
            error = str(exc)
        finally:
            if shell:
                shell.close()
            GLib.idle_add(self.screen_stream_ended, error)

    def screen_stream_ready(self, width, height):
        self.screen_connected = True
        self.screen_width = width
        self.screen_height = height
        self.screen_picture.set_size_request(width, height)
        self.remote_status.set_text(
            f"Connected · {width} × {height}. Click the screen or use the controls."
        )
        return GLib.SOURCE_REMOVE

    def show_screen_frame(self, rgb, width, height):
        pixel_bytes = GLib.Bytes.new(rgb)
        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
            pixel_bytes,
            GdkPixbuf.Colorspace.RGB,
            False,
            8,
            width,
            height,
            width * 3,
        )
        self.screen_picture.set_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
        return GLib.SOURCE_REMOVE

    def screen_stream_ended(self, error):
        self.screen_connected = False
        self.screen_stop_event = None
        self.screen_button.set_sensitive(True)
        self.screen_button.set_label("Start screen sharing")
        self.remote_status.set_text(error or "Screen sharing stopped.")
        if error:
            self.append_log(f"Screen sharing stopped: {error}")
        return GLib.SOURCE_REMOVE

    def on_remote_button(self, _button, command):
        if not self.screen_connected:
            self.remote_status.set_text("Start screen sharing before sending controls.")
            return
        self.screen_commands.put(command)

    def on_screen_touched(self, _gesture, _presses, x, y):
        if not self.screen_connected:
            self.remote_status.set_text("Start screen sharing before sending touch input.")
            return
        width = self.screen_picture.get_width()
        height = self.screen_picture.get_height()
        if width <= 0 or height <= 0:
            return
        scale = min(width / self.screen_width, height / self.screen_height)
        image_width = self.screen_width * scale
        image_height = self.screen_height * scale
        left = (width - image_width) / 2
        top = (height - image_height) / 2
        if not (left <= x < left + image_width and top <= y < top + image_height):
            return
        screen_x = int((x - left) / scale)
        screen_y = int((y - top) / scale)
        self.screen_commands.put(f"touch {screen_x} {screen_y}")

    def remote_command_finished(self, command, response):
        self.remote_status.set_text(f"Sent {command} · {response.strip().splitlines()[-1] if response.strip() else 'OK'}")
        return GLib.SOURCE_REMOVE

    def on_load_releases(self, _button):
        self.load_button.set_sensitive(False)
        self.status.set_text("Checking GitHub for stable and nightly Mayhem releases…")
        threading.Thread(target=self.fetch_releases, daemon=True).start()

    @staticmethod
    def github_json(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mayhem-Flasher/0.2", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.load(response)

    def fetch_releases(self):
        try:
            stable = self.github_json("https://api.github.com/repos/portapack-mayhem/mayhem-firmware/releases/latest")
            nightlies = self.github_json("https://api.github.com/repos/portapack-mayhem/mayhem-firmware/releases?per_page=30")
            nightly = next((r for r in nightlies if r.get("prerelease") and r.get("tag_name", "").startswith("nightly-tag-")), None)
            found = {}
            for key, release in (("stable", stable), ("nightly", nightly)):
                if release:
                    asset = next((a for a in release.get("assets", [])
                                  if a.get("name", "").startswith("OCI_hackrf_mayhem_")
                                  and a.get("name", "").endswith(".ppfw.tar")), None)
                    if asset:
                        found[key] = (release, asset)
            GLib.idle_add(self.releases_loaded, found, None)
        except Exception as exc:
            GLib.idle_add(self.releases_loaded, {}, str(exc))

    def releases_loaded(self, found, error):
        self.load_button.set_sensitive(True)
        self.releases = found
        self.release_choice.remove_all()
        for key, label in (("stable", "Stable release"), ("nightly", "Latest nightly")):
            if key in found:
                release, _asset = found[key]
                version = release.get("tag_name", release.get("name", "release"))
                self.release_choice.append(key, f"{label}: {version}")
        if found:
            selected = "stable" if "stable" in found else "nightly"
            self.release_choice.set_active_id(selected)
            self.status.set_text("Release list loaded from the official Mayhem GitHub releases.")
        else:
            self.release_choice.append("stable", "Stable release")
            self.release_choice.append("nightly", "Latest nightly")
            self.release_choice.set_active_id("stable")
            self.status.set_text("Could not load releases: " + (error or "no compatible firmware archives found"))
        return GLib.SOURCE_REMOVE

    def refresh_serial_ports(self):
        current = self.serial_choice.get_active_id() if hasattr(self, "serial_choice") else None
        candidates = sorted(set(
            glob.glob("/dev/serial/by-id/*")
            + glob.glob("/dev/ttyACM*")
            + glob.glob("/dev/ttyUSB*")
        ), key=lambda path: (0 if path.startswith("/dev/serial/by-id/") else 1, path))
        by_device = {}
        for path in candidates:
            by_device.setdefault(os.path.realpath(path), path)
        ports = sorted(by_device.values())
        self.serial_paths = ports
        self.serial_choice.remove_all()
        for path in ports:
            self.serial_choice.append(path, path)
        if current in ports:
            self.serial_choice.set_active_id(current)
        elif ports:
            self.serial_choice.set_active(0)

    def on_refresh_serial(self, _button):
        self.refresh_serial_ports()
        self.status.set_text("Serial port list refreshed. Select the PortaPack in normal Mayhem mode.")

    def on_full_update(self, _button):
        if self.screen_stop_event and not self.screen_stop_event.is_set():
            self.status.set_text("Stop screen sharing before starting a firmware update.")
            return
        key = self.release_choice.get_active_id()
        if key not in self.releases:
            self.status.set_text("Load the release list first, and make sure you are online.")
            return
        port = self.serial_choice.get_active_id()
        if not port:
            self.status.set_text("No serial port found. Keep Mayhem in normal mode, then click Refresh ports.")
            return
        release, asset = self.releases[key]
        self.confirm_dialog(
            f"Install {release.get('name', release.get('tag_name'))}?",
            f"Official full package: {asset['name']}\nSerial device: {port}\n\n"
            "The app will download the .ppfw.tar, transfer it to the PortaPack, and ask Mayhem to install it. "
            "Leave the device in normal Mayhem mode and do not disconnect it during transfer or installation.",
            "Download & install",
            self.full_update_confirmed,
            (key, port),
        )

    def confirm_dialog(self, title, detail, action_label, callback, callback_data):
        dialog = Gtk.Dialog(title=title, transient_for=self.window, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(action_label, Gtk.ResponseType.ACCEPT)
        dialog.set_default_size(440, -1)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)
        label = Gtk.Label(label=detail)
        label.set_xalign(0)
        label.set_wrap(True)
        content.append(label)
        dialog.get_content_area().append(content)
        dialog.connect("response", callback, callback_data)
        dialog.present()

    def full_update_confirmed(self, dialog, response, data):
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT:
            return
        self.set_busy(True)
        key, port = data
        release, asset = self.releases[key]
        self.status.set_text("Downloading full firmware and app package…")
        self.append_log(f"Downloading {release.get('name', release.get('tag_name'))}: {asset['name']}")
        threading.Thread(target=self.download_full_package, args=(asset, port), daemon=True).start()

    def download_full_package(self, asset, port):
        try:
            self.download_temp = tempfile.TemporaryDirectory(prefix="mayhem-flasher-")
            package_name = os.path.basename(asset["name"])
            package = os.path.join(self.download_temp.name, package_name)
            req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "Mayhem-Flasher/0.2"})
            with urllib.request.urlopen(req, timeout=60) as response, open(package, "wb") as out:
                shutil.copyfileobj(response, out)
            GLib.idle_add(self.status.set_text, "Download complete. Connecting to Mayhem serial shell…")
            remote_path = "/FIRMWARE/" + package_name
            shell = MayhemSerial(port)
            try:
                shell.wait_for_shell()
                identity = shell.command("info").decode("utf-8", "replace")
                GLib.idle_add(self.append_log, "$ info\n" + identity)
                if "ch> " not in identity or "Mayhem Version:" not in identity:
                    raise RuntimeError("The shell did not return complete Mayhem info; stopping before upload.")
                GLib.idle_add(self.append_log, "Connected to the Mayhem serial shell.")
                shell.upload(remote_path, package, self.update_transfer_progress)
                GLib.idle_add(self.status.set_text, "Package transferred. Asking Mayhem to install it…")
                shell.write_all(f"flash {remote_path}".encode("ascii") + b"\r")
                try:
                    response = shell._read_until(b"ch> ", 180)
                    decoded = response.decode("utf-8", "replace")
                    if "error" in decoded.lower() or "failed" in decoded.lower():
                        GLib.idle_add(self.full_update_finished, False, decoded)
                    else:
                        GLib.idle_add(self.full_update_finished, True, decoded)
                except (OSError, TimeoutError):
                    GLib.idle_add(
                        self.full_update_finished,
                        True,
                        "The device closed the serial connection after the install command. It may be rebooting; "
                        "leave it powered and check the PortaPack screen.",
                    )
            finally:
                shell.close()
        except Exception as exc:
            GLib.idle_add(self.full_update_finished, False, f"Full update failed: {exc}")

    def update_transfer_progress(self, sent, total, stage, log_update=False):
        percent = sent * 100 / total if total else 100
        GLib.idle_add(self.show_transfer_progress, percent, sent, total, stage, log_update)

    def show_transfer_progress(self, percent, sent, total, stage, log_update):
        self.status.set_text(f"{stage} {percent:.1f}% ({sent:,} / {total:,} bytes)")
        if log_update:
            self.append_log(f"{percent:.1f}% — {sent:,} / {total:,} bytes transferred")
        return GLib.SOURCE_REMOVE

    def full_update_finished(self, success, output):
        self.append_log(output)
        self.set_busy(False)
        if self.download_temp:
            self.download_temp.cleanup()
            self.download_temp = None
        if success:
            self.status.set_text("Mayhem accepted the full update. Keep it powered while it installs and reboots.")
        elif "permission denied" in output.lower():
            self.status.set_text(
                "Linux denied serial-port access. On Arch, run: sudo usermod -aG uucp $USER, "
                "then fully log out and back in before reopening this app."
            )
        else:
            self.status.set_text(output)
        return GLib.SOURCE_REMOVE

    def set_busy(self, busy):
        self.flash_button.set_sensitive(not busy)
        self.scan_button.set_sensitive(not busy)
        self.load_button.set_sensitive(not busy)
        self.download_button.set_sensitive(not busy)
        self.serial_refresh_button.set_sensitive(not busy)
        self.screen_button.set_sensitive(not busy)

    def release_flash_finished(self, code, output):
        self.append_log(output)
        self.set_busy(False)
        if self.download_temp:
            self.download_temp.cleanup()
            self.download_temp = None
        if code == 0:
            self.status.set_text("Flashing command completed. Follow the Mayhem release notes, then reboot the device.")
        else:
            self.status.set_text("Download or flash failed. Read the output below; keep the device connected until safe.")
        return GLib.SOURCE_REMOVE

    def on_choose_firmware(self, _button):
        if self.screen_stop_event and not self.screen_stop_event.is_set():
            self.status.set_text("Stop screen sharing before starting a firmware operation.")
            return
        if not shutil.which("hackrf_spiflash"):
            self.status.set_text("HackRF tools are not installed. On Arch: sudo pacman -S hackrf")
            return
        dialog = Gtk.FileChooserNative(
            title="Choose Mayhem firmware (.bin)",
            transient_for=self.window,
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Choose",
            cancel_label="Cancel",
        )
        filt = Gtk.FileFilter()
        filt.set_name("Firmware .bin files")
        filt.add_pattern("*.bin")
        dialog.add_filter(filt)
        dialog.connect("response", self.firmware_selected)
        dialog.show()

    def firmware_selected(self, dialog, response):
        if response != Gtk.ResponseType.ACCEPT:
            dialog.destroy()
            return
        file = dialog.get_file()
        path = file.get_path() if file else None
        dialog.destroy()
        if not path or not path.lower().endswith(".bin"):
            self.status.set_text("Please choose a Mayhem firmware file ending in .bin.")
            return
        self.confirm_flash(path)

    def confirm_flash(self, path):
        self.confirm_dialog(
            "Flash this firmware to the device?",
            f"File: {os.path.basename(path)}\n\n"
            "The device must be in HackRF mode. Do not unplug it during flashing. "
            "This writes the HackRF SPI firmware and does not update SD card contents.",
            "Flash firmware",
            self.flash_confirmed,
            path,
        )

    def flash_confirmed(self, dialog, response, path):
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT:
            return
        self.flash_button.set_sensitive(False)
        self.scan_button.set_sensitive(False)
        self.status.set_text("Flashing… keep the device connected.")
        self.append_log(f"$ hackrf_spiflash -w {path}")
        self.run_command(["hackrf_spiflash", "-w", path], self.flash_finished)

    def flash_finished(self, code, output):
        self.append_log(output)
        self.flash_button.set_sensitive(True)
        self.scan_button.set_sensitive(True)
        if code == 0:
            self.status.set_text("Flashing command completed. Follow Mayhem's release notes, then restart the PortaPack.")
        else:
            self.status.set_text("Flash failed. Read the output below; do not disconnect while the device is active.")


if __name__ == "__main__":
    app = MayhemFlasher()
    raise SystemExit(app.run())
