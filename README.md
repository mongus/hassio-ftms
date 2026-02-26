# Fitness Machine Service

[Home Assistant](https://www.home-assistant.io/) [HACS](https://hacs.xyz/) custom component for working with fitness equipment with a Bluetooth interface.

The component is based on the [pyftms](https://github.com/dudanov/pyftms) library, which complies with the [Bluetooth Fitness Machine Service v1.0 standard](https://www.bluetooth.com/specifications/specs/fitness-machine-service-1-0/).

Component capabilities:

1. Automatically detect Bluetooth fitness devices nearby, notifying the user about it;
2. Setup Wizard, which allows you to easily configure the device by determining its type and set of sensors in automatic or manual modes. The set of sensors can be changed.
3. Collects training data from fitness equipment and allows you to set training parameters specific to the type of equipment.

Supported fitness machines:

1. **Treadmill**
2. **Cross Trainer** (Elliptical Trainer)
3. **Rower** (Rowing Machine)
4. **Indoor Bike** (Spin Bike)

Device view example for `FitShow FS-BT-D2 Indoor bike` fitness machine:

![image](images/main.png)

## Installation

### HACS

Follow [this guide](https://hacs.xyz/docs/faq/custom_repositories/) to add this git repository as a custom HACS repository. Then install from HACS as normal.

### Manual Installation

Copy `custom_components/ftms` into your Home Assistant `$HA_HOME/config` directory, then restart Home Assistant.

## Strava Integration

This fork adds optional automatic workout upload to Strava. When configured, the integration detects workout sessions, generates TCX files, and uploads them to Strava automatically.

With Strava unconfigured, the integration behaves identically to upstream.

### Setup

1. Create a Strava API application at https://www.strava.com/settings/api
   - Set the "Authorization Callback Domain" to `localhost`
2. In Home Assistant, go to the FTMS device's Options (Settings > Devices > your device > Configure)
3. Select **Strava** from the options menu
4. Enter your Strava Client ID and Client Secret
5. Click the authorization link, approve on Strava, then copy the `code` parameter from the resulting URL and paste it back
6. Configure activity settings (type, name template, privacy, gear)

### How It Works

- **Session start**: Detected automatically when the treadmill reports active workout time
- **During workout**: Trackpoints (distance, speed, calories) are recorded every second
- **Session end**: Detected when workout time stops incrementing for 30 seconds, or on BLE disconnect
- **Upload**: TCX file is generated and uploaded to Strava with the configured activity settings
- **Pending uploads**: If the upload fails or BLE disconnects mid-workout, the TCX file is saved and retried on next boot

### Activity Type Auto-Detection

| Machine Type   | Activity Type                              |
|----------------|--------------------------------------------|
| Treadmill      | Walk (< 6 km/h) or Run (>= 6 km/h)       |
| Cross Trainer  | Elliptical                                 |
| Indoor Bike    | Ride                                       |
| Rower          | Rowing                                     |

You can also set a fixed activity type in the options.

### Name Template

The default activity name is `{activity} on {device}`. Available placeholders:

- `{activity}` — Walk, Run, Ride, etc.
- `{device}` — BLE device name
- `{date}` — YYYY-MM-DD
- `{distance_km}` — total distance in km
- `{duration_min}` — total duration in minutes

### Privacy Options

- **Hide from Home** — hides the activity from followers' feeds
- **Private** — sets visibility to "Only Me"

### New Entities

When Strava is configured, three new entities appear:

- **Session State** sensor — `idle`, `recording`, or `uploading`
- **Last Workout** sensor — summary of the last recorded workout (e.g. "1.2 km, 15 min")
- **Upload Last Workout** button — manually trigger upload of the last saved workout

## Disclaimer

Since there is a lot of different equipment that I do not own, and given the fact that not all manufacturers follow the FTMS standard strictly, some functions may not work correctly or not work at all.
Please create an [issue](https://github.com/dudanov/hassio-ftms/issues), and I will try to help solve the problem.

## Support

If you find the component useful and want to support me and my work, you can do this by sending me a donation in [TONs](https://ton.org/): `UQCji6LsYAYrJP-Rij7SPjJcL0wkblVDmIkoWVpvP2YydnlA`.
