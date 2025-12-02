"""
ESP32 Coffee Controller with Time-Based WiFi - STANDALONE FIXED VERSION v0.9.4
Fixed for reliable standalone operation without Thonny connection

Key Fixes for Standalone Operation:
1. Added WiFi hardware readiness check (wait_for_wifi_ready)
2. Removed non-existent STAT_CONNECT_FAIL constant (ESP32C6 compatibility)
3. Graceful handling of missing status constants with hasattr() checks
4. Smart boot stabilization (3s + WiFi ready check, not 65s)
5. Multiple WiFi connection retry attempts (10 attempts)
6. Better error recovery and logging
7. Immediate server start if booting during WiFi hours
8. Filtered command responses to exclude DataLogger CSV output (v0.9.2)
9. Fixed response timing - reduced sleep from 0.5s to 0.05s to capture CLI responses (v0.9.3)
10. Fixed race condition - clear UART buffer before sending, only break on <<OK/<<ERROR (v0.9.4)

Valid Commands:
- reg coffee [temp]  (defaults to 108°C)
- reg steam [temp]   (defaults to 145°C)
- reg on
- reg off
- heater on
- heater off

Installation:
1. Adjust WIFI_SSID and WIFI_PASSWORD for your network
1. Adjust WIFI_START_HOUR and WIFI_END_HOUR (saves on battery life)
2. Upload: mpremote connect /dev/cu.usbmodem101 fs cp main.py :main.py
3. Reset device (unplug/replug USB-C)
"""

import network
import socket
import machine
import time
import json
import ntptime
from machine import UART, Pin, RTC, Timer
from ota import OTAUpdater
from WIFI_CONFIG import SSID, PASSWORD
firmware_url = "https://raw.githubusercontent.com/aaronhmiller/silvia-pid-config-webapp/"
ota_updater = OTAUpdater(SSID, PASSWORD, firmware_url, "main.py")
ota_updater.download_and_install_update_if_available()

# ============ CONFIGURATION ============
WIFI_SSID = SSID
WIFI_PASSWORD = PASSWORD
WIFI_START_HOUR = 5    # Start hour (24-hour format)
WIFI_END_HOUR = 8     # End hour (24-hour format)
BASE_TIMEZONE_OFFSET = -8  # PST base offset (UTC-8)
UART_BAUDRATE = 115200
BUILTIN_LED = 15       # Yellow user LED on XIAO ESP32C6
GPIO16 = 16
GPIO17 = 17

# NEW: Boot and connection settings
BOOT_DELAY = 3.0  # Seconds to wait after power-on for hardware stabilization
WIFI_CONNECT_RETRIES = 10  # Number of times to retry WiFi connection (increased from 5)
WIFI_CONNECT_TIMEOUT = 10  # Seconds per connection attempt

# ============ HARDWARE SETUP ============
# UART (RX=GPIO17, TX=GPIO16)
uart = UART(1, baudrate=UART_BAUDRATE, tx=GPIO16, rx=GPIO17, timeout=100, rxbuf=1024)
print('UART initialized: RX=GPIO17, TX=GPIO16')

led = Pin(BUILTIN_LED, Pin.OUT)
rtc = RTC()
wlan = network.WLAN(network.STA_IF)

# ============ DAYLIGHT SAVING TIME ============
def get_nth_weekday_of_month(year, month, weekday, n):
    """
    Find the nth occurrence of a weekday in a given month
    weekday: 0=Monday, 6=Sunday
    n: 1=first, 2=second, etc.
    Returns day of month
    """
    # Start with first day of month
    first_day = time.mktime((year, month, 1, 0, 0, 0, 0, 0, 0))
    first_weekday = time.localtime(first_day)[6]  # Day of week for 1st
    
    # Calculate days until first occurrence of target weekday
    days_until_weekday = (weekday - first_weekday) % 7
    
    # Add weeks to get nth occurrence
    target_day = 1 + days_until_weekday + (n - 1) * 7
    return target_day

def is_dst(year, month, day):
    """
    Check if date is during Daylight Saving Time in California
    DST: Second Sunday in March (2 AM) to First Sunday in November (2 AM)
    """
    # Get DST transition dates for this year
    dst_start_day = get_nth_weekday_of_month(year, 3, 6, 2)  # 2nd Sunday in March
    dst_end_day = get_nth_weekday_of_month(year, 11, 6, 1)   # 1st Sunday in November
    
    # Check if current date is in DST period
    if month < 3 or month > 11:
        return False
    elif month > 3 and month < 11:
        return True
    elif month == 3:
        return day >= dst_start_day
    else:  # month == 11
        return day < dst_end_day

def get_timezone_offset():
    """Get current timezone offset accounting for DST"""
    utc_time = time.localtime()
    year, month, day = utc_time[0], utc_time[1], utc_time[2]
    
    if is_dst(year, month, day):
        return BASE_TIMEZONE_OFFSET + 1  # PDT: UTC-7
    else:
        return BASE_TIMEZONE_OFFSET       # PST: UTC-8

# ============ LED PATTERNS ============
def blink():
    """Fast blink - connecting"""
    led.on()
    time.sleep_ms(50)
    led.off()
    time.sleep_ms(50)

def beat():
    """Heartbeat - idle/power saving"""
    led.off()
    time.sleep_ms(500)
    led.on()
    time.sleep_ms(5000)

def pulse(timer):
    """Pulse - show alive"""
    led.toggle()


# ============ TIME MANAGEMENT ============
def sync_time():
    """Sync time from NTP server with retries"""
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            print(f"Syncing time from NTP (attempt {attempt + 1}/{max_attempts})...")
            ntptime.settime()
            print("Time synced successfully")
            return True
        except Exception as e:
            print(f"NTP sync attempt {attempt + 1} failed: {e}")
            if attempt < max_attempts - 1:
                time.sleep(2)
    return False

def get_local_hour():
    """Get current hour in local timezone (with DST adjustment)"""
    utc_time = time.localtime()
    offset = get_timezone_offset()
    local_hour = (utc_time[3] + offset) % 24
    return local_hour

def get_local_time_str():
    """Get formatted local time string with timezone"""
    utc_time = time.localtime()
    offset = get_timezone_offset()
    local_hour = (utc_time[3] + offset) % 24
    tz_name = "PDT" if offset == -7 else "PST"
    return f"{local_hour:02d}:{utc_time[4]:02d}:{utc_time[5]:02d} {tz_name}"
      
def is_wifi_hours():
    """Check if current time is within WiFi active hours"""
    hour = get_local_hour()
    
    # Handle midnight crossing (e.g., 23:00 to 01:00)
    if WIFI_END_HOUR <= WIFI_START_HOUR:
        # Range crosses midnight: active if hour >= start OR hour < end
        in_hours = hour >= WIFI_START_HOUR or hour < WIFI_END_HOUR
    else:
        # Normal range: active if hour >= start AND hour < end
        in_hours = WIFI_START_HOUR <= hour < WIFI_END_HOUR
    
    return in_hours

# ============ WiFi MANAGEMENT ============
def wait_for_wifi_ready():
    """Wait for WiFi hardware to be fully ready after boot"""
    print("Waiting for WiFi hardware to initialize...")
    max_wait = 30  # Maximum 30 seconds
    
    # First, ensure WiFi is active
    if not wlan.active():
        wlan.active(True)
    
    # Wait for WiFi to be ready (not busy initializing)
    start_time = time.time()
    while time.time() - start_time < max_wait:
        try:
            # Try to get status - if this works, WiFi is ready
            status = wlan.status()
            if status != -1:  # -1 might indicate hardware not ready
                print(f"WiFi hardware ready after {time.time() - start_time:.1f} seconds")
                return True
        except:
            pass
        blink()
        time.sleep(0.5)
    
    print(f"WiFi hardware initialization timeout after {max_wait} seconds")
    return False

def connect_wifi_robust():
    """Connect to WiFi network with multiple retries and better error handling"""
    print(f"\n{'='*60}")
    print("Attempting WiFi Connection")
    print(f"{'='*60}")
    
    # Ensure WiFi hardware is ready first
    if not wait_for_wifi_ready():
        print("ERROR: WiFi hardware not ready")
        return False
    
    # Try multiple times if needed
    for attempt in range(WIFI_CONNECT_RETRIES):
        if wlan.isconnected():
            print(f"Already connected to WiFi!")
            print(f"IP address: {wlan.ifconfig()[0]}")
            return True
            
        print(f"\nConnection attempt {attempt + 1}/{WIFI_CONNECT_RETRIES}")
        print(f"Connecting to: {WIFI_SSID}")
        
        try:
            # Disconnect first if partially connected
            try:
                current_status = wlan.status()
                # Only check for constants we know exist
                if hasattr(network, 'STAT_IDLE'):
                    if current_status != network.STAT_IDLE:
                        wlan.disconnect()
                        time.sleep(0.5)
                else:
                    # If STAT_IDLE doesn't exist, just try to disconnect
                    wlan.disconnect()
                    time.sleep(0.5)
            except:
                # If status check fails, just continue
                pass
            
            wlan.connect(WIFI_SSID, WIFI_PASSWORD)
            
            # Wait for connection with timeout
            timeout = WIFI_CONNECT_TIMEOUT
            while not wlan.isconnected() and timeout > 0:
                blink()
                timeout -= 1
                
                # Check status - only check constants that exist
                try:
                    status = wlan.status()
                    
                    # Check for wrong password (if constant exists)
                    if hasattr(network, 'STAT_WRONG_PASSWORD'):
                        if status == network.STAT_WRONG_PASSWORD:
                            print("ERROR: Wrong password!")
                            return False
                    
                    # Check for no AP found (if constant exists)
                    if hasattr(network, 'STAT_NO_AP_FOUND'):
                        if status == network.STAT_NO_AP_FOUND:
                            print("ERROR: Network not found!")
                            break
                    
                    # Don't check STAT_CONNECT_FAIL as it doesn't exist on ESP32C6
                    
                except:
                    # If status check fails, just continue waiting
                    pass
            
            if wlan.isconnected():
                print(f"\n{'='*60}")
                print("WiFi Connected Successfully!")
                print(f"IP address: {wlan.ifconfig()[0]}")
                try:
                    rssi = wlan.status('rssi')
                    print(f"Signal strength: {rssi} dBm")
                except:
                    pass  # RSSI might not be available
                print(f"{'='*60}\n")
                return True
            else:
                try:
                    status = wlan.status()
                    print(f"Attempt {attempt + 1} failed (status code: {status})")
                except:
                    print(f"Attempt {attempt + 1} failed")
                
        except Exception as e:
            print(f"Connection error on attempt {attempt + 1}: {e}")
            import sys
            sys.print_exception(e)
        
        # Wait before retry (except on last attempt)
        if attempt < WIFI_CONNECT_RETRIES - 1:
            print("Waiting 3 seconds before retry...")
            for _ in range(6):
                blink()
    
    print(f"\n{'='*60}")
    print("WiFi connection failed after all retries")
    print(f"{'='*60}\n")
    return False

def disconnect_wifi():
    """Disconnect and disable WiFi to save power"""
    if wlan.isconnected():
        wlan.disconnect()
    wlan.active(False)
    print("WiFi disabled - power saving mode")

# ============ UART COMMUNICATION ============

def send_command(cmd, timeout=2.0, verbose=True):
    """
    Send command to RP2040 via UART with improved error handling and debugging
    
    Args:
        cmd: Command string to send
        timeout: Response timeout in seconds
        verbose: Print debug information
    
    Returns:
        dict with 'success' (bool), 'response' (list of lines), 'error' (str or None)
    """
    result = {
        'success': False,
        'response': [],
        'error': None
    }
    
    # Prepare command
    cmd_clean = cmd.strip()
    if not cmd_clean:
        result['error'] = 'Empty command'
        return result
    
    cmd_bytes = (cmd_clean + '\n').encode('utf-8')
    
    if verbose:
        print(f'[UART TX] {repr(cmd_bytes)}')
    
    try:
        # CRITICAL: Clear UART buffer BEFORE sending command to avoid contamination from previous responses
        while uart.any():
            uart.read()
        
        # Send command
        bytes_written = uart.write(cmd_bytes)
        if bytes_written != len(cmd_bytes):
            result['error'] = f'Write incomplete: {bytes_written}/{len(cmd_bytes)} bytes'
            return result
        
        # Brief pause for UART transmission to complete (don't wait so long that we miss the response!)
        time.sleep(0.05)
        
        # Read response with timeout
        start_time = time.time()
        response_lines = []
        found_completion = False
        
        while time.time() - start_time < timeout:
            if uart.any():
                try:
                    line = uart.readline()
                    if line:
                        # Decode and clean
                        try:
                            decoded = line.decode('utf-8').strip()
                        except UnicodeDecodeError as e:
                            if verbose:
                                print(f'[UART ERROR] Decode failed: {e}')
                            result['error'] = f'Decode error: {e}'
                            return result
                        
                        if verbose:
                            print(f'[UART RX] {repr(decoded)}')
                        
                        response_lines.append(decoded)
                        
                        # Check for completion markers - ONLY <<OK or <<ERROR, not <<CMD
                        if decoded.startswith('<<OK') or decoded.startswith('<<ERROR'):
                            found_completion = True
                            break
                            
                except Exception as e:
                    if verbose:
                        print(f'[UART ERROR] Read failed: {e}')
                    result['error'] = f'Read error: {e}'
                    return result
            else:
                # No data available, small sleep to avoid busy-waiting
                time.sleep(0.01)
        
        # Check results
        if not response_lines:
            result['error'] = 'No response received'
            return result
        
        # Filter out DataLogger CSV lines (continuous status updates)
        # Keep only command response lines (those starting with << or >>)
        command_response_lines = [
            line for line in response_lines 
            if line.startswith('<<') or line.startswith('>>')
        ]
        
        if not found_completion:
            if verbose:
                print(f'[UART WARNING] Timeout after {timeout}s without completion marker')
            result['error'] = 'Response incomplete (timeout)'
        else:
            result['success'] = True
        
        # Use filtered response lines (command acknowledgments only)
        result['response'] = command_response_lines if command_response_lines else response_lines
        
        # Check for error responses
        for line in command_response_lines:
            if line.startswith('<<ERROR'):
                result['error'] = line
                result['success'] = False
                break
        
        return result
        
    except Exception as e:
        result['error'] = f'UART exception: {e}'
        if verbose:
            print(f'[UART EXCEPTION] {e}')
            import sys
            sys.print_exception(e)
        return result


def parse_status(response_dict):
    """
    Parse status response from controller
    
    Args:
        response_dict: dict returned by send_command
    
    Returns:
        dict with temperature, setpoint, duty_cycle, state, or None if parsing fails
    """
    if not response_dict.get('response'):
        return None
    
    # Get the last line (most recent reading)
    response_lines = response_dict['response']
    
    # Try old format first: STATUS,temp,setpoint,duty,state
    for line in response_lines:
        if line.startswith('>>STATUS') or line.startswith('STATUS'):
            try:
                status_line = line.replace('>>', '')
                parts = status_line.split(',')
                if len(parts) >= 5:
                    return {
                        'temperature': float(parts[1]),
                        'setpoint': float(parts[2]),
                        'duty_cycle': int(parts[3]),
                        'state': parts[4],
                        'raw': line
                    }
            except (ValueError, IndexError) as e:
                print(f'[PARSE ERROR] Status line: {e}')
    
    # Try new format: temp,duty,setpoint (3 values, no prefix)
    # Use the last line as it's the most recent
    if response_lines:
        line = response_lines[-1]
        try:
            parts = line.split(',')
            if len(parts) >= 3:
                # Assuming format is: temp,duty,setpoint
                return {
                    'temperature': float(parts[0]),
                    'duty_cycle': int(parts[1]),
                    'setpoint': float(parts[2]),
                    'state': 'unknown',
                    'raw': line
                }
        except (ValueError, IndexError) as e:
            print(f'[PARSE ERROR] CSV line: {e}')
    
    return None


def parse_temp(response_dict):
    """
    Parse temperature response from controller
    
    Args:
        response_dict: dict returned by send_command
    
    Returns:
        float temperature value, or None if parsing fails
    """
    if not response_dict.get('response'):
        return None
    
    # Look for TEMP line specifically
    for line in response_dict['response']:
        if line.startswith('TEMP,'):
            try:
                parts = line.split(',')
                if len(parts) >= 2:
                    return float(parts[1])
            except (ValueError, IndexError) as e:
                print(f'[PARSE ERROR] Temp line: {e}')
                return None
    
    return None


# ============ WEB SERVER ============
HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>&#9749; Coffee Controller</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            color: #333;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }
        .header h1 {
            font-size: 2em;
            margin-bottom: 10px;
        }
        .content {
            padding: 30px;
        }
        .status-card {
            background: #f8f9fa;
            border-radius: 15px;
            padding: 25px;
            margin-bottom: 25px;
        }
        .status-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid #e9ecef;
        }
        .status-row:last-child {
            border-bottom: none;
        }
        .status-label {
            font-weight: 600;
            color: #666;
        }
        .status-value {
            font-size: 1.3em;
            font-weight: 700;
            color: #667eea;
        }
        .temp-big {
            font-size: 3em !important;
            color: #764ba2;
        }
        .button-group {
            margin-bottom: 20px;
        }
        .button-group h3 {
            margin-bottom: 15px;
            color: #666;
            font-size: 1em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .buttons {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
        }
        button {
            padding: 15px 25px;
            font-size: 16px;
            font-weight: 600;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }
        .btn-secondary {
            background: #6c757d;
            color: white;
        }
        .btn-danger {
            background: #dc3545;
            color: white;
        }
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
        }
        button:active {
            transform: translateY(0);
        }
        .temp-controls {
            display: flex;
            gap: 10px;
            margin-top: 10px;
        }
        input[type="number"] {
            flex: 1;
            padding: 12px;
            border: 2px solid #e9ecef;
            border-radius: 8px;
            font-size: 16px;
        }
        .footer {
            text-align: center;
            padding: 20px;
            color: #999;
            font-size: 0.9em;
            border-top: 1px solid #e9ecef;
        }
        .advice {
            color: #999;
            font-size: 0.9em;
            text-transform: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>&#9749; Coffee Controller</h1>
        </div>
        
        <div class="content">
            <div class="status-card">
                <div class="status-row">
                    <span class="status-label">Current Temperature</span>
                    <span class="status-value temp-big" id="temp">--</span>
                </div>
                <div class="status-row">
                    <span class="status-label">Setpoint</span>
                    <span class="status-value" id="setpoint">--</span>
                </div>
                <div class="status-row">
                    <span class="status-label">Duty Cycle</span>
                    <span class="status-value" id="duty">--</span>
                </div>
                <div class="status-row">
                    <span class="status-label">State</span>
                    <span class="status-value" id="state">--</span>
                </div>
            </div>
            
            <div class="button-group">
                <h3>Coffee Mode <span class="advice">(too sour? try hotter | too bitter? try cooler)</span> </h3>
                <div class="temp-controls">
                    <input type="number" id="coffeeTemp" value="108" min="80" max="120" step="0.5" onblur="enforceMinMax(this)" placeholder="Temp (80-120&#176;C)">
                    <button class="btn-primary" onclick="sendCmd('reg coffee ' + document.getElementById('coffeeTemp').value)">Set Coffee</button>
                </div>
            </div>

            
            <div class="button-group">
                <h3>Steam Mode</h3>
                <div class="temp-controls">
                    <input type="number" id="steamTemp" value="145" min="120" max="160" step="0.5" onblur="enforceMinMax(this)" placeholder="Temp (120-160&#176;C)">
                    <button class="btn-primary" onclick="sendCmd('reg steam ' + document.getElementById('steamTemp').value)">Set Steam</button>
                </div>
            </div>
            
            <div class="button-group">
                <h3>Regulation Control</h3>
                <div class="buttons">
                    <button class="btn-primary" onclick="sendCmd('reg on')">Reg ON</button>
                    <button class="btn-danger" onclick="sendCmd('reg off')">Reg OFF</button>
                </div>
            </div>
            
            <div class="button-group">
                <h3>Heater Control</h3>
                <div class="buttons">
                    <button class="btn-secondary" onclick="sendCmd('heater on')">Heater ON</button>
                    <button class="btn-secondary" onclick="sendCmd('heater off')">Heater OFF</button>
                </div>
            </div>
        </div>
        
        <div class="footer">
            ESP32-C6 Coffee Controller v0.9.4
        </div>
    </div>
    
    <script>
        function updateStatus() {
            fetch('/status')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('temp').textContent = 
                        data.temperature ? data.temperature.toFixed(1) + '°C' : '--';
                    document.getElementById('setpoint').textContent = 
                        data.setpoint ? data.setpoint.toFixed(1) + '°C' : '--';
                    document.getElementById('duty').textContent = 
                        data.duty_cycle ? data.duty_cycle + '%' : '--';
                    document.getElementById('state').textContent = 
                        data.state || 'unknown';
                })
                .catch(e => console.error('Status update failed:', e));
        }
        
        function sendCmd(cmd) {
            fetch('/cmd', {
                method: 'POST',
                body: cmd
            })
            .then(r => r.json())
            .then(data => {
                console.log('Command response:', data);
                setTimeout(updateStatus, 500);
            })
            .catch(e => console.error('Command failed:', e));
        }
        
        function enforceMinMax(element) {
          // Check if the input value is not empty
          if (element.value !== "") {
            let min = parseFloat(element.min);
            let max = parseFloat(element.max);
            let value = parseFloat(element.value);
            
            // Check if the value is a number and is out of range
            if (isNaN(value) || value < min) {
              element.value = min; // Set to the minimum value
            } else if (value > max) {
              element.value = max; // Set to the maximum value
            }
          }
        }        
        
        updateStatus();
        setInterval(updateStatus, 2000);        
        
    </script>
</body>
</html>
"""

def web_server_scheduled():
    """Run web server during scheduled WiFi hours"""
    # CRITICAL: Create socket with proper settings
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(10.0)  # Server socket timeout
    led_timer = Timer(0)

    try:
        s.bind(('0.0.0.0', 80))
        s.listen(1)  # Only need 1 connection in backlog
        print(f"\n{'='*60}")
        print(f"Web server (with pulse) running on http://{wlan.ifconfig()[0]}")
        print(f"Will shut down automatically at {WIFI_END_HOUR:02d}:00")
        print(f"{'='*60}\n")
        led_timer.init(freq=1, mode=Timer.PERIODIC, callback=pulse)

        while True:
            cl = None
            try:
                # Accept connection with timeout
                try:
                    cl, addr = s.accept()
                    print(f'Connection from {addr}')
                    # Pulse while connection alive...seems to slow response from API
                    #pulse()
                except OSError as e:
                    # Check if we should still be running
                    if not is_wifi_hours():
                        print(f"WiFi hours ended at {get_local_time_str()} - shutting down server")
                        break
                    continue  # Timeout, try again
                
                # Client socket timeout
                cl.settimeout(10.0)
                
                # Read request
                try:
                    request = cl.recv(1024).decode('utf-8')
                except OSError as e:
                    print(f'Recv error: {e}')
                    continue
                
                lines = request.split('\r\n')
                
                if len(lines) > 0:
                    request_line = lines[0]
                    parts = request_line.split(' ')
                    
                    if len(parts) >= 2:
                        method = parts[0]
                        path = parts[1]
                        
                        if method == 'GET' and path == '/':
                            response = 'HTTP/1.1 200 OK\r\n'
                            response += 'Content-Type: text/html; charset=utf-8\r\n'
                            response += 'Connection: close\r\n\r\n'
                            response += HTML
                            
                            try:
                                response_bytes = response.encode()
                                chunk_size = 512
                                for i in range(0, len(response_bytes), chunk_size):
                                    chunk = response_bytes[i:i+chunk_size]
                                    cl.send(chunk)
                            except Exception as e:
                                print(f'Send error: {e}')
                            
                        elif method == 'GET' and path == '/status':
                            resp = send_command('status', verbose=False)
                            status = parse_status(resp)
                            
                            response = 'HTTP/1.1 200 OK\r\n'
                            response += 'Content-Type: application/json; charset=utf-8\r\n'
                            response += 'Access-Control-Allow-Origin: *\r\n'
                            response += 'Connection: close\r\n\r\n'
                            response += json.dumps(status if status else {
                                'temperature': None,
                                'setpoint': 0,
                                'duty_cycle': 0,
                                'state': 'unknown'
                            })
                            
                            try:
                                cl.sendall(response.encode())
                            except Exception as e:
                                print(f'Send error: {e}')
                            
                        elif method == 'POST' and path == '/cmd':
                            body_start = request.find('\r\n\r\n')
                            if body_start != -1:
                                command = request[body_start+4:].strip()
                                print(f"Command received: {command}")
                                resp = send_command(command, verbose=False)
                                
                                response = 'HTTP/1.1 200 OK\r\n'
                                response += 'Content-Type: application/json; charset=utf-8\r\n'
                                response += 'Access-Control-Allow-Origin: *\r\n'
                                response += 'Connection: close\r\n\r\n'
                                response += json.dumps({
                                    'success': resp.get('success', False),
                                    'response': resp.get('response', [])
                                })
                                
                                try:
                                    cl.sendall(response.encode())
                                except Exception as e:
                                    print(f'Send error: {e}')
                
                time.sleep(0.02)
                
            except Exception as e:
                print(f'Error: {e}')
                import sys
                sys.print_exception(e)
                time.sleep(0.2)
                
            finally:
                # Always close client connection
                if cl:
                    try:
                        cl.close()
                    except:
                        pass
                    cl = None
                
                # Check if we should still be running
                if not is_wifi_hours():
                    print(f"WiFi hours ended - shutting down server")
                    break
                    
    finally:
        # Cleanup
        print("Cleaning up web server...")
        led_timer.deinit()
        try:
            s.close()
        except:
            pass
        led.off()

# ============ MAIN LOOP ============
def main():
    """Main entry point with time-based WiFi control - STANDALONE FIXED"""
    
    # CRITICAL FIX #1: Wait for hardware to stabilize after boot
    print("\n" + "="*60)
    print("BOOT SEQUENCE STARTING")
    print("="*60)
    print(f"Waiting {BOOT_DELAY} seconds for hardware stabilization...")
    led.on()
    time.sleep(BOOT_DELAY)
    led.off()
    print("Hardware ready!")
    
    print("\n" + "="*60)
    print("Coffee Controller with Scheduled WiFi v0.9.4 (ESP32C6 FIX)")
    print("="*60)
    print(f"WiFi Schedule: {WIFI_START_HOUR:02d}:00 - {WIFI_END_HOUR:02d}:00 (local time)")
    print(f"Base Timezone: UTC{BASE_TIMEZONE_OFFSET:+d} (PST)")
    print("="*60 + "\n")
    
    # CRITICAL FIX #2: Robust initial time sync with retries
    time_synced = False
    print("Attempting initial time sync...")
    
    if connect_wifi_robust():
        if sync_time():
            time_synced = True
            utc_time = time.localtime()
            year, month, day = utc_time[0], utc_time[1], utc_time[2]
            offset = get_timezone_offset()
            dst_active = is_dst(year, month, day)
            print(f"\n{'='*60}")
            print("TIME SYNC SUCCESSFUL")
            print(f"{'='*60}")
            print(f"Date: {year}-{month:02d}-{day:02d}")
            print(f"DST Active: {dst_active} (offset: UTC{offset:+d})")
            print(f"Current local time: {get_local_time_str()}")
            print(f"Current hour: {get_local_hour()}")
            print(f"WiFi hours active: {is_wifi_hours()}")
            print(f"{'='*60}\n")
        else:
            print("\nWARNING: NTP sync failed after retries")
            print("Device will retry time sync periodically")
    else:
        print("\nWARNING: Initial WiFi connection failed")
        print("Device will retry connection periodically")
    
    # CRITICAL FIX #3: Check if we should start web server immediately
    if time_synced and is_wifi_hours():
        print(f"\n{'='*60}")
        print("CURRENTLY IN WIFI HOURS - STARTING WEB SERVER NOW")
        print(f"{'='*60}\n")
        try:
            web_server_scheduled()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"Web server error: {e}")
            import sys
            sys.print_exception(e)
        # After server exits, disconnect
        disconnect_wifi()
    elif time_synced:
        # Not in WiFi hours, disconnect to save power
        disconnect_wifi()
        print(f"Not in WiFi hours yet. Next check in 60 seconds...")
    
    # Main monitoring loop
    last_check = time.time()
    last_time_sync = time.time()
    check_interval = 60  # Check every 1 minute
    time_sync_interval = 3600  # Resync time every hour
    wifi_active = wlan.isconnected()
    
    while True:
        current_time = time.time()
        
        # Periodic time resync (if we have had a successful sync before)
        if time_synced and (current_time - last_time_sync >= time_sync_interval):
            print(f"\nPeriodic time resync (every hour)...")
            if not wifi_active:
                if connect_wifi_robust():
                    sync_time()
                    disconnect_wifi()
                    last_time_sync = current_time
            else:
                sync_time()
                last_time_sync = current_time
        
        # Check if we should toggle WiFi state
        if current_time - last_check >= check_interval:
            last_check = current_time
            
            # If we haven't synced time yet, try again
            if not time_synced:
                print("\nRetrying time sync...")
                if connect_wifi_robust():
                    if sync_time():
                        time_synced = True
                        print("Time sync successful on retry!")
                    disconnect_wifi()
            
            if time_synced:
                should_be_active = is_wifi_hours()
                
                hour = get_local_hour()
                time_str = get_local_time_str()
                print(f"\n[{time_str}] Time check - WiFi should be: {'ON' if should_be_active else 'OFF'}")
                
                if should_be_active and not wifi_active:
                    # Time to turn WiFi ON
                    print(f"\nEntering WiFi hours ({WIFI_START_HOUR:02d}:00-{WIFI_END_HOUR:02d}:00)")
                    if connect_wifi_robust():
                        wifi_active = True
                        print("Starting web server...")
                        try:
                            web_server_scheduled()
                        except KeyboardInterrupt:
                            break
                        except Exception as e:
                            print(f"Web server error: {e}")
                            import sys
                            sys.print_exception(e)
                        # Server shut down, disconnect
                        disconnect_wifi()
                        wifi_active = False
                            
                elif not should_be_active and wifi_active:
                    # Time to turn WiFi OFF
                    print(f"Exiting WiFi hours - disabling WiFi")
                    disconnect_wifi()
                    wifi_active = False
        
        # During OFF hours, heartbeat and sleep
        if not wifi_active:
            beat()

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nShutting down...")
        disconnect_wifi()
        led.off()
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        import sys
        sys.print_exception(e)
        # Blink rapidly to indicate error
        for _ in range(20):
            led.on()
            time.sleep(0.1)
            led.off()
            time.sleep(0.1)
