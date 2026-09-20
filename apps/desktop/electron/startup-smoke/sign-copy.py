"""Sign only a new disposable Electron copy, using the repository's local signer."""
import ast
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from typing import Optional


def sha256(file):
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(app):
    entries = []
    for root, directories, files in os.walk(app):
        for name in directories + files:
            file = Path(root) / name
            relative = file.relative_to(app).as_posix()
            if file.is_symlink():
                # Signing must not follow a copied link back into the dependency.
                if not file.resolve().is_relative_to(app):
                    raise ValueError(f"Bundle symlink escapes copy: {file}")
                entries.append({"path": relative, "symlink": os.readlink(file)})
            elif file.is_file():
                entries.append({"path": relative, "sha256": sha256(file),
                                "mode": file.stat().st_mode & 0o777})
    return sorted(entries, key=lambda item: item["path"])


def write_json(file, value):
    file.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def sign_copy(desktop, source, root):
    desktop, source, root = (item.resolve() for item in (desktop, source, root))
    copy = root / "Electron.app"
    if source.name != "Electron.app" or root.is_relative_to(source):
        raise ValueError("Expected an Electron dependency and a separate disposable root")
    if root.is_relative_to(Path("/Applications")):
        raise ValueError("Never sign an installed application")

    before = inventory(source)
    write_json(root / "source-before.json", before)
    # copytree refuses an existing destination; no caller-selected signing target.
    shutil.copytree(source, copy, symlinks=True)
    copied = inventory(copy)
    write_json(root / "copy-before.json", copied)
    if before != copied:
        raise RuntimeError("Disposable Electron copy differs before signing")

    helper_source = desktop.parents[1] / "hermes_cli/main_desktop.py"
    names = {"_desktop_macos_local_codesign", "_desktop_macos_bundle_id", "_codesign_verify"}
    functions = [node for node in ast.parse(helper_source.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in functions} != names:
        raise RuntimeError("Repository local signing helpers are missing")
    # Importing the CLI module loads unrelated config/runtime modules. Execute only
    # these inspected definitions, with their stdlib dependencies; no discovery or fallback.
    namespace = {"Path": Path, "Optional": Optional, "os": os,
                 "shutil": shutil, "subprocess": subprocess}
    module = ast.Module(body=[], type_ignores=[])
    module.body.extend(functions)
    exec(compile(module, str(helper_source), "exec"), namespace)
    try:
        if not namespace["_desktop_macos_local_codesign"](copy, desktop_dir=desktop, identity="-"):
            raise RuntimeError("Repository local signing helper did not verify the copy")
    finally:
        after = inventory(source)
        write_json(root / "source-after.json", after)
        if before != after:
            raise RuntimeError("Source Electron dependency changed during signing")

    codesign = "/usr/bin/codesign"
    verification = namespace["_codesign_verify"](codesign, copy, check=True, text=True)
    helpers = sorted(p for p in (copy / "Contents/Frameworks").rglob("*.app") if "Helper" in p.name)
    if not helpers:
        raise RuntimeError("No Electron helper apps found for entitlement verification")
    signatures = []
    for bundle in [copy, *helpers]:
        declared = desktop / "electron" / ("entitlements.mac.plist" if bundle == copy
                                           else "entitlements.mac.inherit.plist")
        expected = plistlib.loads(declared.read_bytes())
        actual = subprocess.run([codesign, "-d", "--entitlements", "-", "--xml", str(bundle)],
                                check=True, capture_output=True)
        entitlements = plistlib.loads(actual.stdout)
        if entitlements != expected:
            raise RuntimeError(f"Declared entitlements not preserved: {bundle}")
        details = subprocess.run([codesign, "-d", "--verbose=4", "-r-", str(bundle)],
                                 check=True, capture_output=True, text=True)
        signatures.append({"bundle": str(bundle.relative_to(copy)), "declared": str(declared),
                           "declaredSha256": sha256(declared), "entitlements": entitlements,
                           "entitlementsMatch": True, "details": details.stdout + details.stderr})
    write_json(root / "copy-signed.json", inventory(copy))
    inventories = {name: sha256(root / name) for name in (
        "source-before.json", "copy-before.json", "source-after.json", "copy-signed.json")}
    write_json(root / "signature.json", {
        "app": str(copy), "source": str(source), "kind": "ad-hoc", "identity": "-",
        "verified": True, "productionAcceptance": False,
        "helper": str(helper_source), "helperSourceSha256": sha256(helper_source),
        "verification": {"args": verification.args, "exitCode": verification.returncode,
                         "output": verification.stdout + verification.stderr},
        "copyEqualBeforeSigning": True, "sourceUnchanged": True,
        "inventories": inventories, "signatures": signatures,
    })


if __name__ == "__main__":
    if sys.platform != "darwin":
        raise SystemExit("Disposable ad-hoc signing requires macOS")
    sign_copy(*(Path(argument) for argument in sys.argv[1:]))
