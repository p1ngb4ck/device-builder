from __future__ import annotations

import argparse
import base64
import datetime
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

SETUP_DIR = Path.home() / ".config" / "esphome-packages"
REPOSITORY_FILE = "repository"
DEPLOY_KEY_FILE = "deploy_key"
SIGNING_KEY_FILE = "signing.sec"
CLONE_DIR = "clone"
SIGNING_KEY_ENV = "ESPHOME_PACKAGES_SIGNING_KEY"
FIRMWARE_ID = re.compile(r"component_as_lib_abi_id = (\d+)u")
PUBLISH_ERRORS = (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError)

FIELDS = (
    "Package",
    "Version",
    "Depends",
    "Architecture",
    "ESPHome-ABI",
    "ESPHome-Flash",
    "ESPHome-PSRAM",
    "ESPHome-IRAM",
    "Description",
)


def ar_members(data: bytes) -> dict[str, bytes]:
    if data[:8] != b"!<arch>\n":
        raise ValueError("not an ar archive")
    members = {}
    pos = 8
    while pos + 60 <= len(data):
        header = data[pos : pos + 60]
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        pos += 60
        members[name] = data[pos : pos + size]
        pos += size + (size % 2)
    return members


def control_fields(ipk: bytes) -> dict[str, str]:
    members = ar_members(ipk)
    text = None
    with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name.lstrip("./") == "control":
                text = tar.extractfile(member).read().decode("utf-8")
    if text is None:
        raise ValueError("no control file")
    fields: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and key is not None:
            fields[key] += "\n" + line
        elif ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            fields[key] = value.strip()
    return fields


def paragraph(fields: dict[str, str], filename: str, data: bytes) -> str:
    lines = [f"{name}: {fields[name]}" for name in FIELDS if name != "Description" and fields.get(name)]
    lines += [f"Filename: {filename}", f"Size: {len(data)}", f"SHA256sum: {hashlib.sha256(data).hexdigest()}"]
    if fields.get("Description"):
        lines.append(f"Description: {fields['Description']}")
    return "\n".join(lines) + "\n"


def usign_line(text: str) -> bytes:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return base64.b64decode(lines[-1], validate=True)


def secret_key(text: str) -> tuple[bytes, bytes]:
    raw = usign_line(text)
    if len(raw) != 104 or raw[:2] != b"Ed" or raw[2:4] != b"BK":
        raise ValueError("the signing key is not a usign / signify Ed25519 secret key")
    if struct.unpack(">I", raw[4:8])[0] != 0:
        raise ValueError("the signing key is encrypted; a key without passphrase is needed")
    keynum = raw[32:40]
    seckey = raw[40:104]
    if hashlib.sha512(seckey).digest()[:8] != raw[24:32]:
        raise ValueError("the signing key's checksum does not match")
    return keynum, seckey[:32]


def public_key_file(key_text: str) -> bytes:
    raw = usign_line(key_text)
    secret_key(key_text)
    keynum = raw[32:40]
    public = raw[72:104]
    line = base64.b64encode(b"Ed" + keynum + public).decode("ascii")
    return f"untrusted comment: esphome-packages public key {keynum.hex()}\n{line}\n".encode("ascii")


def sign(index: bytes, key_text: str) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    keynum, seed = secret_key(key_text)
    signature = Ed25519PrivateKey.from_private_bytes(seed).sign(hashlib.sha256(index).digest())
    line = base64.b64encode(b"Ed" + keynum + signature).decode("ascii")
    return f"untrusted comment: signed by key {keynum.hex()}\n{line}\n".encode("ascii")


def keygen(out: Path) -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    keynum = os.urandom(8)
    seckey = seed + public
    raw = b"Ed" + b"BK" + struct.pack(">I", 0) + bytes(16) + hashlib.sha512(seckey).digest()[:8] + keynum + seckey
    secret = out.with_suffix(".sec")
    pub = out.with_suffix(".pub")
    if secret.exists() or pub.exists():
        raise ValueError(f"{secret} or {pub} exists already")
    secret.write_text(
        f"untrusted comment: esphome-packages secret key {keynum.hex()}\n{base64.b64encode(raw).decode('ascii')}\n",
        encoding="ascii",
        newline="\n",
    )
    pub.write_text(
        f"untrusted comment: esphome-packages public key {keynum.hex()}\n"
        f"{base64.b64encode(b'Ed' + keynum + public).decode('ascii')}\n",
        encoding="ascii",
        newline="\n",
    )
    return pub


def publish(ipks: list[Path], repo: Path, board: str, device: str, version: str, key_text: str = "") -> str:
    if not ipks:
        raise ValueError("no .ipk files")
    found = []
    for ipk in sorted(ipks):
        data = ipk.read_bytes()
        found.append((ipk, data, control_fields(data)))
    abis = {fields.get("ESPHome-ABI", "") for _, _, fields in found}
    if len(abis) != 1 or not next(iter(abis)).isdigit():
        raise ValueError("the packages do not carry one firmware id (ESPHome-ABI)")
    firmware_id = f"{int(found[0][2]['ESPHome-ABI']):08x}"
    target = repo / firmware_id
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    paragraphs = []
    for ipk, data, fields in found:
        (target / ipk.name).write_bytes(data)
        paragraphs.append(paragraph(fields, ipk.name, data))
    index = "\n".join(paragraphs).encode("utf-8")
    (target / "Packages").write_bytes(index)
    with gzip.GzipFile(target / "Packages.gz", "wb", mtime=0) as out:
        out.write(index)
    if key_text.strip():
        (target / "Packages.sig").write_bytes(sign(index, key_text))
        (repo / "signing.pub").write_bytes(public_key_file(key_text))
    catalog_path = repo / "index.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.is_file() else {"builds": []}
    builds = [build for build in catalog.get("builds", []) if build.get("id") != firmware_id]
    builds.append(
        {
            "id": firmware_id,
            "board": board,
            "device": device,
            "version": version,
            "date": datetime.date.today().isoformat(),
            "architecture": found[0][2].get("Architecture", ""),
            "packages": [fields["Package"] for _, _, fields in found],
        }
    )
    catalog["builds"] = builds
    catalog_path.write_text(json.dumps(catalog, indent=1) + "\n", encoding="utf-8", newline="\n")
    return firmware_id


def build_packages(build_path: Path) -> list[Path]:
    main = build_path / "src" / "main.cpp"
    folder = build_path / "build" / "packages"
    if not main.is_file() or not folder.is_dir():
        return []
    match = FIRMWARE_ID.search(main.read_text(encoding="utf-8", errors="replace"))
    if match is None:
        return []
    wanted = match.group(1)
    return [ipk for ipk in sorted(folder.glob("*.ipk")) if control_fields(ipk.read_bytes()).get("ESPHome-ABI") == wanted]


def enabled() -> bool:
    return (SETUP_DIR / REPOSITORY_FILE).is_file() and (SETUP_DIR / DEPLOY_KEY_FILE).is_file()


def _git(*args: str, cwd: Path | None = None) -> None:
    env = {
        **os.environ,
        "GIT_SSH_COMMAND": f"ssh -i {SETUP_DIR / DEPLOY_KEY_FILE} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new",
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)


def publish_storage(storage_path: Path) -> str | None:
    if not enabled():
        return None
    storage = json.loads(storage_path.read_text(encoding="utf-8"))
    build_path = Path(storage.get("build_path") or "")
    ipks = build_packages(build_path)
    if not ipks:
        return None
    url = (SETUP_DIR / REPOSITORY_FILE).read_text(encoding="utf-8").strip()
    clone = SETUP_DIR / CLONE_DIR
    if not (clone / ".git").is_dir():
        _git("clone", "--quiet", url, str(clone))
    _git("pull", "--quiet", "--ff-only", cwd=clone)
    key_file = SETUP_DIR / SIGNING_KEY_FILE
    key_text = key_file.read_text(encoding="ascii") if key_file.is_file() else ""
    firmware_id = publish(
        ipks,
        clone,
        str(storage.get("esp_platform") or "").lower(),
        str(storage.get("name") or storage_path.stem),
        str(storage.get("esphome_version") or ""),
        key_text,
    )
    _git("add", "-A", cwd=clone)
    _git(
        "-c", "user.name=esphome-packages", "-c", "user.email=esphome-packages@localhost",
        "commit", "--quiet", "-m", f"{storage.get('name') or storage_path.stem}: firmware {firmware_id}",
        cwd=clone,
    )
    _git("push", "--quiet", "origin", "HEAD", cwd=clone)
    _LOGGER.info("Packages of %s published as firmware %s", storage.get("name"), firmware_id)
    return firmware_id


def main() -> int:
    parser = argparse.ArgumentParser(prog="package_publish")
    sub = parser.add_subparsers(dest="command", required=True)
    keys = sub.add_parser("keygen")
    keys.add_argument("--out", type=Path, required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("--packages", type=Path, required=True)
    pub.add_argument("--repo", type=Path, required=True)
    pub.add_argument("--board", required=True)
    pub.add_argument("--device", required=True)
    pub.add_argument("--version", required=True)
    args = parser.parse_args()
    if args.command == "keygen":
        print(keygen(args.out))
        return 0
    print(
        publish(
            sorted(args.packages.glob("*.ipk")),
            args.repo,
            args.board,
            args.device,
            args.version,
            os.environ.get(SIGNING_KEY_ENV, ""),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
