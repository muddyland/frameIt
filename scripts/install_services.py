import os
import sys
from jinja2 import Template
from getpass import getuser

# Define the service template file paths
service_templates = {
    'frameit-agent': './systemd/frameit-agent.service.j2',
    'frameit-ui': './systemd/frameit-ui.service.j2',
    'frameit-server': './systemd/frameit-server.service.j2'
}

def install_service(location, service_name):
    # Render the service file with Jinja2
    template = Template(open(service_templates[service_name]).read())
    rendered_service_file = template.render(
        install_loc=location,
        server_url=get_server_url(service_name)
    )

    # Create the installation directory if it does not exist
    service_dir = os.path.join(location, 'services')
    os.makedirs(service_dir, exist_ok=True)

    # Save the rendered service file to the installation location
    with open(os.path.join(service_dir, f'{service_name}.service'), 'w') as f:
        f.write(rendered_service_file)

def get_server_url(service_name):
    if service_name == 'frameit-server':
        return 'http://localhost:5000'
    else:
        return input("Enter the URL for the server: ")

def main():
    print("Systemd Service Installer")
    
    # Ask user for the install location
    while True:
        location = input(f"Enter the installation location ({getuser()}/.config/systemd/user/): ")
        
        if not os.path.exists(location):
            print(f"The directory '{location}' does not exist.")
        else:
            break
    
    services_to_install = {}
    
    # Ask user which services to install
    for service_name in service_templates.keys():
        while True:
            choice = input(f"Would you like to install {service_name}? (y/n): ")
            if choice.lower() == 'y':
                services_to_install[service_name] = True
                break
            elif choice.lower() == 'n':
                services_to_install[service_name] = False
                break
    
    # Install selected services
    for service_name, install in services_to_install.items():
        if install:
            print(f"Installing {service_name}...")
            install_service(location, service_name)
        else:
            print(f"Not installing {service_name}")
    
    print("Installation complete.")

if __name__ == "__main__":
    main()