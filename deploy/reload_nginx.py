#!/usr/bin/python3
import subprocess
subprocess.run(['/usr/sbin/nginx','-t'],check=True)
subprocess.run(['/usr/bin/systemctl','reload','nginx'],check=True)
