from flask import Flask, jsonify, request
import subprocess
import json
import psutil
import os
import subprocess

# Define API key
API_KEY = ''
if not API_KEY:
    print('API key not set!')
    exit()        

# Define API key check function
def key_check(func):
    def wrapper(*args, **kwargs):
        if 'X-API-KEY' not in request.headers or request.headers['X-API-KEY'] != API_KEY:
            return jsonify({'err': 'Invalid API Key'}), 401
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

# Function to check if monitor is on
def is_on():
    run = subprocess.run(["xset", "-display", ":0", "q"], stdout=subprocess.PIPE)
    if "DPMS is Disabled" in str(run.stdout) or "Monior is On" in str(run.stdout):
      return True
    else:
      return False

# Create Flask app
app = Flask(__name__)

@app.route('/', methods=['GET'])
@key_check
def index():
    return jsonify({"message": "Hello, world. This is just a dumb api, nothing to see here"})

@app.route("/status/ui", methods=['GET'])
@key_check
def ui_status():
    run = subprocess.run(["xset", "-display", ":0", "q"])
    return jsonify({"status": "on" if is_on() else "off"})

# Function to get or set monitor status
@app.route('/monitor', methods=['POST', 'GET'])
@key_check
def monitor_status():
    if request.method == 'POST':
        body = request.json  # Convert JSON POST data to Python object
        do_action = body
        if do_action.get("on") and not is_on():
            # Force the monitor on
            run = subprocess.run(["xset", "-display", ":0", "dpms", "force", "on"])
            print(f"Turning Monitor On: {run.returncode}")
            if run.stderr:
                print(f"Error setting monitor on: {run.stderr}")
                return jsonify({"error": f"Error setting monitor on: {run.stderr}"}), 500
            # When using dpms to force, we need to then turn the dpms back off
            run = subprocess.run(["xset", "-display", ":0", "-dpms"])
            print(f"Turning off Power Saving: {run.returncode}")
            if run.stderr:
                print(f"Error setting power saving off: {run.stderr}")
                return jsonify({"error": f"Error setting power saving off: {run.stderr}"}), 500
            # Resart UI
            run = subprocess.run(["systemctl", "--user", "restart", "frameit-ui"])
            print(f"Restarting frameit-ui: {run.returncode}")
            if run.stderr:
                print(f"Error restarting frameit-ui: {run.stderr}")
                return jsonify({"error": f"Error restarting frameit-ui: {run.stderr}"}), 500
            return jsonify({'success': True, "exit_code": run.returncode})
        elif do_action.get("off") and is_on():
            # Turn on power saving (this is not needed, doing in case monitor does not turn off on it's own)
            run = subprocess.run(["xset", "-display", ":0", "+dpms"])
            print(f"Turning on Power Saving: {run.returncode}")
            if run.stderr:
                print(f"Error setting power saving on: {run.stderr}")
                return jsonify({"error": f"Error setting power saving off: {run.stderr}"}), 500
            # Force the display off
            run = subprocess.run(["xset", "-display", ":0", "dpms", "force", "off"])
            print(f"Turning Monitor Off: {run.returncode}")
            if run.stderr:
                print(f"Error setting monitor off: {run.stderr}")
                return jsonify({"error": f"Error setting monitor off: {run.stderr}"}), 500
            # Stop Browser 
            run = subprocess.run(["systemctl", "--user", "stop", "frameit-ui"])
            print(f"Stopping frameit-ui: {run.returncode}")
            if run.stderr:
                print(f"Error stopping FrameIt UI: {run.stderr}")
                return jsonify({"error": f"Error stopping frameit-ui: {run.stderr}"}), 500
 
            return jsonify({'success': True, "exit_code": run.returncode})
        else:
            return jsonify({"no_action": True})
    # Return monitor status on GET request
    return jsonify({"status": str(is_on())})

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0")
