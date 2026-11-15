"""Passwörter für den Client im Schlüsselbund des Systems speichern.

macOS:   Schlüsselbundverwaltung
Windows: Anmeldeinformationsverwaltung
Ohne passendes Backend (z. B. Linux ohne Secret Service) ist die Funktion aus."""

import sys

SERVICE = "HopGrid"


def _backend():
    try:
        import keyring
        # Backend fest wählen: In der gebauten App findet keyring seine
        # Backends sonst nicht zuverlässig (fehlende Paket-Metadaten).
        if sys.platform == "darwin":
            from keyring.backends import macOS
            keyring.set_keyring(macOS.Keyring())
        elif sys.platform == "win32":
            from keyring.backends import Windows
            keyring.set_keyring(Windows.WinVaultKeyring())
        return keyring
    except Exception:
        return None


_keyring = _backend()


def available():
    return _keyring is not None


def _account(host, user):
    return f"{user}@{host.strip().casefold()}"


def load(host, user):
    if not _keyring or not host or not user:
        return None
    try:
        return _keyring.get_password(SERVICE, _account(host, user))
    except Exception:
        return None


def store(host, user, password):
    if not _keyring:
        return False
    try:
        _keyring.set_password(SERVICE, _account(host, user), password)
        return True
    except Exception:
        return False


def forget(host, user):
    if not _keyring:
        return
    try:
        _keyring.delete_password(SERVICE, _account(host, user))
    except Exception:
        pass
