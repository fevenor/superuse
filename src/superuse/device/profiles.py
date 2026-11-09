from __future__ import annotations

BAUD_RATES = (
    9600,
    14400,
    19200,
    28800,
    38400,
    56000,
    57600,
    76800,
    115200,
    128000,
    153600,
    230400,
    256000,
    460800,
    500000,
    921600,
    1000000,
    2000000,
)


def capabilities(device_type: str) -> dict:
    extended = device_type == "superkvm"
    return {
        "device_type": device_type,
        "device_info": extended,
        "mouse_mode": extended,
        "video_power": extended,
        "usb_identity": False,
        "device_logs": extended,
        "restart": extended,
    }
