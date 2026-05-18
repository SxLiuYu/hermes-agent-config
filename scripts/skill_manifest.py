#!/usr/bin/env python3
"""
Hermes Skill Manifest — declarative security declarations for skills.
Inspired by OpenClaw's clawmanifest.json (v2026.4.12).

Every skill can declare a manifest.json that specifies:
- Which filesystem paths it needs (read/write)
- Which network endpoints it contacts
- Which shell commands it executes (SHA256-pinned)
- Environment variables it requires
- Memory safety requirements

The validator runs BEFORE skill execution and enforces these declarations.
For sandboxed execution, the manifest generates a restricted environment.
"""

import json
import hashlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass, field


# ─── Manifest Schema ───────────────────────────────────────────────────────

@dataclass
class PathRule:
    """Filesystem access rule."""
    path: str
    access: str  # ro | rw | create
    pattern: Optional[str] = None  # glob pattern for matching

    def matches(self, target: str) -> bool:
        target = os.path.realpath(os.path.expanduser(target))
        base = os.path.realpath(os.path.expanduser(self.path))
        if self.pattern:
            import fnmatch
            return fnmatch.fnmatch(target, os.path.join(base, self.pattern))
        return target.startswith(base)

    def allowed_operation(self, operation: str) -> bool:
        """Check if operation (read/write/create/delete) is allowed."""
        if self.access == "ro":
            return operation in ("read",)
        if self.access == "rw":
            return operation in ("read", "write")
        if self.access == "create":
            return operation in ("read", "write", "create")
        if self.access == "delete":
            return True
        return False


@dataclass
class CommandRule:
    """Shell command rule with optional SHA256 pinning."""
    command: str  # base command name or full path
    sha256: Optional[str] = None  # hash of the binary for integrity check
    args_pattern: Optional[str] = None  # regex for allowed arguments
    allow_pipes: bool = False
    allow_redirects: bool = False

    def matches(self, cmd_string: str) -> Tuple[bool, Optional[str]]:
        """Check if command matches rule. Returns (allowed, reason_if_blocked)."""
        # Parse command
        try:
            parts = shlex.split(cmd_string)
        except ValueError:
            return False, "Invalid shell syntax"

        if not parts:
            return False, "Empty command"

        base_cmd = os.path.basename(parts[0])

        # Check command name
        cmd_name = os.path.basename(self.command)
        if base_cmd != cmd_name:
            return False, f"Command '{base_cmd}' not in allowlist"

        # Check pipes
        if "|" in cmd_string and not self.allow_pipes:
            return False, "Pipes not allowed"

        # Check redirects
        if any(c in cmd_string for c in (">", ">>", "<")) and not self.allow_redirects:
            return False, "Redirects not allowed"

        # Check args pattern
        if self.args_pattern:
            args_str = " ".join(parts[1:])
            if not re.match(self.args_pattern, args_str):
                return False, f"Arguments don't match pattern: {self.args_pattern}"

        # Check SHA256 if pinned
        if self.sha256:
            try:
                actual_hash = _hash_binary(parts[0])
                if actual_hash != self.sha256:
                    return False, f"SHA256 mismatch: expected {self.sha256[:12]}, got {actual_hash[:12]}"
            except Exception as e:
                return False, f"Hash verification failed: {e}"

        return True, None


@dataclass
class NetworkRule:
    """Network access rule."""
    host: str  # hostname or IP
    port: Optional[int] = None
    protocol: str = "https"  # http | https | tcp
    purpose: Optional[str] = None


@dataclass
class SkillManifest:
    """Complete skill manifest declaration."""
    name: str
    version: str
    description: Optional[str] = None
    author: Optional[str] = None
    min_hermes_version: Optional[str] = None

    # Access declarations
    filesystem: List[PathRule] = field(default_factory=list)
    commands: List[CommandRule] = field(default_factory=list)
    network: List[NetworkRule] = field(default_factory=list)
    env_vars: List[str] = field(default_factory=list)  # env vars this skill reads
    required_tools: List[str] = field(default_factory=list)

    # Safety
    side_effects: bool = True  # Does this skill modify state?
    requires_approval: bool = False  # Needs human approval before every run
    max_concurrent_instances: int = 1
    timeout_seconds: int = 600

    # Verify
    def validate(self) -> List[str]:
        """Validate manifest structure. Returns list of issues."""
        issues = []
        if not self.name or not re.match(r"^[a-z0-9_-]+$", self.name):
            issues.append("Invalid or missing name")
        if not self.version:
            issues.append("Missing version")
        if not self.filesystem and not self.commands and not self.network:
            issues.append("Manifest declares no access — skill can't do anything")

        # Validate paths exist or have valid patterns
        for pr in self.filesystem:
            expanded = os.path.expanduser(pr.path)
            if not os.path.exists(expanded) and "*" not in pr.path:
                issues.append(f"Path not found: {pr.path}")

        return issues


# ─── Manifest Validator ────────────────────────────────────────────────────

class ManifestValidator:
    """Validates runtime actions against a skill manifest."""

    def __init__(self, manifest: SkillManifest):
        self.manifest = manifest
        self._cmd_cache: Dict[str, str] = {}  # cmd → sha256

    def check_file_access(self, path: str, operation: str) -> Tuple[bool, Optional[str]]:
        """Check if file access is allowed by manifest."""
        if not self.manifest.filesystem:
            return True, None  # No restrictions declared — allow all (legacy mode)

        for rule in self.manifest.filesystem:
            if rule.matches(path):
                if rule.allowed_operation(operation):
                    return True, None
                return False, f"Operation '{operation}' not allowed on '{path}' (max: {rule.access})"

        return False, f"Path '{path}' not declared in manifest"

    def check_command(self, cmd_string: str) -> Tuple[bool, Optional[str]]:
        """Check if shell command is allowed by manifest."""
        if not self.manifest.commands:
            return True, None  # No restrictions — allow all (legacy mode)

        for rule in self.manifest.commands:
            allowed, reason = rule.matches(cmd_string)
            if allowed:
                return True, None

        return False, reason or "Command not in manifest allowlist"

    def check_network(self, host: str, port: Optional[int] = None) -> Tuple[bool, Optional[str]]:
        """Check if network access is allowed."""
        if not self.manifest.network:
            return True, None

        for rule in self.manifest.network:
            if rule.host == host or rule.host == "*":
                if rule.port is None or rule.port == port:
                    return True, None

        return False, f"Network access to {host}:{port or 'any'} not declared"


# ─── Manifest Loader ───────────────────────────────────────────────────────

def load_manifest(skill_dir: str) -> Optional[SkillManifest]:
    """Load manifest.json from a skill directory."""
    manifest_path = Path(skill_dir) / "manifest.json"
    if not manifest_path.exists():
        return None

    data = json.loads(manifest_path.read_text())

    manifest = SkillManifest(
        name=data["name"],
        version=data["version"],
        description=data.get("description"),
        author=data.get("author"),
        min_hermes_version=data.get("min_hermes_version"),
        side_effects=data.get("side_effects", True),
        requires_approval=data.get("requires_approval", False),
        max_concurrent_instances=data.get("max_concurrent_instances", 1),
        timeout_seconds=data.get("timeout_seconds", 600),
    )

    # Parse filesystem rules
    for fs in data.get("filesystem", []):
        manifest.filesystem.append(PathRule(
            path=fs["path"],
            access=fs.get("access", "ro"),
            pattern=fs.get("pattern"),
        ))

    # Parse command rules
    for cmd in data.get("commands", []):
        manifest.commands.append(CommandRule(
            command=cmd["command"],
            sha256=cmd.get("sha256"),
            args_pattern=cmd.get("args_pattern"),
            allow_pipes=cmd.get("allow_pipes", False),
            allow_redirects=cmd.get("allow_redirects", False),
        ))

    # Parse network rules
    for net in data.get("network", []):
        manifest.network.append(NetworkRule(
            host=net["host"],
            port=net.get("port"),
            protocol=net.get("protocol", "https"),
            purpose=net.get("purpose"),
        ))

    manifest.env_vars = data.get("env_vars", [])
    manifest.required_tools = data.get("required_tools", [])

    return manifest


def generate_manifest(
    skill_dir: str,
    name: str,
    version: str = "1.0",
    interactive: bool = False,
) -> SkillManifest:
    """Generate a manifest by scanning skill files (semi-automatic)."""
    skill_path = Path(skill_dir)
    commands = set()
    paths = set()
    network = set()

    # Scan Python files for shell commands
    for py_file in skill_path.rglob("*.py"):
        content = py_file.read_text()
        # Find subprocess.run / os.system calls
        for m in re.finditer(r'subprocess\.run\(\["([^"]+)"', content):
            commands.add(m.group(1))
        for m in re.finditer(r'os\.system\(["\']([^"\']+)', content):
            parts = shlex.split(m.group(1))
            if parts:
                commands.add(parts[0])

    # Scan for file paths
    for m in re.finditer(r'["\'](~?\/[^"\']+\.\w+)["\']', content):
        paths.add(m.group(1))

    # Scan for URLs
    for m in re.finditer(r'https?://([^/"\'\s]+)', content):
        network.add(m.group(1))

    manifest = SkillManifest(
        name=name,
        version=version,
        description=f"Auto-generated for {name}",
        filesystem=[PathRule(path=p, access="rw") for p in sorted(paths)[:20]],
        commands=[CommandRule(command=c, allow_pipes=True, allow_redirects=True)
                  for c in sorted(commands)[:30]],
        network=[NetworkRule(host=h, protocol="https") for h in sorted(network)[:10]],
        side_effects=True,
    )

    if interactive:
        print(f"\n=== Auto-detected for {name} ===")
        print(f"Commands: {sorted(commands)}")
        print(f"Paths: {sorted(paths)}")
        print(f"Network: {sorted(network)}")
        resp = input("\nSave manifest.json? [y/N] ")
        if resp.lower() == "y":
            _save_manifest(skill_path, manifest)

    return manifest


# ─── Sandboxed Execution ───────────────────────────────────────────────────

class SandboxedRunner:
    """Execute a skill with sandbox restrictions from its manifest."""

    def __init__(self, manifest: SkillManifest):
        self.manifest = manifest
        self.validator = ManifestValidator(manifest)
        self.violations: List[str] = []

    def run_command(self, cmd_string: str, cwd: Optional[str] = None) -> Tuple[int, str, str]:
        """Run a shell command, enforcing command rules."""
        allowed, reason = self.validator.check_command(cmd_string)
        if not allowed:
            self.violations.append(f"Blocked command: {reason}")
            return -1, "", f"MANIFEST VIOLATION: {reason}"

        try:
            result = subprocess.run(
                cmd_string, shell=True, capture_output=True, text=True,
                timeout=self.manifest.timeout_seconds,
                cwd=cwd or os.getcwd(),
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            self.violations.append("Command timed out")
            return -1, "", "TIMEOUT"

    def check_path(self, path: str, operation: str = "read") -> bool:
        """Check if file path access is allowed."""
        allowed, reason = self.validator.check_file_access(path, operation)
        if not allowed:
            self.violations.append(f"Blocked file access: {reason}")
        return allowed

    def report(self) -> str:
        """Generate violation report."""
        if not self.violations:
            return "✅ No manifest violations"
        return "❌ Violations:\n" + "\n".join(f"  - {v}" for v in self.violations)


# ─── Manifest ↔ SKILL.md Integration ──────────────────────────────────────

def extract_manifest_from_skill_md(skill_md_path: str) -> Optional[Dict]:
    """Extract manifest-like declarations from SKILL.md frontmatter."""
    content = Path(skill_md_path).read_text()

    # Parse YAML frontmatter
    if content.startswith("---"):
        try:
            import yaml
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = yaml.safe_load(parts[1])
                return _frontmatter_to_manifest(frontmatter)
        except Exception:
            pass
    return None


def _frontmatter_to_manifest(fm: Dict) -> Dict:
    """Convert SKILL.md frontmatter to manifest-like structure."""
    manifest = {
        "name": fm.get("name", "unknown"),
        "version": fm.get("version", "1.0"),
        "description": fm.get("description", ""),
        "side_effects": fm.get("side_effects", True),
        "requires_approval": fm.get("requires_approval", False),
    }

    # Extract tool requirements from frontmatter
    tools = fm.get("tools", [])
    if tools:
        manifest["required_tools"] = tools

    # Extract filesystem hints
    paths = fm.get("paths", [])
    if paths:
        manifest["filesystem"] = [
            {"path": p, "access": "rw"} for p in paths
        ]

    return manifest


# ─── Utility Functions ─────────────────────────────────────────────────────

def _hash_binary(binary_path: str) -> str:
    """Compute SHA256 of a binary."""
    # Resolve full path
    if "/" not in binary_path:
        result = subprocess.run(["which", binary_path], capture_output=True, text=True)
        if result.returncode == 0:
            binary_path = result.stdout.strip()
        else:
            raise FileNotFoundError(f"Binary not found: {binary_path}")

    with open(binary_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _save_manifest(skill_dir: Path, manifest: SkillManifest):
    """Save manifest as JSON."""
    data = {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "side_effects": manifest.side_effects,
        "requires_approval": manifest.requires_approval,
        "filesystem": [
            {"path": r.path, "access": r.access, "pattern": r.pattern}
            for r in manifest.filesystem
        ],
        "commands": [
            {
                "command": r.command,
                "sha256": r.sha256,
                "args_pattern": r.args_pattern,
                "allow_pipes": r.allow_pipes,
                "allow_redirects": r.allow_redirects,
            }
            for r in manifest.commands
        ],
        "network": [
            {"host": r.host, "port": r.port, "protocol": r.protocol}
            for r in manifest.network
        ],
    }
    path = skill_dir / "manifest.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"✅ Saved: {path}")


# ─── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Hermes Skill Manifest — security declarations for skills"
    )
    sub = parser.add_subparsers(dest="command")

    # validate command
    validate = sub.add_parser("validate", help="Validate a manifest.json")
    validate.add_argument("skill_dir", help="Path to skill directory containing manifest.json")

    # generate command
    gen = sub.add_parser("generate", help="Auto-generate manifest from skill files")
    gen.add_argument("skill_dir", help="Path to skill directory")
    gen.add_argument("--name", required=True, help="Skill name")
    gen.add_argument("--version", default="1.0", help="Skill version")
    gen.add_argument("--interactive", action="store_true", help="Interactive mode")

    # scan-all command
    scan_all = sub.add_parser("scan-all", help="Scan all skills and report manifest status")
    scan_all.add_argument("--skills-dir", default="~/.hermes/skills",
                          help="Root skills directory")

    # extract command (from SKILL.md)
    extract = sub.add_parser("extract", help="Extract manifest from SKILL.md frontmatter")
    extract.add_argument("skill_md", help="Path to SKILL.md")

    args = parser.parse_args()

    if args.command == "validate":
        manifest = load_manifest(args.skill_dir)
        if manifest is None:
            print(f"❌ No manifest.json found in {args.skill_dir}")
            print("   Run: python skill_manifest.py generate {args.skill_dir} --name <name>")
            sys.exit(1)

        issues = manifest.validate()
        if issues:
            print(f"❌ Manifest issues ({len(issues)}):")
            for i in issues:
                print(f"   - {i}")
            sys.exit(1)
        else:
            print(f"✅ Manifest '{manifest.name}' v{manifest.version} is valid")
            print(f"   Filesystem rules: {len(manifest.filesystem)}")
            print(f"   Command rules: {len(manifest.commands)}")
            print(f"   Network rules: {len(manifest.network)}")

    elif args.command == "generate":
        generate_manifest(
            args.skill_dir,
            name=args.name,
            version=args.version,
            interactive=args.interactive,
        )

    elif args.command == "scan-all":
        skills_dir = Path(args.skill_dir).expanduser()
        print(f"Scanning: {skills_dir}\n")

        total = 0
        with_manifest = 0

        for skill_path in sorted(skills_dir.iterdir()):
            if not skill_path.is_dir():
                continue
            total += 1

            manifest_file = skill_path / "manifest.json"
            skill_md = skill_path / "SKILL.md"

            if manifest_file.exists():
                manifest = load_manifest(str(skill_path))
                issues = manifest.validate() if manifest else ["failed to load"]
                status = "✅" if not issues else "⚠️"
                with_manifest += 1
            elif skill_md.exists():
                status = "📝 (SKILL.md only)"
            else:
                status = "❌ (no files)"

            print(f"  {status} {skill_path.name}")

        print(f"\nSummary: {with_manifest}/{total} skills have manifest.json")

    elif args.command == "extract":
        data = extract_manifest_from_skill_md(args.skill_md)
        if data:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print("❌ Could not extract manifest from SKILL.md frontmatter")
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()