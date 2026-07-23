from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import struct
import zlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "logs" / "mobile_capture" / "debug" / "ths_switcher_base.apk"
DEFAULT_OUTPUT = ROOT / "logs" / "mobile_capture" / "debug" / "ths_switcher_usb_fixed.apk"
DEFAULT_KEY = ROOT / "logs" / "mobile_capture" / "debug" / "ths_switcher_patch_key.pem"

REPLACEMENTS = {
    # Keep DEX strings length-stable; avoid userinfo URLs on Android HttpURLConnection.
    b"http://192.168.1.3:5001": b"http://127.000.0.1:5001",
    b"http://192.168.44.1:5001": b"http://127.000.0001:5001",
    b"http://192.168.42.129:5001": b"http://127.000.000.01:5001",
}


def _load_or_create_key(path: Path) -> rsa.RSAPrivateKey:
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return key


def _make_cert(key: rsa.RSAPrivateKey) -> x509.Certificate:
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "THS Switcher Local Patch")])
    now = dt.datetime.now(dt.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )


def _b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _patch_dex(data: bytes) -> tuple[bytes, dict[str, int]]:
    patched = bytearray(data)
    counts: dict[str, int] = {}
    for old, new in REPLACEMENTS.items():
        if len(old) != len(new):
            raise ValueError(f"replacement length mismatch: {old!r} -> {new!r}")
        count = patched.count(old)
        if count != 1:
            raise ValueError(f"expected one occurrence of {old!r}, got {count}")
        patched = bytearray(bytes(patched).replace(old, new, 1))
        counts[old.decode("ascii")] = count

    patched[12:32] = hashlib.sha1(patched[32:]).digest()
    patched[8:12] = struct.pack("<I", zlib.adler32(patched[12:]) & 0xFFFFFFFF)
    return bytes(patched), counts


def _manifest_sections(files: list[tuple[str, bytes]]) -> tuple[bytes, list[bytes]]:
    main = b"Manifest-Version: 1.0\r\nCreated-By: 1.0 (Android)\r\n\r\n"
    sections = []
    for name, data in files:
        section = f"Name: {name}\r\nSHA-256-Digest: {_b64_sha256(data)}\r\n\r\n".encode("ascii")
        sections.append(section)
    return main + b"".join(sections), sections


def _signature_file(manifest: bytes, sections: list[bytes]) -> bytes:
    out = [
        b"Signature-Version: 1.0\r\n",
        b"Created-By: 1.0 (Android)\r\n",
        f"SHA-256-Digest-Manifest: {_b64_sha256(manifest)}\r\n\r\n".encode("ascii"),
    ]
    for section in sections:
        first = section.split(b"\r\n", 1)[0] + b"\r\n"
        out.append(first)
        out.append(f"SHA-256-Digest: {_b64_sha256(section)}\r\n\r\n".encode("ascii"))
    return b"".join(out)


def _pkcs7_signature(sf_data: bytes, key: rsa.RSAPrivateKey, cert: x509.Certificate) -> bytes:
    return (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(sf_data)
        .add_signer(cert, key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary, pkcs7.PKCS7Options.NoCapabilities])
    )


def patch_apk(input_apk: Path, output_apk: Path, key_path: Path) -> dict[str, object]:
    entries: list[tuple[ZipInfo, bytes]] = []
    counts: dict[str, int] = {}
    with ZipFile(input_apk, "r") as zin:
        for info in zin.infolist():
            if info.filename.upper().startswith("META-INF/"):
                continue
            data = zin.read(info.filename)
            if info.filename == "classes.dex":
                data, counts = _patch_dex(data)
            entries.append((info, data))

    files = [(info.filename, data) for info, data in entries]
    manifest, sections = _manifest_sections(files)
    sf_data = _signature_file(manifest, sections)
    key = _load_or_create_key(key_path)
    cert = _make_cert(key)
    rsa_data = _pkcs7_signature(sf_data, key, cert)

    output_apk.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output_apk, "w") as zout:
        for old_info, data in entries:
            info = ZipInfo(old_info.filename, old_info.date_time)
            info.compress_type = old_info.compress_type if old_info.filename != "resources.arsc" else ZIP_STORED
            info.external_attr = old_info.external_attr
            zout.writestr(info, data)
        for name, data in (
            ("META-INF/MANIFEST.MF", manifest),
            ("META-INF/CERT.SF", sf_data),
            ("META-INF/CERT.RSA", rsa_data),
        ):
            info = ZipInfo(name, (2026, 7, 22, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            zout.writestr(info, data)

    return {"output": str(output_apk), "counts": counts, "size": output_apk.stat().st_size}


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch THS switcher preset URLs to USB reverse localhost.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY)
    args = parser.parse_args()
    print(patch_apk(args.input, args.output, args.key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
