`tuning.py` is from https://github.com/respeaker/usb_4_mic_array (Seeed's control implementation).
Local compatibility changes: array.tostring() -> bytes(array), USB timeout reduced to 1 second.
See LICENSE for the upstream license.

`odas.cfg` is the ReSpeaker configuration from introlab/odas commit bcb845434495e293df3d48f1203b7a86e1852449. See ODAS-LICENSE. Runtime configuration preserves its microphone geometry and selects the host raw-channel order.
