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
    
    if service_name == 'frameit-agent':
        api_key = input("Enter your FrameIT Agent API key (make it random): ")
    else:
        api_key = None
    
    if service_name == 'frameit-ui':
        server_url = input("Enter the URL for the server (including http(s) and port): ")
    else:
        server_url = None
    
    # Render the service file with Jinja2
    template = Template(open(service_templates[service_name]).read())
    rendered_service_file = template.render(
        install_loc=location,
        server_url=server_url,
        api_key=api_key
    )

    # Create the installation directory if it does not exist
    service_dir = os.path.join(f"/home/{getuser()}/.config/systemd/user/")
    os.makedirs(service_dir, exist_ok=True)

    # Save the rendered service file to the installation location
    with open(os.path.join(service_dir, f'{service_name}.service'), 'w') as f:
        f.write(rendered_service_file)

def main():
    print("Systemd Service Installer")
    
    location = os.getcwd()
    print(f"Pointing Services to {location}...")
        
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