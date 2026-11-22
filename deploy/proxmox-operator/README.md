# Private operator relay

The devbox can retain its historical loopback operator address after the API moves to the control guest. A systemd user unit forwards a loopback port through SSH to the guest's own `127.0.0.1:8300`. The control guest uses its existing private SSH listener. Its dedicated account allows only that destination and rejects shell sessions, remote forwards, PTYs, agent forwarding and Unix socket forwarding.

1. Obtain the guest's Ed25519 host public key over Proxmox management. On the devbox, pipe it to `bash install-client.sh CONTROL_LAN_IP`. The script creates an independent SSH key inside the devbox's encrypted vault and installs `reeldub-operator-forward@.service`. It leaves the instance stopped.
2. Use the management channel to pipe the generated `operator-forward/id_ed25519.pub` into `bash install-server.sh DEVBOX_LAN_IP` inside the control guest. The account's authorized key is bound to the devbox source address. The server installer validates the effective OpenSSH settings for that account.
3. Start a spare instance on the devbox with `systemctl --user start reeldub-operator-forward@18430.service`. After the control guest's operator API is running, check the expected operator response through `http://127.0.0.1:18430`.
4. At cutover, release port 8300 from the old local operator service, enable and start `reeldub-operator-forward@8300.service`, verify the operator response, and stop the spare relay. Preserve the old service configuration until the new route is proven. Roll back by stopping the port-8300 relay before starting the former service again.

The server host key is pinned. After a host-key change, verify the replacement through authenticated Proxmox management and repeat the client installation. An SSH listener on the spare port proves transport setup. The API response through it verifies the backend path.
