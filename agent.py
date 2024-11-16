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

def service_active(service_name):
    """
    Check if a systemd service is active.

    Parameters:
    service_name (str): The name of the service to check.

    Returns:
    bool: True if the service is active, False otherwise.
    """
    try:
        # Run the systemctl command to check the status of the service
        result = subprocess.run(
            ['systemctl', '--user', 'is-active', service_name],
            capture_output=True,
            text=True,
            check=True
        )

        # Check if the output of the command is 'active'
        if result.stdout.strip() == 'active':
            return True
        else:
            return False
    except subprocess.CalledProcessError:
        # If the command fails, it means the service is not active
        return False

def start_service(service_name):
    """
    Start a systemd service.

    Parameters:
    service_name (str): The name of the service to start.

    Returns:
    bool: True if the service was started successfully, False otherwise.
    """
    try:
        # Run the systemctl command to start the service
        subprocess.run(['systemctl', '--user', 'start', service_name], check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to start service {service_name}: {e}")
        return False

def stop_service(service_name):
    """
    Stop a systemd service.

    Parameters:
    service_name (str): The name of the service to stop.

    Returns:
    bool: True if the service was stopped successfully, False otherwise.
    """
    try:
        # Run the systemctl command to stop the service
        subprocess.run(['systemctl', '--user', 'stop', service_name], check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to stop service {service_name}: {e}")
        return False

def run_command(command):
    """
    Run a command and return the output as a string.

    Parameters:
    command (str): The command to run.

    Returns:
    str: The output of the command, or an empty string if it failed.
     """
    try:
         # Run the command
        result = subprocess.run(command, capture_output=True)
        return result
    except subprocess.CalledProcessError:
        return False
        

# Define API key check function
def key_check(func):
    def wrapper(*args, **kwargs):
        if 'X-API-KEY' not in request.headers or request.headers['X-API-KEY'] != API_KEY:
            return jsonify({'err': 'Invalid API Key'}), 401
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

# Define commands to turn on/off monitor
off_command = ["xset", "-display", ":0", "dpms", "force", "off"]
on_command = ["xset", "-display", ":0", "dpms", "force", "on"]
status = ["xset", "-display", ":0", "q"]

# Function to check if monitor is on
def is_on():
    run = subprocess.run(status, stdout=subprocess.PIPE)
    return "Monitor is On" in str(run.stdout)

# Create Flask app
app = Flask(__name__)

@app.route('/', methods=['GET'])
@key_check
def index():
    return jsonify({"message": "Hello, world. This is just a dumb api, nothing to see here"})

# Function to get or set monitor status
@app.route('/monitor', methods=['POST', 'GET'])
@key_check
def monitor_status():
    if request.method == 'POST':
        body = request.json  # Convert JSON POST data to Python object
        do_action = body
        if do_action.get("on") and not is_on():
            run = subprocess.run(on_command)
            start_ui = start_service('frameit-ui')
            if run.returncode == 0 and start_ui:
                return jsonify({'success': True, "exit_code": run.returncode})
            else:
                return jsonify({'success': False, "start_ui" : start_ui, "exit_code" : run.returncode}), 500
        elif do_action.get("off") and is_on():
            run = subprocess.run(off_command)
            stop_ui = stop_service('frameit-ui')
            if run.returncode == 0 and stop_ui:
                return jsonify({'success': True, "exit_code": run.returncode})
            else:
                return jsonify({'success': False, "stop_ui" : stop_ui, "exit_code": run.returncode}), 500
        else:
            return jsonify({"no_action": True})
    
    # Return monitor status on GET request
    return jsonify({"status": str(is_on()), "ui_service" : service_active('frameit-ui')})

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0")
