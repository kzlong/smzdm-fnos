"""Assemble x86_64 Linux dependencies and package the native fnOS application."""

import argparse
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parent
APP = "smzdm-checkin"


def build_version(base, build_number=None, start_run_number=1):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", base):
        raise ValueError("Manifest version must contain three numeric parts")
    if build_number is None:
        return base
    if start_run_number < 1 or build_number < start_run_number:
        raise ValueError("Build number must be at least the configured start run number")
    major, minor, patch = map(int, base.split("."))
    return f"{major}.{minor}.{patch + build_number - start_run_number}"


def include_dependency(name, lean):
    if not lean or not name.startswith("Crypto/"):
        return True
    if name.startswith("Crypto/Util/"):
        return True
    if name in {"Crypto/__init__.py", "Crypto/Cipher/__init__.py", "Crypto/Cipher/DES.py", "Crypto/Cipher/_mode_ecb.py"}:
        return True
    return name.startswith(("Crypto/Cipher/_raw_des.", "Crypto/Cipher/_raw_ecb."))


def normalize_archive(source, destination, outer=False):
    """Write POSIX permissions even when the source archive was made on Windows."""
    with tarfile.open(fileobj=source, mode="r:gz") as original:
        with tarfile.open(fileobj=destination, mode="w:gz") as updated:
            for item in original:
                name = item.name.removeprefix("./")
                stream = original.extractfile(item) if item.isfile() else None
                if item.isdir():
                    item.mode = 0o755
                if stream:
                    item.mode = 0o755 if outer and name.startswith("cmd/") else 0o644
                    if outer and name == "app.tgz":
                        packed = io.BytesIO()
                        normalize_archive(io.BytesIO(stream.read()), packed)
                        item.size = packed.tell()
                        packed.seek(0)
                        stream = packed
                updated.addfile(item, stream)


def verify(path):
    with tarfile.open(path, "r:gz") as outer:
        members = {item.name.removeprefix("./"): item for item in outer}
        for name in ("manifest", "app.tgz", "cmd/main", "config/privilege", "config/resource", "ICON.PNG", "ICON_256.PNG"):
            if name not in members:
                raise ValueError(f"Missing FPK entry: {name}")
        for name, item in members.items():
            if name.startswith("cmd/") and item.isfile():
                data = outer.extractfile(item).read()
                if not (item.mode & 0o111) or b"\r" in data or not data.startswith(b"#!"):
                    raise ValueError(f"Invalid lifecycle script: {name} ({item.mode:o})")
        inner_bytes = outer.extractfile(members["app.tgz"]).read()
    with tarfile.open(fileobj=io.BytesIO(inner_bytes), mode="r:gz") as inner:
        files = {item.name.removeprefix("./") for item in inner if item.isfile()}
    for name in ("service.py", "ui/config", "ui/index.html", "ui/app.js", "ui/style.css",
                 "vendor/smzdm_bot/client.py", "vendor/pydantic_core/_pydantic_core.cpython-312-x86_64-linux-gnu.so"):
        if name not in files:
            raise ValueError(f"Missing application payload: {name}")


def build(fnpack, upstream, wheels, output, source, lean=False, version=None):
    (ROOT / ".upstream").mkdir(exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / ".upstream") as temp:
        stage = Path(temp) / APP
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns("__pycache__", "*.fpk", "vendor", ".DS_Store"))
        if version:
            manifest = stage / "manifest"
            lines = manifest.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if line.split("=", 1)[0].strip() == "version":
                    lines[index] = f"version = {version}"
            manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for script in (stage / "cmd").iterdir():
            script.write_bytes(script.read_bytes().replace(b"\r\n", b"\n"))
            script.chmod(0o755)
        vendor = stage / "app" / "vendor"
        vendor.mkdir()
        for wheel in sorted(wheels.glob("*.whl")):
            with zipfile.ZipFile(wheel) as archive:
                for name in archive.namelist():
                    if not name.endswith("/") and include_dependency(name, lean):
                        archive.extract(name, vendor)
        shutil.copytree(upstream / "src" / "smzdm_bot", vendor / "smzdm_bot", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(upstream / "LICENSE", vendor / "smzdm-bot-LICENSE")
        if (upstream / "NOTICE").exists():
            shutil.copyfile(upstream / "NOTICE", vendor / "smzdm-bot-NOTICE")
        subprocess.run([str(fnpack), "build"], cwd=stage, check=True)
        fpk = stage / f"{APP}.fpk"
        normalized = stage / "normalized.fpk"
        with fpk.open("rb") as source, normalized.open("wb") as destination:
            normalize_archive(source, destination, outer=True)
        verify(normalized)
        shutil.copyfile(normalized, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fnpack", type=Path)
    parser.add_argument("--source", type=Path, default=ROOT / APP)
    parser.add_argument("--lean", action="store_true", help="Package only DES/ECB from the cryptography library")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--build-number", type=int)
    parser.add_argument("--start-run-number", type=int, default=1)
    args = parser.parse_args()
    manifest = (args.source / "manifest").read_text(encoding="utf-8")
    metadata = dict(tuple(part.strip() for part in line.split("=", 1)) for line in manifest.splitlines() if "=" in line)
    try:
        version = build_version(metadata["version"], args.build_number, args.start_run_number)
    except ValueError as error:
        parser.error(str(error))
    output = args.output or ROOT / "dist" / f"{APP}-{version}-x86_64.fpk"
    build(args.fnpack.resolve(), ROOT / "upstream/smzdm-bot", ROOT / ".upstream/linux-wheels", output, args.source, args.lean, version)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as step_output:
            step_output.write(f"version={version}\n")
    print(output.resolve())
