# Panasonic AW Image Adjust Controller

A dark Tkinter desktop UI for adjusting Panasonic AW camera image settings over the camera HTTP CGI interface.

The app provides tabs for:

- **Brightness** controls such as iris, shutter, gain, frame mix, ND filter, and day/night mode.
- **Picture** controls such as white balance, color temperature, pedestal, detail, gamma, DRS, and knee settings.
- **Matrix** controls including matrix type, linear matrix sliders, and color correction saturation/phase sliders.

## Requirements

- Python 3.9 or newer
- `requests`
- Tkinter, which is included with many Python desktop installations but may need to be installed separately on some Linux distributions.

Install the Python dependency with:

```bash
python3 -m pip install requests
```

## Run

```bash
python3 panasonic_control.py
```

## Camera connection settings

At startup, the application uses the default settings defined near the top of `panasonic_control.py`:

- Camera IP: `192.168.103.30`
- Username: `admin`
- Password: `Spain01@`

You can change the camera connection at runtime from the settings row at the top of the UI:

1. Enter the camera IP address or hostname.
2. Update the username and password if needed.
3. Click **Apply camera settings**.

Applying new settings resets the HTTP session and authentication state, updates the status bar, and the next poll/send request uses the new camera address.

## Notes

- The app polls `/live/camdata.html` every 200 ms.
- Control changes are sent to `/cgi-bin/aw_cam` with `cmd` and `res=1` query parameters.
- Slider sends are debounced to reduce request volume while dragging.
- The log panel shows queued commands, send results, poll errors, and optional successful poll messages.
