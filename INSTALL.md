# Install on a fresh Raspberry Pi 5

Assumes Raspberry Pi OS (64-bit, Bookworm or later) with the desktop, the AI HAT+ 2 fitted and HailoRT 5.3
installed. About 6 GB of disk is needed.

## 1. Get the code and run setup

```bash
sudo apt-get update && sudo apt-get install -y git
git clone git@github.com:Senthi1Kumar/nova-cabin.git
cd nova-cabin
./setup.sh
```

`./setup.sh` creates `.venv`, installs the Python dependencies, builds the Parakeet library, downloads the models
listed in `models.lock` (checksums verified), routes audio through PipeWire, and runs the doctor. Options:

| Command | Effect |
|---|---|
| `./setup.sh --yes` | answer yes to every question |
| `./setup.sh --no-apt` | skip system packages |
| `./setup.sh --check` | run only the doctor |
| `SETUP_DRY_RUN=1 ./setup.sh` | print every step, execute nothing |

## 2. Configure

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Set `OPENROUTER_API_KEY`. Add `MAPBOX_ACCESS_TOKEN` for live places and routes; `EXA_API_KEY` and
`TYPESAFE_API_KEY` are optional.

## 3. Audio

Nova Cabin reads the microphone and plays through PipeWire. Pick the section for your hardware.

### WM8960 audio HAT

1. Enable the codec overlay in `/boot/firmware/config.txt` (use the overlay that matches your HAT) and reboot.
2. Remove any stale `/etc/asound.conf` left from older HAT drivers and let PipeWire own the card.
3. Confirm the card is visible: `arecord -l` and `wpctl status`.
4. Keep the default echo handling in `.env`:

```
AEC=auto
AEC_FALLBACK=half_duplex
```

The WM8960 has no hardware echo canceller, so the app cancels the speaker signal in software and estimates the
speaker-to-microphone delay on start. If the assistant still hears itself, set `AEC_DELAY_MS` to a measured value, or
lower the speaker volume. `AEC_FALLBACK=half_duplex` mutes the microphone while the assistant speaks, which disables
barge-in but never loops.

### ReSpeaker XVF3800 USB array

1. Plug it in and confirm it appears: `arecord -l` and `lsusb`.
2. Select it by name in `.env`:

```
AUDIO_INPUT_DEVICE=XVF3800
DRIVER_GATE=auto
DRIVER_BEARING_DEG=0
```

3. Allow USB access to the control endpoint (direction of arrival):

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2886", MODE="0666"' | sudo tee /etc/udev/rules.d/99-xvf3800.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

4. Leave `AUDIO_OUTPUT_DEVICE` blank to use the system default output, or name the speaker you want.

The XVF3800 cancels echo in hardware. `DRIVER_BEARING_DEG` is the driver's azimuth relative to the array and
`DRIVER_ZONE_DEG` the accepted spread; speech from outside the zone is ignored.

## 4. Optional hardware

- **GPS:** `sudo apt-get install gpsd gpsd-clients`, then point gpsd at the receiver (for example `/dev/ttyUSB0`).
  Without it the app uses a fixed start position.
- **Face ID (RealSense F455):** install the RealSense ID tools; `./setup.sh --check` reports the firmware state.

## 5. Run and verify

```bash
./setup.sh --check
./run.sh
```

Run the doctor any time with `.venv/bin/python scripts/doctor.py`. Run the offline test suite with
`NOVA_AUDIO_MUTE=1 .venv/bin/python -m pytest tests -q -p no:cacheprovider`.
