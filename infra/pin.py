#!/usr/bin/env python3
"""Pin third-party GitHub Actions to commit SHAs and container images to digests (supply-chain control).

    python3 infra/pin.py check     # CI: fail if any action is not a 40-hex SHA or disagrees with its tag comment
    python3 infra/pin.py actions   # re-resolve every `uses: owner/repo@<sha> # vX.Y.Z` from the tag in the comment
    python3 infra/pin.py images    # resolve every `# pin-image: <ref>` marker to <ref>@sha256:<digest>
    python3 infra/pin.py all

Conventions:
  * workflow lines look like   `uses: actions/checkout@<40-hex> # v7.0.1`   (the comment is the source of truth;
    to upgrade, edit the tag in the comment and run `actions`).
  * an image to pin is preceded by a line containing `# pin-image: <ref>` (e.g. python:3.12-slim); the next
    line that contains <ref> gets `<ref>@sha256:<digest>` (placeholder `@sha256:PIN_ME` is replaced).
Needs: git (network to github.com) for actions; docker buildx (or crane) for images.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
IMAGE_FILES = [ROOT / "backend" / "Dockerfile", ROOT / "sandbox" / "Dockerfile", ROOT / "infra" / "gcp" / "env.sh", *WORKFLOWS]
USES = re.compile(r"^(?P<pre>\s*-?\s*uses:\s*)(?P<repo>[\w.-]+/[\w./-]+)@(?P<ref>[^\s#]+)(?P<post>\s*#\s*(?P<tag>\S+).*)?$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def resolve_tag(repo: str, tag: str) -> str:
    base = "/".join(repo.split("/")[:2])  # owner/repo/path@ref -> owner/repo
    out = subprocess.run(["git", "ls-remote", "--tags", f"https://github.com/{base}", f"refs/tags/{tag}",
                          f"refs/tags/{tag}^{{}}"], capture_output=True, text=True, timeout=60, check=True).stdout
    peeled = [l.split()[0] for l in out.splitlines() if l.endswith("^{}")]
    plain = [l.split()[0] for l in out.splitlines() if l.endswith(f"refs/tags/{tag}")]
    sha = (peeled or plain or [""])[0]
    if not SHA.match(sha):
        raise SystemExit(f"cannot resolve {base}@{tag}")
    return sha


def actions(check_only: bool) -> int:
    bad = 0
    for wf in WORKFLOWS:
        lines = wf.read_text().splitlines(keepends=True)
        changed = False
        for i, line in enumerate(lines):
            m = USES.match(line.rstrip("\n"))
            if not m or m["repo"].startswith("./"):
                continue
            ref, tag = m["ref"], m["tag"]
            if check_only:
                if not SHA.match(ref):
                    print(f"UNPINNED {wf.name}:{i + 1}: {m['repo']}@{ref}")
                    bad = 1
                elif not tag:
                    print(f"NO TAG COMMENT {wf.name}:{i + 1}: {m['repo']}@{ref} (add '# vX.Y.Z')")
                    bad = 1
                continue
            if not tag:
                print(f"skip {wf.name}:{i + 1} (no '# tag' comment)")
                continue
            sha = resolve_tag(m["repo"], tag)
            if sha != ref:
                lines[i] = f"{m['pre']}{m['repo']}@{sha}{m['post']}\n"
                changed = True
                print(f"pinned {m['repo']} {tag} -> {sha}")
        if changed:
            wf.write_text("".join(lines))
    if check_only:
        print("actions: all pinned" if not bad else "actions: FIX with `python3 infra/pin.py actions`")
    return bad


def digest_of(ref: str) -> str:
    if shutil.which("docker"):
        p = subprocess.run(["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest.Digest}}"],
                           capture_output=True, text=True)
        if p.returncode == 0 and "sha256:" in p.stdout:
            return p.stdout.strip().strip('"')
    if shutil.which("crane"):
        p = subprocess.run(["crane", "digest", ref], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout.strip()
    raise SystemExit(f"cannot resolve digest for {ref} (need docker buildx or crane, and registry access)")


def images(check_only: bool) -> int:
    bad = 0
    for f in IMAGE_FILES:
        if not f.exists():
            continue
        lines = f.read_text().splitlines(keepends=True)
        changed = False
        for i, line in enumerate(lines):
            m = re.search(r"#\s*pin-image:\s*(\S+)", line)
            if not m:
                continue
            ref = m.group(1)
            for j in range(i + 1, min(i + 4, len(lines))):
                if ref in lines[j]:
                    pinned = re.search(re.escape(ref) + r"@sha256:[0-9a-f]{64}", lines[j])
                    if check_only:
                        if not pinned:
                            print(f"UNPINNED IMAGE {f.relative_to(ROOT)}:{j + 1}: {ref}")
                            bad = 1
                        break
                    d = digest_of(ref)
                    new = re.sub(re.escape(ref) + r"(@sha256:[0-9A-Za-z_]+)?", f"{ref}@{d}", lines[j], count=1)
                    if new != lines[j]:
                        lines[j] = new
                        changed = True
                        print(f"pinned {ref} -> {d}")
                    break
        if changed:
            f.write_text("".join(lines))
    return bad


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "check"
    if cmd == "check":
        rc = actions(True)
        if images(True):
            print("images: some are not pinned by digest (`python3 infra/pin.py images`) — deploy refuses PIN_ME")
            if "--strict" in argv:
                rc = 1
        return rc
    if cmd in ("actions", "all"):
        actions(False)
    if cmd in ("images", "all"):
        images(False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
