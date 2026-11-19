"""Download verified fnpack and locked Linux CPython 3.12 wheels."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent


def main():
    key = "windows-amd64" if sys.platform == "win32" else "linux-amd64"
    config = json.loads((ROOT / "fnpack.lock.json").read_text())
    directory = ROOT / ".upstream"
    directory.mkdir(exist_ok=True)
    binary = directory / ("fnpack.exe" if sys.platform == "win32" else "fnpack")
    if not binary.exists() or hashlib.sha256(binary.read_bytes()).hexdigest() != config[key]:
        url = f"https://static2.fnnas.com/fnpack/fnpack-{config['version']}-{key}"
        with urlopen(url, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != config[key]:
            raise RuntimeError("fnpack download checksum mismatch")
        binary.write_bytes(data)
    binary.chmod(0o755)
    subprocess.run([
        sys.executable, "-m", "pip", "download", "--require-hashes", "--no-deps", "--only-binary=:all:",
        "--implementation", "cp", "--python-version", "3.12", "--abi", "cp312",
        "--platform", "manylinux_2_28_x86_64", "--platform", "manylinux2014_x86_64",
        "--platform", "manylinux_2_17_x86_64", "-r", str(ROOT / "requirements-linux.lock"),
        "--dest", str(directory / "linux-wheels"),
    ], check=True)
    print(f"Build tools and dependency wheels ready: {binary}")


if __name__ == "__main__":
    main()
