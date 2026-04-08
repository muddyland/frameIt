import os
from jinja2 import Template
from getpass import getuser

# frameit-agent is now a separate system-level service installed by install.sh
service_templates = {
    'frameit-ui': './systemd/frameit-ui.service.j2',
    'frameit-server': './systemd/frameit-server.service.j2',
}


def install_service(location, service_name):
    server_url = None
    if service_name == 'frameit-ui':
        server_url = input("Enter the FrameIT server URL (e.g. http://localhost:5000): ").strip()

    template = Template(open(service_templates[service_name]).read())
    rendered = template.render(install_loc=location, server_url=server_url)

    service_dir = os.path.join(f"/home/{getuser()}/.config/systemd/user/")
    os.makedirs(service_dir, exist_ok=True)

    dest = os.path.join(service_dir, f'{service_name}.service')
    with open(dest, 'w') as f:
        f.write(rendered)
    print(f"    Written to {dest}")


def main():
    print("FrameIT Systemd Service Installer")
    location = os.getcwd()
    print(f"Install location: {location}")

    for service_name in service_templates:
        choice = input(f"Install {service_name}? (y/n): ").strip().lower()
        if choice == 'y':
            print(f"Installing {service_name}...")
            install_service(location, service_name)
        else:
            print(f"Skipping {service_name}")

    print("\nDone. Run: systemctl --user daemon-reload && systemctl --user enable --now frameit-server frameit-ui")
    print("The frameit-agent is installed separately via the web UI token flow.")


if __name__ == "__main__":
    main()
