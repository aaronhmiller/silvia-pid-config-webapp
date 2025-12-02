# silvia-pid-config-webapp
Allows config via a web app as well as OTA updates for changes to web app code
## Usage
1. Fork this repository
2. create WIFI_CONFIG.py with the following entries:
   ```
   SSID="<YOUR_SSID_HERE>"
   PASSWORD="<YOUR_PASSWORD_HERE>"
   ```
3. in `main.py` update the `firmware_url` to the repo url of your fork
4. also, in the same file, if you wish to run off battery, set the `WIFI_START_HOUR` and `WIFI_END_HOUR` to limit the time the radio runs.

Then, you'll need to load:
```
main.py
ota.py
WIFI_CONFIG.py
```
to your XIAO ESP32C6 device.
## Hardware connections
From the XIAO, connect pins D6 and D7 (GPIO16 (TX) & GPIO17 (RX)) and GND to pins 1 and 2 (GP0 (RX) & GP1 (TX)) and GND to the RP2040
