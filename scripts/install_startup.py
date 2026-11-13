#!/usr/bin/env python3
"""Install or remove desktop autostart and the supervised face-agent user service."""
import argparse
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))


def quoted(path):
    return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remove', action='store_true', help='Stop the stack and remove autostart; keep settings/key.')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run this as your desktop user, without sudo.')
    unit = CONFIG / 'systemd/user/face-agent.service'
    desktop = CONFIG / 'autostart/face-agent.desktop'
    if args.remove:
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'face-agent.service'], check=True)
        unit.unlink(missing_ok=True)
        desktop.unlink(missing_ok=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        print('Removed face-agent autostart. Settings and API key were kept.')
        return
    unit.parent.mkdir(parents=True, exist_ok=True)
    desktop.parent.mkdir(parents=True, exist_ok=True)
    settings = CONFIG / 'face-agent/startup.env'
    settings.parent.mkdir(parents=True, exist_ok=True)
    if not settings.exists():
        settings.write_text('# Loaded by start-stack.sh (shell variable assignments).\n'
                            'DOTS_RENDERER=prerendered\nDOTS_FULLSCREEN=1\n'
                            'DOTS_CAMERA_PROFILE=rgbd\nDOTS_INITIAL_STATUS=SLEEPING\n'
                            '# PORT=5173\n# ROS_DOMAIN_ID=0\n')
    # An interactive shell's key is not inherited at the next login. Use the
    # application's existing private key file, never a unit or desktop entry.
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    key_file = CONFIG / 'mimo-dots/openai-api-key'
    if key and not key_file.exists():
        key_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(key_file, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            output.write(key + '\n')
        print('Saved the current API key in the private application key file for startup.')
    unit.write_text('[Unit]\nDescription=Kuro face, voice agent and ROS camera\n'
                    'After=graphical-session-pre.target\nPartOf=graphical-session.target\n'
                    'StartLimitIntervalSec=0\n\n[Service]\nType=simple\n'
                    f'WorkingDirectory={str(ROOT).replace("%", "%%")}\nExecStart={quoted(ROOT / "start-stack.sh")}\n'
                    'Restart=always\nRestartSec=10\nKillMode=control-group\nTimeoutStopSec=90\n'
                    '\n[Install]\nWantedBy=graphical-session.target\n')
    desktop.write_text('[Desktop Entry]\nType=Application\nName=Kuro robot stack\n'
                       'Comment=Start the fullscreen face, voice agent and ROS camera\n'
                       f'Exec={quoted(ROOT / "scripts/desktop_start.sh")}\n'
                       'Terminal=false\nX-GNOME-Autostart-enabled=true\n')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', 'face-agent.service'], check=True)
    print(f'Installed desktop autostart. Settings: {settings}')
    print('Start now: scripts/desktop_start.sh')
    print('Stop: systemctl --user stop face-agent.service')
    print('For unattended boot, enable automatic desktop login (see README).')


if __name__ == '__main__':
    main()
