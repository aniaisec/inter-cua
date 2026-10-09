"""Local application preflight; target interaction requires an explicit probe.

Reports never serialize credentials, configuration validation inputs, response
bodies, or raw subprocess errors. Diagnosing must not mint keys or approvals.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Literal, NoReturn
from urllib.parse import urlsplit

import httpx
import yaml

from cua.artifact.recorder import load_family
from cua.artifact.schema import SCHEMA_VERSION
from cua.policy.allowlist import Policy, load_policy, origin_allowed
from cua.project import ProjectContext
from cua.secrets.resolver import SecretError, binding_for, parse_ref, resolve
from cua.surface.adapters import adapter_for
from cua.tenant import SYSTEM_SECRET_PREFIX, Tenant, load_tenant

Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    explanation: str
    remedy: str
    subject: str = ""


@dataclass
class Report:
    report_version: int = 1
    project: dict[str, object] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def add(
        self, code: str, severity: Severity, explanation: str, remedy: str, subject: str = ""
    ) -> None:
        self.findings.append(Finding(code, severity, explanation, remedy, subject))

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "ready": self.ready}, indent=2)

    def text(self) -> str:
        lines = [
            "cua doctor: " + ("ready for checked prerequisites" if self.ready else "checks failed")
        ]
        if self.project:
            lines.append(
                f"Project: {self.project['root']} (configuration v{self.project['config_version']})"
            )
            lines.append(
                f"inter-cua {self.project['package_version']}; Python {self.project['python']}; "
                f"{self.project['platform']}; capability schema "
                f"{self.project['capability_schema_version']}"
            )
        for finding in self.findings:
            subject = f" ({finding.subject})" if finding.subject else ""
            lines.append(f"[{finding.severity}] {finding.code}{subject}: {finding.explanation}")
            lines.append(f"  Remedy: {finding.remedy}")
        return "\n".join(lines)


class DoctorParser(argparse.ArgumentParser):
    """Doctor invocation errors use its report and exit contract, including JSON."""

    def error(self, message: str) -> NoReturn:
        raise ValueError("malformed doctor invocation")


def is_invocation(arguments: list[str]) -> bool:
    """Recognize the top-level command without confusing a root named doctor."""
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--root":
            index += 2
        elif token.startswith("--root="):
            index += 1
        else:
            return token == "doctor"
    return False


def invocation_error(*, json_output: bool, configuration: bool = False) -> int:
    report = Report()
    report.add(
        "PROJECT_INVALID" if configuration else "INVOCATION_INVALID",
        "error",
        "The selected project root or cua.toml is invalid."
        if configuration
        else "The doctor arguments are invalid.",
        "Select an existing --root directory and correct cua.toml (version 1)."
        if configuration
        else "Run cua doctor --help and correct the arguments.",
    )
    print(report.to_json() if json_output else report.text())
    return 64


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("doctor", help="Check local application prerequisites; no model calls")
    parser.add_argument("--tenant", default="local", help="Tenant name or YAML file")
    parser.add_argument("--json", action="store_true", help="Print a versioned JSON report")
    parser.add_argument(
        "--probe-browser", action="store_true", help="Launch and close local Chromium"
    )
    parser.add_argument(
        "--probe-app",
        action="store_true",
        help="Send an unauthenticated HEAD to the tenant base URL",
    )
    parser.add_argument("--policy", type=Path, help="Override the tenant policy")
    parser.add_argument("--families-dir", type=Path, default=Path("capabilities/families"))
    parser.add_argument("--capabilities-dir", type=Path, default=Path("capabilities"))


def main(args: argparse.Namespace) -> int:
    report = diagnose(
        args.project,
        args.tenant,
        policy_path=args.policy,
        families_dir=args.families_dir,
        capabilities_dir=args.capabilities_dir,
        probe_browser=args.probe_browser,
        probe_app=args.probe_app,
    )
    print(report.to_json() if args.json else report.text())
    return 0 if report.ready else 1


def diagnose(
    project: ProjectContext,
    tenant_name: str,
    *,
    policy_path: Path | None = None,
    families_dir: Path | None = None,
    capabilities_dir: Path | None = None,
    probe_browser: bool = False,
    probe_app: bool = False,
) -> Report:
    report = Report(
        project={
            "root": str(project.root),
            "configured": project.configured,
            "config_version": project.config.version,
            "capability_schema_version": SCHEMA_VERSION,
            "python": platform.python_version(),
            "platform": sys.platform,
        }
    )
    report.add(
        "PROJECT_OK", "info", "Project configuration is valid (version 1).", "No action needed."
    )
    try:
        version = metadata.version("inter-cua")
    except metadata.PackageNotFoundError:
        version = "uninstalled"
    report.project["package_version"] = version
    _runtime_directories(report, project)
    try:
        tenant = load_tenant(tenant_name, project=project)
    except (OSError, ValueError, yaml.YAMLError):
        report.add(
            "TENANT_INVALID",
            "error",
            "The tenant file is missing, unreadable, or invalid.",
            "Check the selected tenant YAML and configured tenants directory.",
        )
        return report
    report.project["tenant"] = tenant.id
    report.add("TENANT_OK", "info", "Tenant configuration is valid.", "No action needed.")
    target = "desktop" if tenant.desktop is not None else "web"
    descriptor = adapter_for(target)
    report.project["adapter"] = descriptor.summary() if descriptor else None
    if descriptor is None:
        report.add(
            "ADAPTER_UNAVAILABLE",
            "error",
            "This platform has no adapter for the tenant target.",
            "Use an interactive Windows session for Windows UI Automation.",
        )
    else:
        report.add(
            "ADAPTER_OK",
            "info",
            f"Selected {descriptor.name} v{descriptor.version} on {sys.platform}.",
            "No action needed.",
        )
    url_valid = _location(report, tenant)
    policy = _policy(report, policy_path or tenant.policy_file, tenant)
    family_surface = _family(report, tenant, families_dir or project.path("families"))
    if family_surface is not None and (
        descriptor is None or family_surface not in descriptor.targets
    ):
        report.add(
            "FAMILY_SURFACE_MISMATCH",
            "error",
            "The family surface does not match the tenant adapter.",
            "Set family target.surface to the tenant's supported surface kind.",
        )
    _secrets(report, project, tenant)
    _capabilities(report, project, tenant, capabilities_dir or project.path("capabilities"), target)
    if target == "web":
        available = _dependency(report, "playwright", required=True)
        if available:
            _chromium(report, project)
        if probe_browser and available:
            _browser_probe(report, project)
        elif probe_browser:
            report.add(
                "BROWSER_PROBE_SKIPPED",
                "warning",
                "Browser probe needs Playwright installed.",
                "Install inter-cua, then repeat --probe-browser.",
            )
    else:
        if sys.platform == "win32":
            _dependency(report, "comtypes", required=True, install="inter-cua[windows]")
        report.add(
            "DESKTOP_SESSION_UNVERIFIED",
            "warning",
            "Static checks cannot verify an interactive desktop.",
            "Run in an unlocked interactive Windows session and verify the target manually.",
        )
        if probe_browser:
            report.add(
                "BROWSER_PROBE_UNSUPPORTED",
                "error",
                "A browser probe does not check this desktop target.",
                "Omit --probe-browser for a Windows UIA tenant.",
            )
    for package, extra in (
        ("anthropic", "anthropic"),
        ("google-genai", "gemini"),
        ("numpy", "vision"),
        ("pillow", "vision"),
    ):
        _dependency(report, package, required=False, install=f"inter-cua[{extra}]")
    if probe_app:
        if target == "desktop":
            report.add(
                "APPLICATION_PROBE_UNSUPPORTED",
                "error",
                "A HEAD request cannot check a desktop application.",
                "Omit --probe-app and verify the Windows application manually.",
            )
        elif url_valid and policy is not None and _origin_allowed(policy, tenant.base_url):
            _application_probe(report, tenant.base_url)
        else:
            report.add(
                "APPLICATION_PROBE_BLOCKED",
                "error",
                "The application probe lacks a valid, allowed origin.",
                "Correct the tenant URL and policy before requesting --probe-app.",
            )
    report.add(
        "CHECK_SCOPE",
        "info",
        "Readiness covers only the reported checks; no model or replay was run.",
        "Use --probe-browser for a launch check and --probe-app for a HEAD reachability check.",
    )
    return report


def _location(report: Report, tenant: Tenant) -> bool:
    try:
        url = urlsplit(tenant.base_url)
        valid = bool(url.hostname) and url.username is None and url.password is None
        valid = valid and url.scheme in (("uia",) if tenant.desktop else ("http", "https"))
        _ = url.port  # Invalid ports must be rejected before a network request.
        if tenant.desktop is None:
            httpx.URL(tenant.base_url)  # Validate HTTP/IDNA syntax without connecting.
    except (ValueError, httpx.InvalidURL):
        valid = False
    if not valid:
        report.add(
            "APPLICATION_LOCATION_INVALID",
            "error",
            "The tenant application location is invalid.",
            "Use an http(s) base URL without embedded credentials, or uia:// for a desktop tenant.",
        )
    elif url.hostname and url.hostname.endswith(".invalid"):
        valid = False
        report.add(
            "APPLICATION_LOCATION_PLACEHOLDER",
            "error",
            "The tenant still names a reserved example host.",
            "Replace the template base_url with your application origin.",
        )
    return valid


def _origin_allowed(policy: Policy, location: str) -> bool:
    url = urlsplit(location)
    return origin_allowed(policy, location) and any(
        re.search(pattern, url.path or "/") for pattern in policy.allowed_paths
    )


def _policy(report: Report, path: Path, tenant: Tenant) -> Policy | None:
    try:
        policy = load_policy(path, tenant)
        patterns = [
            *policy.allowed_paths,
            *policy.credential_paths,
            policy.download_pattern,
            policy.sensitive_labels,
            *(p.pattern for p in policy.scrub_patterns),
            *(r.name for r in policy.risky_rules),
            *(r.location for r in policy.risky_rules if r.location is not None),
        ]
        for pattern in patterns:
            re.compile(pattern)
    except (OSError, ValueError, yaml.YAMLError, re.error):
        report.add(
            "POLICY_INVALID",
            "error",
            "The policy file or one of its regular expressions is invalid.",
            "Correct the policy YAML and regex fields; check its project-relative path.",
        )
        return None
    report.add(
        "POLICY_OK",
        "info",
        "Policy structure and regular expressions are valid.",
        "No action needed.",
    )
    if tenant.desktop is None:
        try:
            allowed = origin_allowed(policy, tenant.base_url)
        except ValueError:
            allowed = False
        if not allowed:
            report.add(
                "APPLICATION_POLICY_BLOCKED",
                "error",
                "The policy does not allow the tenant application origin.",
                "Allow the intended origin with narrowly scoped policy rules.",
            )
    return policy


def _family(report: Report, tenant: Tenant, directory: Path) -> str | None:
    try:
        family = load_family(tenant.app_family, directory)
    except (OSError, ValueError, yaml.YAMLError):
        report.add(
            "FAMILY_INVALID",
            "error",
            "The application family template is missing, unreadable, or invalid.",
            "Create or correct families/<app_family>.yaml in the configured families directory.",
        )
        return None
    report.add("FAMILY_OK", "info", "Application family template is valid.", "No action needed.")
    if family.target.vendor == "REPLACE_ME" or family.target.version_hint == "REPLACE_ME":
        report.add(
            "FAMILY_PLACEHOLDER",
            "warning",
            "The family still contains starter placeholders.",
            "Set the application vendor/version and define tested outcomes and recovery rules.",
        )
    return family.target.surface


def _secrets(report: Report, project: ProjectContext, tenant: Tenant) -> None:
    for key, binding in sorted(tenant.secrets.items()):
        subject = f"secret://{tenant.id}/{key}"
        try:
            resolve(subject, tenant, environ=project.environ, root=project.root)
        except (SecretError, OSError, ValueError):
            report.add(
                "SECRET_UNAVAILABLE",
                "error",
                "The secret source is missing, unreadable, empty, or has the wrong format.",
                f"Provide {binding.source} in format {binding.format}; no keys are created.",
                subject,
            )
        else:
            report.add(
                "SECRET_OK",
                "info",
                "The configured secret source is available and has the expected format.",
                "No action needed; keep its value private.",
                subject,
            )


def _capabilities(
    report: Report, project: ProjectContext, tenant: Tenant, directory: Path, target: str
) -> None:
    from cua import catalog
    from cua.artifact import requirements
    from cua.registry.store import Registry

    try:
        if directory.exists() and not directory.is_dir():
            raise NotADirectoryError
        entries, broken = catalog.scan(directory, project=project)
    except (OSError, ValueError):
        report.add(
            "CAPABILITY_DIRECTORY_INVALID",
            "error",
            "The capability directory or registry is unreadable.",
            "Correct the configured capability path and inspect the registry records.",
        )
        return
    for item in broken:
        report.add(
            "CAPABILITY_INVALID",
            "error",
            "A capability or registry record is unreadable or invalid.",
            "Inspect the artifact with cua describe and correct it as a draft.",
            str(item.path),
        )
    applicable = [
        entry
        for entry in entries
        if tenant.refusal(entry.capability.name, entry.capability.target.app_family) is None
    ]
    if not applicable:
        report.add(
            "CAPABILITY_NONE",
            "warning",
            "No capabilities apply to this tenant yet.",
            "Discover and record a draft, then describe and approve its exact content.",
        )
    for entry in applicable:
        cap = entry.capability
        for credential in cap.credentials.values():
            ref = credential.ref.replace("{tenant.id}", tenant.id)
            try:
                binding = binding_for(ref, tenant)
                _, key = parse_ref(ref)
                valid = not key.startswith(SYSTEM_SECRET_PREFIX) and set(credential.fields) <= set(
                    binding.fields
                )
            except SecretError:
                valid = False
            if not valid:
                report.add(
                    "CAPABILITY_CREDENTIAL_INVALID",
                    "error",
                    "The capability credential does not match an allowed tenant binding.",
                    "Use this tenant's application secrets and declared fields; "
                    "system signing keys cannot be application credentials.",
                    cap.name,
                )
        fits_target = (cap.target.surface == "desktop") == (target == "desktop")
        if not fits_target or requirements.refusal(cap) is not None:
            report.add(
                "CAPABILITY_INCOMPATIBLE",
                "error",
                "The capability requires an unavailable target or surface feature.",
                "Run cua surfaces and use an adapter that supports every required feature.",
                cap.name,
            )
        else:
            report.add(
                "CAPABILITY_OK",
                "info",
                f"Capability schema {cap.schema_version} and surface requirements are compatible.",
                "No action needed.",
                cap.name,
            )
        if entry.status != "approved":
            report.add(
                "CAPABILITY_NOT_APPROVED",
                "warning",
                f"Capability lifecycle status is {entry.status}.",
                "Describe and approve drafts; inspect registry status before replay.",
                cap.name,
            )
        elif not Registry(directory, project=project).approval_on_record(cap):
            report.add(
                "CAPABILITY_APPROVAL_MISSING",
                "error",
                "The registry has no approval for this exact capability content.",
                "Review and approve exact content, or restore the trusted registry record.",
                cap.name,
            )


def _dependency(
    report: Report, package: str, *, required: bool, install: str | None = None
) -> bool:
    try:
        version = metadata.version(package)
    except metadata.PackageNotFoundError:
        report.add(
            "UIA_DEPENDENCY_MISSING" if package == "comtypes" else "DEPENDENCY_MISSING",
            "error" if required else "warning",
            "The dependency is not installed.",
            f'Run python -m pip install "{install or package}".',
            package,
        )
        return False
    report.add(
        "DEPENDENCY_OK", "info", f"Installed version {version}.", "No action needed.", package
    )
    return True


def _write_probe(path: Path) -> None:
    """Test actual creation/write/delete, and remove only directories we created."""
    missing = [p for p in (path, *path.parents) if not p.exists()]
    created: list[Path] = []
    try:
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)
        with tempfile.NamedTemporaryFile(prefix=".cua-doctor-", dir=path) as stream:
            stream.write(b"doctor write probe\n")
            stream.flush()
    finally:
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise


def _runtime_directories(report: Report, project: ProjectContext) -> None:
    for name in ("runs", "state"):
        path = project.path(name)
        try:
            _write_probe(path)
        except OSError:
            report.add(
                "RUNTIME_NOT_WRITABLE",
                "error",
                "The runtime directory could not create, write, and remove a temporary file.",
                "Grant this user directory write access or change the configured runtime path.",
                str(path),
            )
        else:
            report.add(
                "RUNTIME_WRITABLE",
                "info",
                "Temporary file creation, writing, and cleanup succeeded.",
                "No action needed.",
                str(path),
            )


def _chromium_installations(project: ProjectContext) -> list[Path]:
    # Let the installed Playwright version select revisions, platform layouts,
    # and PLAYWRIGHT_BROWSERS_PATH. Its dry run downloads and launches nothing.
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
        env=project.environ,
        cwd=project.root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=True,
    )
    paths = [Path(p.strip()) for p in re.findall(r"Install location:\s*([^\r\n]+)", result.stdout)]
    paths = [p for p in paths if p.name.startswith(("chromium-", "chromium_headless_shell-"))]
    if not paths:
        raise ValueError("no Chromium installation locations reported")
    return paths


def _chromium(report: Report, project: ProjectContext) -> None:
    try:
        paths = _chromium_installations(project)
        for path in paths:
            shell = path.name.startswith("chromium_headless_shell-")
            names = (
                {
                    "headless_shell",
                    "headless_shell.exe",
                    "chrome-headless-shell",
                    "chrome-headless-shell.exe",
                }
                if shell
                else {"chrome", "chrome.exe", "Chromium", "Google Chrome for Testing"}
            )
            executables = [p for p in path.rglob("*") if p.name in names and p.is_file()]
            if not executables:
                report.add(
                    "CHROMIUM_MISSING",
                    "error",
                    "The expected Chromium executable is absent.",
                    "Run python -m playwright install chromium in this environment.",
                    str(path),
                )
            elif not any(os.access(p, os.R_OK | os.X_OK) for p in executables):
                report.add(
                    "CHROMIUM_NOT_EXECUTABLE",
                    "error",
                    "Chromium exists but cannot be read or executed.",
                    "Grant executable access or reinstall Chromium for this user.",
                    str(path),
                )
            else:
                report.add(
                    "CHROMIUM_OK",
                    "info",
                    "The expected Chromium executable is available.",
                    "Use --probe-browser to verify that this environment permits launching it.",
                    str(path),
                )
    except (OSError, ValueError, subprocess.SubprocessError):
        report.add(
            "CHROMIUM_CHECK_FAILED",
            "error",
            "Playwright could not report or inspect its Chromium installation.",
            "Check execution permissions; run python -m playwright install --dry-run chromium.",
        )


_BROWSER_SCRIPT = """
import json
from playwright.sync_api import sync_playwright
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, timeout=5000)
        browser.close()
except Exception as exc:
    text = str(exc).lower()
    code = 'BROWSER_LAUNCH_FAILED'
    if "executable doesn't exist" in text or 'executable does not exist' in text:
        code = 'BROWSER_EXECUTABLE_MISSING'
    elif any(t in text for t in (
        'permission denied', 'access is denied', 'operation not permitted', 'eacces', 'winerror 5'
    )):
        code = 'BROWSER_LAUNCH_DENIED'
    elif 'timeout' in text:
        code = 'BROWSER_LAUNCH_TIMEOUT'
    print(json.dumps({'code': code}))
else:
    print(json.dumps({'code': 'BROWSER_LAUNCH_OK'}))
"""


def _browser_probe(report: Report, project: ProjectContext) -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-c", _BROWSER_SCRIPT],
            env=project.environ,
            cwd=project.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=True,
        )
        code = json.loads(result.stdout)["code"]
    except PermissionError:
        code = "BROWSER_LAUNCH_DENIED"
    except subprocess.TimeoutExpired:
        code = "BROWSER_LAUNCH_TIMEOUT"
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        code = "BROWSER_LAUNCH_FAILED"
    messages = {
        "BROWSER_LAUNCH_OK": (
            "Chromium launched and closed without navigating to an application.",
            "No action needed.",
        ),
        "BROWSER_EXECUTABLE_MISSING": (
            "The browser probe could not find its executable.",
            "Run python -m playwright install chromium.",
        ),
        "BROWSER_LAUNCH_DENIED": (
            "The environment denied the browser launch.",
            "Check process/executable permissions; use an environment that permits Chromium.",
        ),
        "BROWSER_LAUNCH_TIMEOUT": (
            "The local browser probe timed out.",
            "Check local process restrictions and browser startup, then repeat --probe-browser.",
        ),
        "BROWSER_LAUNCH_FAILED": (
            "Chromium could not complete the local launch probe.",
            "Check system dependencies; on Linux run "
            "python -m playwright install --with-deps chromium.",
        ),
    }
    if not isinstance(code, str) or code not in messages:
        code = "BROWSER_LAUNCH_FAILED"
    explanation, remedy = messages[code]
    report.add(code, "info" if code == "BROWSER_LAUNCH_OK" else "error", explanation, remedy)


def _application_probe(report: Report, url: str) -> None:
    try:
        # No login, browser navigation, redirects, proxy credentials, or response bodies.
        with httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False) as client:
            response = client.head(url)
    except httpx.TimeoutException:
        report.add(
            "APPLICATION_TIMEOUT",
            "error",
            "The application HEAD request timed out.",
            "Check that the application is running and reachable from this machine.",
        )
    except httpx.HTTPError:
        report.add(
            "APPLICATION_UNREACHABLE",
            "error",
            "The application HEAD request could not connect or complete TLS.",
            "Check the tenant origin, service, network, and trusted TLS certificates.",
        )
    else:
        status = response.status_code
        severity: Severity
        if status in (401, 403):
            code, severity = "APPLICATION_AUTH_REQUIRED", "warning"
            remedy = (
                "The endpoint is reachable; verify login with the configured credential source."
            )
        elif status in (405, 501):
            code, severity = "APPLICATION_HEAD_UNSUPPORTED", "warning"
            remedy = (
                "Verify reachability manually; the application does not support this HEAD probe."
            )
        elif status >= 400:
            code, severity = "APPLICATION_HTTP_ERROR", "error"
            remedy = "Check the configured base URL and application health."
        else:
            code, severity = "APPLICATION_REACHABLE", "info"
            remedy = "No action needed; redirects and authentication were not followed."
        report.add(
            code,
            severity,
            f"The application returned HTTP {status} to an unauthenticated HEAD.",
            remedy,
        )
