# systemd packaging status

Production units are intentionally deferred until the Sunshine backend exists.
The final packaging will use one worker instance per VM and one restricted Unix
D-Bus address per instance:

```text
qemu-vm@<id>.service
sunshine-qemu@<id>.service
/run/sunshine-qemu/<id>/dbus.address
/etc/sunshine-qemu/<id>/sunshine.conf
```

The development probe should not be installed as the final service because it
writes diagnostic PPM/MKV files rather than serving Moonlight clients.
