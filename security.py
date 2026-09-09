"""Shared local transport validation and public channel metadata."""
import hashlib
import ipaddress
import re


def loopback_host(value):
    host = str(value).strip()
    if host == 'localhost':
        return '127.0.0.1'
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise ValueError('proxy.host must be a loopback IP or localhost')


def fingerprint(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:12].upper()


def public_payload(value):
    if isinstance(value, list):
        return [public_payload(item) for item in value]
    if isinstance(value, dict):
        out = {}
        secret = value.get('full_url') or value.get('previous_full_url') or value.get('active_room_url') or value.get('hash')
        if secret:
            out['fingerprint'] = fingerprint(secret)
        for key, item in value.items():
            lowered = key.lower()
            if any(part in lowered for part in ('hash', 'full_url', 'primary_url', 'room_url', 'psk', 'passkey', 'private_key')) and lowered != 'has_psk':
                continue
            out[key] = public_payload(item)
        return out
    if isinstance(value, str):
        return re.sub(r'https?://[^\s"<>]*#[^\s"<>]*', '[channel URL hidden]', value)
    return value
